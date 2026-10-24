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


THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
THAI_MONTHS = [  # (ชื่อเต็ม, ชื่อย่อ ไม่มีจุด)
    ("มกราคม", "มค"), ("กุมภาพันธ์", "กพ"), ("มีนาคม", "มีค"), ("เมษายน", "เมย"),
    ("พฤษภาคม", "พค"), ("มิถุนายน", "มิย"), ("กรกฎาคม", "กค"), ("สิงหาคม", "สค"),
    ("กันยายน", "กย"), ("ตุลาคม", "ตค"), ("พฤศจิกายน", "พย"), ("ธันวาคม", "ธค"),
]


def to_ce_year(y):
    """รับปี 2 หลัก/4 หลัก (ค.ศ. หรือ พ.ศ.) คืนค่าเป็น ค.ศ. 4 หลัก
    26 -> 2026, 69 -> 2026 (พ.ศ. 2569), 2569 -> 2026, 2026 -> 2026"""
    y = int(y)
    if y < 100:
        return 2000 + y if y < 43 else y + 2500 - 543
    if y >= 2400:
        return y - 543
    return y


def parse_slip_month(date_text):
    """อ่านเดือน/ปีจากคอลัมน์ 'วันที่ในสลิป' เช่น '16 ต.ค. 2569 - 17:03'
    คืนค่า (เดือน, ปี ค.ศ.) หรือ None ถ้าอ่านไม่ได้"""
    s = unicodedata.normalize("NFKC", str(date_text)).translate(THAI_DIGITS)
    s = s.replace(".", "")

    # รูปแบบตัวเลข เช่น 16/10/2569
    m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4}|\d{2})(?!\d)", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(2)), to_ce_year(m.group(3))

    month = None
    for i, (full, abbr) in enumerate(THAI_MONTHS, 1):
        if full in s or abbr in s:
            month = i
            break
    y = re.search(r"(?<!\d)(\d{4})(?!\d)", s)
    if month and y:
        return month, to_ce_year(y.group(1))
    return None


def parse_month_filter(query):
    """แยกเงื่อนไขเดือนออกจากคำค้น เช่น 'expe เดือน 9/26' หรือ 'expe 9/2569'
    คืนค่า (คำค้นที่เหลือ, (เดือน, ปี ค.ศ.) หรือ None)"""
    m = re.search(r"(?:เดือน\s*)?(?<!\d)(\d{1,2})\s*/\s*(\d{4}|\d{2})(?!\d)", query)
    if m and 1 <= int(m.group(1)) <= 12:
        rest = (query[:m.start()] + " " + query[m.end():]).strip()
        return rest, (int(m.group(1)), to_ce_year(m.group(2)))
    return query, None


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text.strip()
    # ตอบเฉพาะเมื่อพิมพ์ "slip ..." (ไม่สนตัวพิมพ์เล็ก/ใหญ่) กันตอบมั่วเวลาคุยปกติ
    # ตัวอย่าง: "slip จริส" / "slip expe เดือน 9/26" / "slip เดือน 10/26"
    parts = text.split(None, 1)
    if len(parts) < 2 or parts[0].lower() != SEARCH_COMMAND:
        return

    rest, month_filter = parse_month_filter(parts[1])
    keyword = norm(rest)
    if not keyword and not month_filter:
        return

    try:
        rows = get_sheet().get_all_values()[1:]
    except Exception:
        log("ERROR search:")
        log(traceback.format_exc())
        return

    found = [r for r in rows if keyword in norm(" ".join(r[:6]))] if keyword else list(rows)

    unreadable = 0
    if month_filter:
        in_month = []
        for r in found:
            ym = parse_slip_month(r[1]) if len(r) > 1 else None
            if ym is None:
                unreadable += 1
            elif ym == month_filter:
                in_month.append(r)
        found = in_month

    log("SEARCH keyword =", repr(keyword), "month =", month_filter,
        "rows =", len(rows), "found =", len(found), "unreadable_date =", unreadable)

    label = f"[{rest.strip()}]" if rest.strip() else ""
    if month_filter:
        label = (label + f" เดือน {month_filter[0]}/{month_filter[1] + 543}").strip()

    if not found:
        msg = f"ไม่พบสลิปที่ตรงกับ {label}"
    else:
        total = 0.0
        for r in found:
            try:
                total += float(str(r[4]).replace(",", ""))
            except (ValueError, IndexError):
                pass
        lines = [f"พบ {len(found)} สลิป {label} รวม {total:,.2f} บาท\n"]
        shown = found[-15:]
        for r in reversed(shown):
            lines.append(f"- {r[1] or r[0]} | {r[3]} | {r[4]} บาท | อ้างอิง {r[5]}")
        if len(found) > len(shown):
            lines.append(f"\n(แสดง {len(shown)} รายการล่าสุด จากทั้งหมด {len(found)})")
        msg = "\n".join(lines)

    if unreadable:
        msg += f"\n\nหมายเหตุ: มี {unreadable} แถวที่อ่านวันที่ในสลิปไม่ได้ จึงไม่นับรวม"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
