import os
import sys
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

# 設定 API Key
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    # 使用 gemini-1.5-flash-latest 避開 404 錯誤
    #Google API 對於 v1beta 的模型名稱解析較為嚴格，不允許帶有 -latest 尾綴。請將 app.py 中初始化模型的宣告改為 gemini-1.5-flash（不帶 -latest），並手動指定 API 版本為 v1
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
    except Exception as e:
        print(f"Callback 處理發生錯誤: {e}", file=sys.stderr)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    try:
        if hasattr(event.source, 'group_id'):
            group_id = event.source.group_id
        elif hasattr(event.source, 'user_id'):
            group_id = event.source.user_id
        else:
            group_id = "default_room"

        user_text = event.message.text

        if group_id not in group_chat_history:
            group_chat_history[group_id] = []

        if "摘要" in user_text:
            if len(group_chat_history[group_id]) == 0:
                reply_text = "目前還沒有收到新對話喔！請先在群組內聊幾句後再輸入「摘要」。"
            else:
                try:
                    full_logs = "\n".join(group_chat_history[group_id][-100:])
                    prompt = f"請幫我針對以下 LINE 群組對話紀錄進行重點摘要與待辦事項整理：\n\n{full_logs}"
                    response = model.generate_content(prompt)
                    reply_text = response.text
                except Exception as ai_err:
                    print(f"Gemini 呼叫失敗: {ai_err}", file=sys.stderr)
                    reply_text = f"摘要產出失敗，原因：{ai_err}"

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

    except Exception as e:
        print(f"handle_message 發生例外: {e}", file=sys.stderr)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
