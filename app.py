import os
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import google.generativeai as genai

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

group_chat_history = {}

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    source_type = event.source.type
    group_id = event.source.group_id if source_type == 'group' else event.source.user_id
    user_text = event.message.text

    if group_id not in group_chat_history:
        group_chat_history[group_id] = []

    if "摘要" in user_text:
        if len(group_chat_history[group_id]) == 0:
            reply_text = "目前還沒有累積足夠的對話記錄可以摘要喔！"
        else:
            full_logs = "\n".join(group_chat_history[group_id][-100:])
            prompt = f"請幫我針對以下 LINE 群組對話紀錄進行重點摘要與待辦事項整理：\n\n{full_logs}"
            
            response = model.generate_content(prompt)
            reply_text = response.text

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
    else:
        group_chat_history[group_id].append(user_text)
        if len(group_chat_history[group_id]) > 200:
            group_chat_history[group_id].pop(0)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
