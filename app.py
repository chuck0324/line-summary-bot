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
from google import genai

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

# 初始化 Google Gemini Client
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ==================== PostgreSQL 資料庫操作 ====================
def get_db_connection():
    """建立資料庫連線"""
    return psycopg2.connect(DATABASE_URL)

def get_user_profile(user_id):
    """讀取使用者的完整特徵記憶（含自我描述與他人印象）"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT user_name, self_description, others_opinion, profile_summary 
            FROM user_profiles WHERE user_id = %s;
        """, (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {
                "name": row[0],
                "self_desc": row[1] or "無",
                "others_opinion": row[2] or "無",
                "summary": row[3] or "尚無紀錄"
            }
    except Exception as e:
        print(f"[DB Error] 讀取 Profile 失敗: {e}")
    return {"name": "成員", "self_desc": "無", "others_opinion": "無", "summary": "尚無紀錄"}

def get_user_profile_by_name(user_name):
    """依據姓名搜尋用戶記錄"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, self_description, others_opinion 
            FROM user_profiles WHERE user_name = %s;
        """, (user_name,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {"user_id": row[0], "self_desc": row[1] or "", "others_opinion": row[2] or ""}
    except Exception as e:
        print(f"[DB Error] 依姓名讀取 Profile 失敗: {e}")
    return None

def update_user_profile_async(sender_id, sender_name, user_msg, bot_reply):
    """背景非同步處理：精準區分【自我提及】與【他人評價】"""
    def task():
        try:
            # 1. 取得發言者記憶
            sender_profile = get_user_profile(sender_id)

            # 2. 讓 Gemini 分析這句話屬於「自我描述」還是「描述他人」
            prompt = f"""
你是一個長期記憶與情報分析助手。請分析發言者【{sender_name}】所說的話：

【發言者】：{sender_name}
【對話內容】：{user_msg}

任務：
1. 判斷這段話主要是在描述誰？（寫出名字，若是發言者自己請寫 "{sender_name}"）。
2. 判斷這段話的性質：
   - 如果是「{sender_name}」在講【自己】的事（例如：我喜歡吃辣），標註 TYPE: SELF
   - 如果是「{sender_name}」在評價/描述【別人】（例如：小明很怕狗），標註 TYPE: OTHERS
3. 提取出最新的特徵短句（20字以內精簡重點）。

請嚴格按格式輸出：
TARGET_NAME: 目標姓名
TYPE: [SELF 或 OTHERS]
FEATURE: 提煉出的特徵重點
"""
            response = ai_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt
            )
            result_text = response.text.strip()

            target_name = sender_name
            desc_type = "SELF"
            feature = ""

            for line in result_text.split("\n"):
                if line.startswith("TARGET_NAME:"):
                    target_name = line.replace("TARGET_NAME:", "").strip()
                elif line.startswith("TYPE:"):
                    desc_type = line.replace("TYPE:", "").strip()
                elif line.startswith("FEATURE:"):
                    feature = line.replace("FEATURE:", "").strip()

            if not feature:
                return

            # 3. 確定目標 ID 與資料欄位
            target_id = sender_id
            if target_name != sender_name:
                existing = get_user_profile_by_name(target_name)
                if existing:
                    target_id = existing["user_id"]
                else:
                    target_id = f"named_{target_name}"

            target_profile = get_user_profile(target_id)

            conn = get_db_connection()
            cur = conn.cursor()

            if desc_type == "SELF" and target_name == sender_name:
                # 更新自我描述
                new_self = f"{target_profile['self_desc']}; {feature}".strip("; ")
                new_summary = f"【自我陳述】：{new_self}\n【朋友印象】：{target_profile['others_opinion']}"
                
                cur.execute("""
                    INSERT INTO user_profiles (user_id, user_name, self_description, profile_summary, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) 
                    DO UPDATE SET user_name = EXCLUDED.user_name, self_description = EXCLUDED.self_description, profile_summary = EXCLUDED.profile_summary, updated_at = NOW();
                """, (target_id, target_name, new_self, new_summary))
            else:
                # 更新他人評價（帶上是誰說的）
                new_opinion = f"{target_profile['others_opinion']}; [{sender_name}提到]: {feature}".strip("; ")
                new_summary = f"【自我陳述】：{target_profile['self_desc']}\n【朋友印象】：{new_opinion}"

                cur.execute("""
                    INSERT INTO user_profiles (user_id, user_name, others_opinion, profile_summary, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) 
                    DO UPDATE SET user_name = EXCLUDED.user_name, others_opinion = EXCLUDED.others_opinion, profile_summary = EXCLUDED.profile_summary, updated_at = NOW();
                """, (target_id, target_name, new_opinion, new_summary))

            conn.commit()
            cur.close()
            conn.close()
            print(f"[Memory Classified] 已分類更新【{target_name}】的記憶庫（類型: {desc_type}）！")

        except Exception as e:
            print(f"[Memory Error] 分類更新 Profile 失敗: {e}")

    threading.Thread(target=task).start()

# ==================== Gemini AI 生成邏輯 ====================
def generate_ai_response(user_id, user_name, user_msg):
    # 1. 撈取發言者的長期記憶
    profile = get_user_profile(user_id)

    # 2. 組合 System Instruction
    system_instruction = f"""
你是一個活潑、在地且貼心的 LINE 群組機器人助手。
你現在正在和成員【{user_name}】對話。

【關於 {user_name} 的長期紀錄】：
- 他自己的介紹：{profile['self_desc']}
- 其他朋友對他的評價/描述：{profile['others_opinion']}

【互動原則】：
1. 請根據上述背景，用自然、親切的口吻回覆。
2. 區分「他自己說過的話」與「別人對他的評價」。如果別人爆料的事，可以帶點幽默調侃；如果是他自己強調的事，要展現尊重與認同。
3. 輕鬆聊天即可，不要太正式。
"""

    prompt = f"成員【{user_name}】說：{user_msg}"

    # 3. 呼叫 Gemini 3.6 Flash
    response = ai_client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[system_instruction, prompt]
    )
    bot_reply = response.text.strip()

    # 4. 背景更新記憶
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

    user_name = "成員"
    try:
        with ApiClient(configuration) as api_client_line:
            line_bot_api = MessagingApi(api_client_line)
            if event.source.type == "group":
                profile = line_bot_api.get_group_member_profile(event.source.group_id, user_id)
            else:
                profile = line_bot_api.get_profile(user_id)
            user_name = profile.display_name
    except Exception as e:
        print(f"[LINE Profile Error] 抓取使用者暱稱失敗: {e}")

    reply_text = generate_ai_response(user_id, user_name, user_msg)

    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
