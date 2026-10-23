import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_text = event.message.text.strip()
    
    # ถ้าพิมพ์คำว่า "ค้นหา" หรือคำสั่งค้นหา
    if user_text.startswith("หา"):
        keyword = user_text.replace("หา", "").strip()
        # ตรงนี้จำลองการตอบกลับข้อมูลที่ค้นหา (เดี๋ยวเราผูกกับ Google Sheets ต่อครับ)
        reply_msg = f"ผลการค้นหาสำหรับ [{keyword}]:\n- ยังไม่พบข้อมูลในระบบ (กำลังเชื่อมต่อฐานข้อมูล)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    # เมื่อมีคนส่งรูปสลิปเข้ามา บอทจะดึงรูปต้นฉบับเก็บไว้
    message_id = event.message.id
    
    # ตัวอย่างการตอบกลับยืนยันว่าได้รับสลิปแล้วและกำลังบันทึก
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="ได้รับสลิปเรียบร้อย บันทึกเข้าสู่ระบบแล้วครับ")
    )

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
