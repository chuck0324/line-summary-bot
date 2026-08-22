import os
import sys
import re
import random
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

client = genai.Client(api_key=GEMINI_API_KEY)

# 記憶體快取
group_chat_history = {}
group_games = {}  # 儲存遊戲暫存狀態

# --- LINE API Profile 抓取真實暱稱 ---

def get_user_name(group_id, user_id):
    if user_id == 'unknown_user':
        return "未知用戶"
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
            return profile.display_name
    except Exception as e:
        print(f"Get Profile Error for {user_id}: {e}", file=sys.stderr)
        return f"用戶({user_id[:6]})"

# --- PostgreSQL 資料庫控制 ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. 聊天紀錄表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_logs (
                id SERIAL PRIMARY KEY,
                group_id VARCHAR(255) NOT NULL,
                user_id VARCHAR(255) NOT NULL,
                message_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 2. 帳務表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                group_id VARCHAR(255) NOT NULL,
                payer_name VARCHAR(100) NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                item_name VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 3. 餐廳清單表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS restaurants (
                id SERIAL PRIMARY KEY,
                group_id VARCHAR(255) NOT NULL,
                restaurant_name VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        cursor.close()
        conn.close()
        print("PostgreSQL 所有資料表初始化完成！", file=sys.stderr)
    except Exception as e:
        print(f"DB Init Error: {e}", file=sys.stderr)

init_db()

# --- 基本紀錄與搜尋 ---

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

def search_db_messages(group_id, keyword, limit=5):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = '''
            SELECT user_id, message_text, created_at 
            FROM message_logs 
            WHERE group_id = %s AND message_text LIKE %s 
            ORDER BY created_at DESC LIMIT %s
        '''
        cursor.execute(query, (group_id, f'%{keyword}%', limit))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"DB Search Error: {e}", file=sys.stderr)
        return []

# --- 分帳助手邏輯 ---

def add_expense(group_id, payer_name, amount, item_name):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO expenses (group_id, payer_name, amount, item_name) VALUES (%s, %s, %s, %s)',
            (group_id, payer_name, amount, item_name)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Add Expense Error: {e}", file=sys.stderr)
        return False

def calculate_expenses(group_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT payer_name, amount, item_name FROM expenses WHERE group_id = %s', (group_id,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        if not rows:
            return "目前沒有任何記帳紀錄喔！"
        
        total_amount = sum(row[1] for row in rows)
        payers = set(row[0] for row in rows)
        person_count = len(payers)
        avg_amount = total_amount / person_count if person_count > 0 else 0
        
        balances = {p: 0.0 for p in payers}
        details = []
        for p, amt, item in rows:
            balances[p] += float(amt)
            details.append(f"• {p} 付了 ${amt:.0f} ({item})")
            
        for p in balances:
            balances[p] -= float(avg_amount)
            
        debtors = []
        creditors = []
        for p, bal in balances.items():
            if bal < -0.01:
                debtors.append([p, -bal])
            elif bal > 0.01:
                creditors.append([p, bal])
                
        transfers = []
        i, j = 0, 0
        while i < len(debtors) and j < len(creditors):
            debtor, d_amount = debtors[i]
            creditor, c_amount = creditors[j]
            settle_amount = min(d_amount, c_amount)
            
            transfers.append(f"👉 {debtor} 應給 {creditor} ${settle_amount:.0f}")
            
            debtors[i][1] -= settle_amount
            creditors[j][1] -= settle_amount
            
            if debtors[i][1] < 0.01:
                i += 1
            if creditors[j][1] < 0.01:
                j += 1

        res = [
            f"💰 【群組結帳統計】",
            f"總花費：${total_amount:.0f}",
            f"參與人數：{person_count} 人 (每人均分：${avg_amount:.0f})",
            "\n【消費明細】"
        ] + details + ["\n【最佳平帳方案】"] + (transfers if transfers else ["大家費用剛剛好，不用互相轉帳！"])
        
        return "\n".join(res)
    except Exception as e:
        print(f"Calc Expenses Error: {e}", file=sys.stderr)
        return "結帳計算失敗，請稍後再試。"

def clear_expenses(group_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM expenses WHERE group_id = %s', (group_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Clear Expenses Error: {e}", file=sys.stderr)
        return False

# --- 美食抽籤邏輯 ---

def add_restaurant(group_id, name):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO restaurants (group_id, restaurant_name) VALUES (%s, %s)', (group_id, name))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Add Restaurant Error: {e}", file=sys.stderr)
        return False

def pick_restaurant(group_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT restaurant_name FROM restaurants WHERE group_id = %s', (group_id,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        if not rows:
            return None
        return random.choice(rows)[0]
    except Exception as e:
        print(f"Pick Restaurant Error: {e}", file=sys.stderr)
        return None

# --- Webhook 主邏輯 ---

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

        # 1. 摘要
        match_summary = re.match(r"^摘要\s*(\d+)?$", user_text)
        if match_summary:
            limit = int(match_summary.group(1)) if match_summary.group(1) else 100
            limit = min(limit, 200)

            if len(group_chat_history[group_id]) == 0:
                reply_text = "目前還沒有收到新對話喔！"
            else:
                try:
                    formatted_logs = [f"[{get_user_name(group_id, m['user_id'])}]: {m['text']}" for m in group_chat_history[group_id][-limit:]]
                    full_logs = "\n".join(formatted_logs)
                    prompt = f"你是一個高效率的群組對話整理助手。以下是 LINE 群組的最新對話紀錄：\n1. 話題分類\n2. 主題摘要與待辦事項。\n\n對話紀錄：\n{full_logs}"
                    response = client.models.generate_content(model='gemini-3.5-flash', contents=prompt)
                    reply_text = response.text
                except Exception as ai_err:
                    reply_text = f"摘要產出失敗：{ai_err}"

        # 2. 綺語
        elif user_text in ["綺語", "綺語榜"]:
            if not group_chat_history[group_id]:
                reply_text = "目前還沒有對話紀錄可以統計喔！"
            else:
                user_counts = Counter(msg['user_id'] for msg in group_chat_history[group_id])
                top_users = user_counts.most_common(5)
                msg_lines = ["🪷 近期「綺語」排行榜："]
                for rank, (uid, count) in enumerate(top_users, 1):
                    msg_lines.append(f"第 {rank} 名: {get_user_name(group_id, uid)} ({count} 則訊息)")
                reply_text = "\n".join(msg_lines)

        # 3. 今日廢話王
        elif user_text == "今日廢話王":
            leaderboard = get_custom_leaderboard(group_id, days=1)
            if not leaderboard:
                reply_text = "今日還沒有足夠的對話紀錄喔！"
            else:
                msg_lines = [f"👑 今日廢話王排行榜 ({datetime.now().strftime('%Y-%m-%d')})："]
                for rank, (uid, count) in enumerate(leaderboard, 1):
                    msg_lines.append(f"第 {rank} 名: {get_user_name(group_id, uid)} ({count} 則發言)")
                reply_text = "\n".join(msg_lines)

        # 4. 自訂天數廢話王
        elif re.match(r"^廢話王\s*(\d+)\s*天?$", user_text):
            days = int(re.match(r"^廢話王\s*(\d+)\s*天?$", user_text).group(1))
            leaderboard = get_custom_leaderboard(group_id, days=days)
            if not leaderboard:
                reply_text = f"近 {days} 天還沒有足夠的對話紀錄喔！"
            else:
                msg_lines = [f"👑 近 {days} 天廢話王排行榜："]
                for rank, (uid, count) in enumerate(leaderboard, 1):
                    msg_lines.append(f"第 {rank} 名: {get_user_name(group_id, uid)} ({count} 則發言)")
                reply_text = "\n".join(msg_lines)

        # 5. 搜尋歷史發言
        elif re.match(r"^(搜尋|找|查)\s*(.+)$", user_text):
            keyword = re.match(r"^(搜尋|找|查)\s*(.+)$", user_text).group(2).strip()
            results = search_db_messages(group_id, keyword)
            if not results:
                reply_text = f"🔍 找不到與「{keyword}」相關的歷史留言喔！"
            else:
                msg_lines = [f"🔍 找到與「{keyword}」相關的最新 5 則留言："]
                for uid, msg, created_at in results:
                    msg_lines.append(f"• [{created_at.strftime('%m/%d %H:%M')}] {get_user_name(group_id, uid)}: {msg}")
                reply_text = "\n".join(msg_lines)

        # 6. 分帳助手 - 記帳
        elif re.match(r"^!記帳\s+(\S+)\s+(\d+(\.\d+)?)\s+(.+)$", user_text):
            m = re.match(r"^!記帳\s+(\S+)\s+(\d+(\.\d+)?)\s+(.+)$", user_text)
            p_name, amt, item = m.group(1), float(m.group(2)), m.group(4)
            if add_expense(group_id, p_name, amt, item):
                reply_text = f"✅ 已記錄：{p_name} 付了 ${amt:.0f} ({item})"
            else:
                reply_text = "記帳失敗，請稍後再試。"

        # 7. 分帳助手 - 算帳
        elif user_text in ["!算帳", "!結帳"]:
            reply_text = calculate_expenses(group_id)

        # 8. 分帳助手 - 清空帳目
        elif user_text == "!清空帳目":
            if clear_expenses(group_id):
                reply_text = "🗑️ 群組帳務資料已全部清空！"

        # 9. 美食抽籤 - 新增餐廳
        elif re.match(r"^!新增餐廳\s+(.+)$", user_text):
            r_name = re.match(r"^!新增餐廳\s+(.+)$", user_text).group(1).strip()
            if add_restaurant(group_id, r_name):
                reply_text = f"🍱 已新增餐廳：「{r_name}」到口袋名單！"

        # 10. 美食抽籤 - 抽午餐
        elif user_text in ["!吃什麼", "吃什麼", "抽午餐"]:
            chosen = pick_restaurant(group_id)
            if not chosen:
                reply_text = "口袋名單是空的！請先用 `!新增餐廳 [名稱]` 來新增吧！"
            else:
                prompt = f"請用非常吸引人且幽默的方式，用兩句話介紹並推薦「{chosen}」這家餐廳當今天的午餐或晚餐選擇。"
                try:
                    res = client.models.generate_content(model='gemini-3.5-flash', contents=prompt)
                    reply_text = f"🎲 今天的命定美食是：【{chosen}】！\n\n💡 {res.text}"
                except:
                    reply_text = f"🎲 今天的命定美食是：【{chosen}】！"

        # 11. 黑歷史成語產生器
        elif re.match(r"^!黑歷史(\s+.*)?$", user_text):
            m_target = re.match(r"^!黑歷史(\s+.*)?$", user_text).group(1)
            target_name = m_target.strip() if m_target else get_user_name(group_id, user_id)
            
            # 從 DB 隨機撈 10 則發言
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('SELECT message_text FROM message_logs WHERE group_id = %s ORDER BY RANDOM() LIMIT 10', (group_id,))
                rows = cursor.fetchall()
                cursor.close()
                conn.close()
                
                if not rows:
                    reply_text = "資料庫裡的歷史留言還不夠多，無法煉製成語！"
                else:
                    quotes = "\n".join([f"- {r[0]}" for r in rows])
                    prompt = f"""請根據以下這些群組歷史發言內容，為「{target_name}」創立一個全新的【四字成語】，並給出嚴肅又搞笑的漢語拼音、釋義與典故出處。

歷史發言參考：
{quotes}"""
                    res = client.models.generate_content(model='gemini-3.5-flash', contents=prompt)
                    reply_text = f"📜 【{target_name} 專屬黑歷史成語】\n\n{res.text}"
            except Exception as e:
                reply_text = f"生成黑歷史成語失敗：{e}"

        # 12. 默契大考驗 - 出題
        elif user_text == "!出題":
            prompt = "請設計一題超爆笑、具爭議性或令人糾結的「二選一情境選擇題」（例如：一輩子不洗澡 vs 一輩子不刷牙）。請給出題目與 A、B 兩個選項。"
            try:
                res = client.models.generate_content(model='gemini-3.5-flash', contents=prompt)
                group_games[group_id] = {'question': res.text, 'answers': {}}
                reply_text = f"🎮 【默契大考驗】題目來了！\n\n{res.text}\n\n👉 請成員輸入 `!回答 A` 或 `!回答 B` 來下注！"
            except Exception as e:
                reply_text = f"出題失敗：{e}"

        # 13. 默契大考驗 - 回答
        elif re.match(r"^!回答\s+(.+)$", user_text):
            ans = re.match(r"^!回答\s+(.+)$", user_text).group(1).strip()
            if group_id not in group_games or 'question' not in group_games[group_id]:
                reply_text = "目前沒有進行中的題目，請先輸入 `!出題` 喔！"
            else:
                u_name = get_user_name(group_id, user_id)
                group_games[group_id]['answers'][u_name] = ans
                
                ans_dict = group_games[group_id]['answers']
                if len(ans_dict) < 2:
                    reply_text = f"✅ {u_name} 已選擇【{ans}】！還需要至少 1 位成員輸入 `!回答` 才能計算默契！"
                else:
                    ans_summary = "\n".join([f"• {k}: {v}" for k, v in ans_dict.items()])
                    prompt = f"""以下是群組遊戲「默契大考驗」的成員選擇：
{ans_summary}

請評定這些成員之間的默契指數（0% ~ 100%），並用搞笑、犀利的語氣講評他們的選擇與默契程度！"""
                    try:
                        res = client.models.generate_content(model='gemini-3.5-flash', contents=prompt)
                        reply_text = f"🎯 【默契結算】\n\n成員選擇：\n{ans_summary}\n\n🤖 Gemini 裁判講評：\n{res.text}"
                        del group_games[group_id]  # 清除該局遊戲
                    except Exception as e:
                        reply_text = f"結算失敗：{e}"

        # 14. AI 智能問答
        elif re.match(r"^(!問|@機器人|問[:：])\s*(.+)$", user_text, re.DOTALL):
            user_question = re.match(r"^(!問|@機器人|問[:：])\s*(.+)$", user_text, re.DOTALL).group(2).strip()
            group_chat_history[group_id].append({'user_id': user_id, 'text': user_text})
            log_message_to_db(group_id, user_id, user_text)

            try:
                ask_prompt = f"你是一個樂於助人的 LINE 群組 AI 助手。請用簡明、親切且精準的繁體中文回答以下問題：\n\n{user_question}"
                response = client.models.generate_content(model='gemini-3.5-flash', contents=ask_prompt)
                reply_text = response.text
            except Exception as ai_err:
                reply_text = "抱歉，我現在有點轉不過來，請稍後再試一次！"

        # 15. 一般訊息：紀錄並忽略
        else:
            group_chat_history[group_id].append({'user_id': user_id, 'text': user_text})
            if len(group_chat_history[group_id]) > 200:
                group_chat_history[group_id].pop(0)
            log_message_to_db(group_id, user_id, user_text)

        # 統一回傳
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
