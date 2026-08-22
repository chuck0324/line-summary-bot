import os
import sys
import threading
import psycopg2
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

# ==================== 環境變數設定與檢查 ====================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

missing_envs = []
if not LINE_CHANNEL_SECRET: missing_envs.append("LINE_CHANNEL_SECRET")
if not LINE_CHANNEL_ACCESS_TOKEN: missing_envs.append("LINE_CHANNEL_ACCESS_TOKEN")
if not GEMINI_API_KEY: missing_envs.append("GEMINI_API_KEY")
if not DATABASE_URL: missing_envs.append("DATABASE_URL")

if missing_envs:
    print(f"❌ 錯誤：缺少以下環境變數: {', '.join(missing_envs)}")
    sys.exit(1)

# 初始化 LINE SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Google Gemini API
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-3.5-flash")

# ==================== PostgreSQL 資料庫操作 ====================
def get_db_connection():
    """建立資料庫連線"""
    return psycopg2.connect(DATABASE_URL)

def get_user_profile(user_id):
    """讀取使用者的長期特徵記憶"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_name, profile_summary FROM user_profiles WHERE user_id = %s;", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {"name": row[0], "summary": row[1]}
    except Exception as e:
        print(f"[DB Error] 讀取 Profile 失敗: {e}")
    return {"name": "成員", "summary": "尚無長期記憶紀錄。"}

def update_user_profile_async(user_id, user_name, user_msg, bot_reply):
    """背景非同步提煉並更新使用者的長期記憶"""
    def task():
        try:
            profile = get_user_profile(user_id)
            old_summary = profile["summary"]
            
            prompt = f"""
你是一個長期個人特徵分析助手。請根據以下【舊有特徵與偏好】與【最新一次對話】，更新並提煉出關於成員「{user_name}」的【最新個人特徵摘要】。

【舊有特徵與偏好】：
{old_summary}

【最新對話】：
使用者 ({user_name})：{user_msg}
機器人：{bot_reply}

請輸出更新後的【最新個人特徵摘要】（包含：喜好、性格特點、常聊話題、特別習慣或背景等，請保持精簡條理，控制在 150 字內）：
"""
            response = gemini_model.generate_content(prompt)
            new_summary = response.text.strip()

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_profiles (user_id, user_name, profile_summary, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) 
                DO UPDATE SET user_name = EXCLUDED.user_name, profile_summary = EXCLUDED.profile_summary, updated_at = NOW();
            """, (user_id, user_name, new_summary))
            conn.commit()
            cur.close()
            conn.close()
            print(f"[Memory Updated] 已成功更新 {user_name} ({user_id}) 的個人記憶！")
        except Exception as e:
            print(f"[Memory Error] 更新 Profile 失敗: {e}")

    # 使用獨立 Thread 在背景執行，不卡住 LINE 訊息回覆速度
    threading.Thread(target=task).start()

# ==================== Gemini AI 生成邏輯 ====================
def generate_ai_response(user_id, user_name, user_msg):
    # 1. 撈取長期記憶
    profile = get_user_profile(user_id)
    user_summary = profile["summary"]

    # 2. 組合 System Instruction
    system_instruction = f"""
你是一個活潑、在地且貼心的 LINE 群組機器人助手。
你現在正在和成員【{user_name}】對話。

【關於 {user_name} 的長期了解與個人偏好】：
{user_summary}

請根據以上長期了解，用自然、親切且符合人設的口吻回覆【{user_name}】的發言。不需要太過正式，像熟絡的朋友聊天即可。
"""

    prompt = f"使用者【{user_name}】說：{user_msg}"

    # 3. 呼叫 Gemini 3.5 Flash
    response = gemini_model.generate_content(
        contents=[system_instruction, prompt]
    )
    bot_reply = response.text.strip()

    # 4. 啟動背景 Task 提煉並更新記憶
    update_user_profile_async(user_id, user_name, user_msg, bot_reply)

    return bot_reply

# ==================== Flask Webhook 路由 ====================
@app.route("/", methods=['GET'])
def index():
    return "LINE Bot Status: Active"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

# ==================== LINE 訊息事件處理 ====================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()

    # 嘗試抓取使用者在 LINE 上的暱稱
    user_name = "成員"
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            # 優先嘗試抓取群組內 Profile，若無則抓取個人 Profile
            if event.source.type == "group":
                profile = line_bot_api.get_group_member_profile(event.source.group_id, user_id)
            else:
                profile = line_bot_api.get_profile(user_id)
            user_name = profile.display_name
    except Exception as e:
        print(f"[LINE Profile Error] 抓取使用者暱稱失敗: {e}")

    # 呼叫 Gemini AI 生成回覆
    reply_text = generate_ai_response(user_id, user_name, user_msg)

    # 回覆訊息給使用者
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
