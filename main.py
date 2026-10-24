import os
import io
import json
import base64
import threading
import time
import re
import unicodedata
import traceback
from datetime import datetime, timezone, timedelta

import requests
import gspread
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

# Pillow เป็นตัวเลือก: ถ้าติดตั้งไว้ จะย่อรูปก่อนส่งให้ Gemini (เร็วขึ้น)
try:
    from PIL import Image
except ImportError:
    Image = None

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("CHANNEL_SECRET"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
SHEET_ID = os.environ.get("SHEET_ID")
TH_TZ = timezone(timedelta(hours=7))
SEARCH_COMMAND = "slip"  # คำสั่งค้นหาในกลุ่ม เช่น "slip จริส"

MODELS = [GEMINI_MODEL]
if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
    MODELS.append(GEMINI_FALLBACK_MODEL)

PROMPT = (
    "ดูรูปนี้ ถ้าเป็นสลิปโอนเงินธนาคารไทยให้ดึงข้อมูลและตอบเป็น JSON เท่านั้น "
    'รูปแบบ {"is_slip": true, "datetime": "วันที่เวลาตามที่เห็นในสลิป", '
    '"sender": "ชื่อผู้โอน", "receiver": "ชื่อผู้รับ", "amount": 0.00, "ref": "รหัสอ้างอิง"} '
    'ถ้าไม่ใช่สลิปโอนเงินให้ตอบ {"is_slip": false}'
)


def log(*args):
    print(*args, flush=True)


def norm(s):
    """ทำข้อความให้เทียบกันได้: NFKC, ตัดช่องว่างและอักขระล่องหน, ตัวพิมพ์เล็ก"""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"[\s\u200b\u200c\u200d\u2060\ufeff]+", "", s)
    return s.lower()


# ---------------------------------------------------------------- Google Sheet
def get_sheet():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    gc = gspread.service_account_from_dict(info)
    return gc.open_by_key(SHEET_ID).sheet1


# ---------------------------------------------------------------------- Gemini
def shrink_image(image_bytes, max_side=1280):
    """ย่อรูปให้ด้านยาวไม่เกิน max_side แล้วบีบเป็น JPEG (ถ้ามี Pillow)"""
    if Image is None:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        log("shrink_image failed, ใช้รูปเดิม:", type(e).__name__)
        return image_bytes


def call_gemini(payload):
    """เรียก Gemini พร้อม retry และโมเดลสำรอง (คีย์ส่งใน header ไม่ติดใน URL/ล็อก)"""
    last_err = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(
                    url,
                    json=payload,
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    timeout=30,
                )
            except requests.exceptions.RequestException as e:
                last_err = f"{model} {type(e).__name__}"
                log("GEMINI retry:", last_err)
                continue

            if r.status_code in (429, 500, 503):
                last_err = f"{model} {r.status_code}"
                log("GEMINI retry:", last_err)
                time.sleep(2)
                continue

            if r.status_code == 404:
                # ชื่อโมเดลไม่ถูก/ใช้ไม่ได้ ข้ามไปโมเดลถัดไปเลย
                last_err = f"{model} 404"
                log("GEMINI model not found:", model)
                break

            if not r.ok:
                # แสดงเฉพาะสถานะและข้อความ error ไม่แสดง URL
                raise RuntimeError(f"Gemini {model} HTTP {r.status_code}: {r.text[:300]}")

            return r.json()

    raise RuntimeError(f"Gemini ไม่พร้อมใช้งาน: {last_err}")


def read_slip(image_bytes):
    image_bytes = shrink_image(image_bytes)
    payload = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(image_bytes).decode()}},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    result = call_gemini(payload)
    text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

    # กันกรณีโมเดลห่อด้วย ```json ... ```
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    return json.loads(text)


# ------------------------------------------------------------ ประมวลผลสลิป
def process_slip(message_id, group_id):
    # ทำงานเงียบๆ เบื้องหลัง ไม่ตอบกลับในกลุ่ม
    try:
        log("1) รับสลิป message_id =", message_id)
        content = line_bot_api.get_message_content(message_id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        log("2) ดึงรูปแล้ว ขนาด", len(image_bytes), "bytes")

        data = read_slip(image_bytes)
        log("3) Gemini อ่านได้:", data)

        if not data.get("is_slip"):
            log("ไม่ใช่สลิป ข้ามการบันทึก")
            return

        sheet = get_sheet()

        # กันแถวซ้ำ (LINE อาจส่ง webhook ซ้ำ) เช็กจากคอลัมน์ G = message_id
        if message_id in sheet.col_values(7):
            log("มี message_id นี้แล้ว ข้ามการบันทึก")
            return

        now = datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([
            now,
            data.get("datetime", ""),
            data.get("sender", ""),
            data.get("receiver", ""),
            data.get("amount", 0),
            data.get("ref", ""),
            message_id,
            group_id,
        ])
        log("4) บันทึกลง Sheet แล้ว")
    except Exception:
        log("ERROR process_slip:")
        log(traceback.format_exc())


# ----------------------------------------------------------------------- LINE
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/", methods=["GET", "HEAD"])
def health():
    return "OK"


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    src = event.source
    group_id = (
        getattr(src, "group_id", None)
        or getattr(src, "room_id", None)
        or getattr(src, "user_id", "")
    )
    threading.Thread(
        target=process_slip, args=(event.message.id, group_id), daemon=True
    ).start()


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text.strip()
    # ตอบเฉพาะเมื่อพิมพ์ "slip " ตามด้วยคำค้น (ไม่สนตัวพิมพ์เล็ก/ใหญ่) กันตอบมั่วเวลาคุยปกติ
    parts = text.split(None, 1)
    if len(parts) < 2 or parts[0].lower() != SEARCH_COMMAND:
        return
    keyword = norm(parts[1])
    if not keyword:
        return

    try:
        rows = get_sheet().get_all_values()[1:]
    except Exception:
        log("ERROR search:")
        log(traceback.format_exc())
        return

    found = [r for r in rows if keyword in norm(" ".join(r[:6]))]
    log("SEARCH keyword =", repr(keyword), "rows =", len(rows), "found =", len(found))
    if not found:
        msg = f"ไม่พบสลิปที่ตรงกับ [{keyword}]"
    else:
        total = 0.0
        for r in found:
            try:
                total += float(str(r[4]).replace(",", ""))
            except (ValueError, IndexError):
                pass
        lines = [f"พบ {len(found)} สลิป รวม {total:,.2f} บาท\n"]
        for r in reversed(found[-15:]):
            lines.append(f"- {r[1] or r[0]} | {r[3]} | {r[4]} บาท | อ้างอิง {r[5]}")
        msg = "\n".join(lines)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
