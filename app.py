import os
import sys
import re
from datetime import datetime
from collections import Counter
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
import psycopg2

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Google GenAI Client
client = genai.Client(api_key=GEMINI_API_KEY)

# 記憶體快取 (近 200 則訊息供「綺語」快速查詢)
group_chat_history = {}

# --- PostgreSQL 資料庫控制 ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_logs (
                id SERIAL PRIMARY KEY,
                group_id VARCHAR(255) NOT NULL,
                user_id VARCHAR(255) NOT NULL,
                message_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        print("PostgreSQL 資料庫初始化完成！", file=sys.stderr)
    except Exception as e:
        print(f"DB Init Error: {e}", file=sys.stderr)

init_db()

def log_message_to_db(group_id, user_id, message_text):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO message_logs (group_id, user_id, message_text) VALUES (%s, %s, %s)',
            (group_id, user_id, message_text)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"DB Log Error: {e}", file=sys.stderr)

def get_custom_leaderboard(group_id, days=1, start_date=None, end_date=None):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if start_date and end_date:
            query = '''
                SELECT user_id, COUNT(*) as msg_count 
                FROM message_logs 
                WHERE group_id = %s AND created_at::date BETWEEN %s::date AND %s::date
                GROUP BY user_id ORDER BY msg_count DESC LIMIT 5
            '''
            cursor.execute(query, (group_id, start_date, end_date))
        else:
            query = f'''
                SELECT user_id, COUNT(*) as msg_count 
                FROM message_logs 
                WHERE group_id = %s AND created_at >= NOW() - INTERVAL '{days} days'
                GROUP BY user_id ORDER BY msg_count DESC LIMIT 5
            '''
            cursor.execute(query, (group_id,))
            
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"DB Leaderboard Error: {e}", file=sys.stderr)
        return []

# --- LINE Webhook 處理 ---

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
        source_type = event.source.type
        if source_type == 'group':
            group_id = event.source.group_id
        elif source_type == 'room':
            group_id = event.source.room_id
        else:
            group_id = event.source.user_id

        user_id = getattr(event.source, 'user_id', 'unknown_user')
        user_text = event.message.text.strip()

        if group_id not in group_chat_history:
            group_chat_history[group_id] = []

        reply_text = None

        # 【指令 1：摘要 (可帶數字，例如「摘要 50」或「摘要」)】
        match_summary = re.match(r"^摘要\s*(\d+)?$", user_text)
        if match_summary:
            limit = int(match_summary.group(1)) if match_summary.group(1) else 100
            limit = min(limit, 200)

            if len(group_chat_history[group_id]) == 0:
                reply_text = "目前還沒有收到新對話喔！"
            else:
                try:
                    formatted_logs = [f"[{msg['user_id'][:6]}]: {msg['text']}" for msg in group_chat_history[group_id][-limit:]]
                    full_logs = "\n".join(formatted_logs)
                    
                    prompt = f"""你是一個高效率的群組對話整理助手。以下是 LINE 群組的最新對話紀錄。
由於成員會同時討論多個不同話題，請幫我執行以下任務：
1. **話題分類**：將對話依照「不同討論主題/專案」拆解開來（忽略打招呼、梗圖文字、閒聊等無關雜訊）。
2. **主題摘要**：每個主題列出【主題名稱】、討論重點摘要、目前結論與待辦事項。

對話紀錄如下：\n{full_logs}"""

                    response = client.models.generate_content(
                        model='gemini-3.5-flash',
                        contents=prompt
                    )
                    reply_text = response.text
                except Exception as ai_err:
                    print(f"Gemini 呼叫失敗: {ai_err}", file=sys.stderr)
                    reply_text = f"摘要產出失敗，原因：{ai_err}"

        # 【指令 2：綺語 (記憶體近 200 則榜)】
        elif user_text in ["綺語", "綺語榜"]:
            if not group_chat_history[group_id]:
                reply_text = "目前還沒有對話紀錄可以統計喔！"
            else:
                user_counts = Counter(msg['user_id'] for msg in group_chat_history[group_id])
                top_users = user_counts.most_common(5)
                
                msg_lines = ["🪷 近期「綺語」排行榜（前 5 名）："]
                for rank, (uid, count) in enumerate(top_users, 1):
                    msg_lines.append(f"第 {rank} 名: {uid[:6]}... ({count} 則訊息)")
                reply_text = "\n".join(msg_lines)

        # 【指令 3：今日廢話王】
        elif user_text == "今日廢話王":
            leaderboard = get_custom_leaderboard(group_id, days=1)
            if not leaderboard:
                reply_text = "今日還沒有足夠的對話紀錄喔！"
            else:
                msg_lines = [f"👑 今日廢話王排行榜 ({datetime.now().strftime('%Y-%m-%d')})："]
                for rank, (uid, count) in enumerate(leaderboard, 1):
                    msg_lines.append(f"第 {rank} 名: {uid[:6]}... ({count} 則發言)")
                reply_text = "\n".join(msg_lines)

        # 【指令 4：自訂天數廢話王 (例如：廢話王 7 / 廢話王 30天)】
        elif re.match(r"^廢話王\s*(\d+)\s*天?$", user_text):
            match_days = re.match(r"^廢話王\s*(\d+)\s*天?$", user_text)
            days = int(match_days.group(1))
            leaderboard = get_custom_leaderboard(group_id, days=days)
            if not leaderboard:
                reply_text = f"近 {days} 天還沒有足夠的對話紀錄喔！"
            else:
                msg_lines = [f"👑 近 {days} 天廢話王排行榜："]
                for rank, (uid, count) in enumerate(leaderboard, 1):
                    msg_lines.append(f"第 {rank} 名: {uid[:6]}... ({count} 則發言)")
                reply_text = "\n".join(msg_lines)

        # 【一般訊息：同時存入記憶體與 PostgreSQL 雲端資料庫】
        else:
            group_chat_history[group_id].append({'user_id': user_id, 'text': user_text})
            if len(group_chat_history[group_id]) > 200:
                group_chat_history[group_id].pop(0)
            
            # 寫入 PostgreSQL 雲端資料庫
            log_message_to_db(group_id, user_id, user_text)

        # 統一回傳訊息
        if reply_text:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                )

    except Exception as e:
        print(f"handle_message 發生例外: {e}", file=sys.stderr)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
