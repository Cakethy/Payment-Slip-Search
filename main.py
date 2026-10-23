import os
import json
import base64
import threading
from datetime import datetime, timezone, timedelta

import requests
import gspread
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
SHEET_ID = os.environ.get('SHEET_ID')
TH_TZ = timezone(timedelta(hours=7))

PROMPT = (
    "ดูรูปนี้ ถ้าเป็นสลิปโอนเงินธนาคารไทยให้ดึงข้อมูลและตอบเป็น JSON เท่านั้น "
    'รูปแบบ {"is_slip": true, "datetime": "วันที่เวลาตามที่เห็นในสลิป", '
    '"sender": "ชื่อผู้โอน", "receiver": "ชื่อผู้รับ", "amount": 0.00, "ref": "รหัสอ้างอิง"} '
    'ถ้าไม่ใช่สลิปโอนเงินให้ตอบ {"is_slip": false}'
)


def get_sheet():
    info = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    gc = gspread.service_account_from_dict(info)
    return gc.open_by_key(SHEET_ID).sheet1


def read_slip(image_bytes):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(image_bytes).decode()}}
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def process_slip(message_id, group_id):
    # ทำงานเงียบๆ เบื้องหลัง ไม่ตอบกลับในกลุ่ม
    try:
        content = line_bot_api.get_message_content(message_id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        data = read_slip(image_bytes)
        if not data.get("is_slip"):
            return
        now = datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M:%S")
        get_sheet().append_row([
            now,
            data.get("datetime", ""),
            data.get("sender", ""),
            data.get("receiver", ""),
            data.get("amount", 0),
            data.get("ref", ""),
            message_id,
            group_id,
        ])
        print("บันทึกสลิปแล้ว:", data)
    except Exception as e:
        print("ERROR process_slip:", e)


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    src = event.source
    group_id = getattr(src, 'group_id', None) or getattr(src, 'room_id', None) or getattr(src, 'user_id', '')
    threading.Thread(target=process_slip, args=(event.message.id, group_id), daemon=True).start()


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text.strip()
    # ตอบเฉพาะเมื่อพิมพ์ "หา " ตามด้วยคำค้น (กันตอบมั่วเวลาคุยปกติ)
    if not text.startswith("หา "):
        return
    keyword = text[2:].strip().lower()
    if not keyword:
        return

    try:
        rows = get_sheet().get_all_values()[1:]
    except Exception as e:
        print("ERROR search:", e)
        return

    found = [r for r in rows if keyword in " ".join(r[:6]).lower()]
    if not found:
        msg = f"ไม่พบสลิปที่ตรงกับ [{keyword}]"
    else:
        total = 0.0
        for r in found:
            try:
                total += float(r[4])
            except (ValueError, IndexError):
                pass
        lines = [f"พบ {len(found)} สลิป รวม {total:,.2f} บาท\n"]
        for r in reversed(found[-15:]):
            lines.append(f"- {r[1] or r[0]} | {r[3]} | {r[4]} บาท | อ้างอิง {r[5]}")
        msg = "\n".join(lines)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
