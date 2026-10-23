import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage

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
    # ตรงนี้บอทจะรับข้อความไปประมวลผลเงียบๆ โดยไม่ส่งข้อความตอบกลับ
    print(f"ได้รับข้อความ: {user_text}")

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    # ตรงนี้บอทจะรับรูปภาพ/สลิปไปประมวลผลเงียบๆ โดยไม่ส่งข้อความตอบกลับเช่นกัน
    print("ได้รับรูปภาพ/สลิปแล้ว")

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
