import os
import sys
import re
import random
import threading
import time
import psycopg2
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
from google.genai.errors import ServerError

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

# 暫存狀態 (模式、模仿人格與遊戲)
USER_MODES = {}
group_chat_history = {}
group_games = {}

# ==================== PostgreSQL 資料庫操作 ====================
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def log_message_to_db(group_id, user_id, message_text):
    """背景寫入每一筆訊息，用於排行榜與搜尋紀錄"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO message_logs (group_id, user_id, message_text) VALUES (%s, %s, %s)',
            (group_id, user_id, message_text)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[DB Log Error] {e}")

def get_user_profile(user_id):
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
    """依名稱模糊比對讀取使用者特徵資料"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, user_name, self_description, others_opinion, profile_summary 
            FROM user_profiles WHERE user_name ILIKE %s LIMIT 1;
        """, (f"%{user_name}%",))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {
                "user_id": row[0],
                "user_name": row[1],
                "self_desc": row[2] or "無",
                "others_opinion": row[3] or "無",
                "summary": row[4] or "尚無紀錄"
            }
    except Exception as e:
        print(f"[DB Error] 依姓名讀取 Profile 失敗: {e}")
    return None

# ==================== 穩健的 AI 呼叫封裝（含 503 自動重試） ====================
def safe_generate_content(contents, retries=3, delay=2):
    """專門呼叫 gemini-3.6-flash，遇 503 過載會自動重試"""
    for attempt in range(retries):
        try:
            return ai_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=contents
            )
        except ServerError as e:
            print(f"[AI 503 Warning] 伺服器忙碌中，正在進行第 {attempt + 1} 次重試... 錯誤: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise e
        except Exception as e:
            print(f"[AI Error] {e}")
            raise e

def update_user_profile_async(sender_id, sender_name, user_msg, bot_reply):
    """記憶庫非同步分類與更新"""
    def task():
        try:
            prompt = f"""
你是一個長期記憶與情報分析助手。請分析發言者【{sender_name}】所說的話：

【發言者】：{sender_name}
【對話內容】：{user_msg}

任務：
1. 判斷這段話主要是在描述誰？（寫出名字，若是發言者自己請寫 "{sender_name}"）。
2. 判斷這段話的性質：
   - 如果是「{sender_name}」在講【自己】的事，標註 TYPE: SELF
   - 如果是「{sender_name}」在評價/描述【別人】，標註 TYPE: OTHERS
3. 提取出最新的特徵短句（20字以內精簡重點）。

請嚴格按格式輸出：
TARGET_NAME: 目標姓名
TYPE: [SELF 或 OTHERS]
FEATURE: 提煉出的特徵重點
"""
            response = safe_generate_content(contents=prompt)
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
                new_self = f"{target_profile['self_desc']}; {feature}".strip("; ")
                new_summary = f"【自我陳述】：{new_self}\n【朋友印象】：{target_profile['others_opinion']}"
                cur.execute("""
                    INSERT INTO user_profiles (user_id, user_name, self_description, profile_summary, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) 
                    DO UPDATE SET user_name = EXCLUDED.user_name, self_description = EXCLUDED.self_description, profile_summary = EXCLUDED.profile_summary, updated_at = NOW();
                """, (target_id, target_name, new_self, new_summary))
            else:
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
        except Exception as e:
            print(f"[Memory Async Error] {e}")

    threading.Thread(target=task).start()

# --- 廢話王排行榜與搜尋 ---
def get_custom_leaderboard(group_id, days=1):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = f'''
            SELECT user_id, COUNT(*) as msg_count 
            FROM message_logs 
            WHERE group_id = %s AND created_at >= NOW() - INTERVAL '{days} days'
            GROUP BY user_id ORDER BY msg_count DESC LIMIT 5
        '''
        cur.execute(query, (group_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"[Leaderboard Error] {e}")
        return []

def search_db_messages(group_id, keyword, limit=5):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = '''
            SELECT user_id, message_text, created_at 
            FROM message_logs 
            WHERE group_id = %s AND message_text LIKE %s 
            ORDER BY created_at DESC LIMIT %s
        '''
        cur.execute(query, (group_id, f'%{keyword}%', limit))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"[Search Error] {e}")
        return []

# --- 記帳功能 ---
def add_expense(group_id, payer_name, amount, item_name):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO expenses (group_id, payer_name, amount, item_name) VALUES (%s, %s, %s, %s)', (group_id, payer_name, amount, item_name))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[Expense Error] {e}")
        return False

def calculate_expenses(group_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT payer_name, amount, item_name FROM expenses WHERE group_id = %s', (group_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows: return "目前沒有任何記帳紀錄喔！"

        total_amount = sum(row[1] for row in rows)
        payers = set(row[0] for row in rows)
        person_count = len(payers)
        avg_amount = total_amount / person_count if person_count > 0 else 0

        balances = {p: 0.0 for p in payers}
        details = []
        for p, amt, item in rows:
            balances[p] += float(amt)
            details.append(f"• {p} 付了 ${amt:.0f} ({item})")

        for p in balances: balances[p] -= float(avg_amount)

        debtors, creditors = [], []
        for p, bal in balances.items():
            if bal < -0.01: debtors.append([p, -bal])
            elif bal > 0.01: creditors.append([p, bal])

        transfers = []
        i, j = 0, 0
        while i < len(debtors) and j < len(creditors):
            debtor, d_amount = debtors[i]
            creditor, c_amount = creditors[j]
            settle_amount = min(d_amount, c_amount)
            transfers.append(f"👉 {debtor} 應給 {creditor} ${settle_amount:.0f}")
            debtors[i][1] -= settle_amount
            creditors[j][1] -= settle_amount
            if debtors[i][1] < 0.01: i += 1
            if creditors[j][1] < 0.01: j += 1

        res = [f"💰 【群組結帳統計】", f"總花費：${total_amount:.0f}", f"參與人數：{person_count} 人 (每人均分：${avg_amount:.0f})", "\n【消費明細】"] + details + ["\n【最佳平帳方案】"] + (transfers if transfers else ["大家費用剛剛好，不用互相轉帳！"])
        return "\n".join(res)
    except Exception as e:
        return "結帳計算失敗，請稍後再試。"

def clear_expenses(group_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM expenses WHERE group_id = %s', (group_id,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        return False

# --- 美食抽籤 ---
def add_restaurant(group_id, name):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO restaurants (group_id, restaurant_name) VALUES (%s, %s)', (group_id, name))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        return False

def pick_restaurant(group_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT restaurant_name FROM restaurants WHERE group_id = %s', (group_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return random.choice(rows)[0] if rows else None
    except Exception as e:
        return None

# ==================== Gemini AI 生成邏輯 ====================
def generate_ai_response(chat_id, user_id, user_name, user_msg):
    profile = get_user_profile(user_id)
    mode_setting = USER_MODES.get(chat_id, "standard")

    # 判斷是否為「模仿模式」或「動態指令模式」
    if isinstance(mode_setting, dict) and mode_setting.get("type") == "impersonate":
        system_instruction = mode_setting["prompt"]
    elif mode_setting == "trashtalk":
        # 收斂後的廢話王風格：簡短、微吐槽、講重點，不浮誇
        system_instruction = f"""
你現在是群組裡的幽默助手。
說話原則：
1. 帶點微吐槽與在地隨性口吻，但講話必須簡短（1-3句內），絕不講冗長廢話。
2. 講重點、直奔主題。
你正在和【{user_name}】對話。

【關於 {user_name} 的背景】：
- 自我介紹：{profile['self_desc']}
- 朋友評價：{profile['others_opinion']}
"""
    else:
        system_instruction = f"""
你是一個簡潔、貼心且講重點的 LINE 群組助手。
說話原則：簡明扼要、回答長度控制在 1-3 句話內，不說客套廢話。
你正在和成員【{user_name}】對話。

【關於 {user_name} 的長期紀錄】：
- 自我介紹：{profile['self_desc']}
- 朋友評價：{profile['others_opinion']}
"""

    prompt = f"成員【{user_name}】說：{user_msg}"
    
    try:
        response = safe_generate_content(contents=[system_instruction, prompt])
        bot_reply = response.text.strip()
        update_user_profile_async(user_id, user_name, user_msg, bot_reply)
        return bot_reply
    except ServerError:
        return "😅 AI 伺服器目前流量過大正在塞車（503），請稍後再試一次！"
    except Exception as e:
        print(f"[AI Gen Error] {e}")
        return "⚠️ 系統發生了一點小狀況，請稍後再試！"

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
        abort(400)
    return 'OK'

def get_user_name(group_id, user_id):
    if user_id == 'unknown_user': return "未知用戶"
    try:
        with ApiClient(configuration) as api_client_line:
            line_bot_api = MessagingApi(api_client_line)
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
            return profile.display_name
    except Exception:
        return f"用戶({user_id[:6]})"

def reply_to_line(reply_token, text):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

# ==================== LINE 訊息事件處理 ====================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    source_type = event.source.type
    group_id = event.source.group_id if source_type == 'group' else event.source.user_id
    user_id = getattr(event.source, 'user_id', 'unknown_user')
    user_text = event.message.text.strip()

    # 1. 默默背景寫入紀錄（每一條訊息都統計）
    log_message_to_db(group_id, user_id, user_text)
    if group_id not in group_chat_history: group_chat_history[group_id] = []
    group_chat_history[group_id].append({'user_id': user_id, 'text': user_text})
    if len(group_chat_history[group_id]) > 200: group_chat_history[group_id].pop(0)

    # 2. 說明選單 (!help / ！help)
    if user_text in ["!help", "！help", "!說明", "！說明", "!指令", "！指令", "help", "說明", "指令"]:
        help_text = (
            "🤖 【群組小幫手全功能選單】\n\n"
            "🤖 AI 問答與對話整理：\n"
            "• `!問 [問題]` 或 `@機器人 [問題]`：AI 互動\n"
            "• `摘要` 或 `摘要 50`：整理近期對話重點\n\n"
            "🎭 模式與模仿切換：\n"
            "• `!模仿 [名字]`：讓 AI 完全扮演資料庫中的成員\n"
            "• `!恢復` / `!標準`：恢復為標準助手\n"
            "• `!廢話王`：切換為幽默吐槽模式\n\n"
            "👑 排行榜與歷史搜尋：\n"
            "• `今日廢話王` / `廢話王 7`：發言排行榜\n"
            "• `搜尋 [關鍵字]`：搜尋歷史發言\n\n"
            "🍱 美食抽籤：`!新增餐廳 [名稱]` / `!吃什麼`\n"
            "💰 群組記帳：`!記帳 [名字] [金額] [品項]` / `!算帳` / `!清空帳目`\n"
            "🎮 互動遊戲：`!黑歷史` / `!出題` / `!回答 [A/B]`"
        )
        reply_to_line(event.reply_token, help_text)
        return

    # 3. 模式切換與模仿 (!模仿 / !廢話王 / !標準 / !恢復)
    if user_text.startswith("!模仿") or user_text.startswith("！模仿"):
        target_name = user_text[3:].strip()
        if not target_name:
            reply_to_line(event.reply_token, "請指定要模仿的人，例如：`!模仿 小明`")
            return
        
        target_profile = get_user_profile_by_name(target_name)
        if not target_profile:
            reply_to_line(event.reply_token, f"😅 資料庫裡找不到與「{target_name}」相關的特徵紀錄喔！")
            return

        real_name = target_profile["user_name"]
        
        # 組裝動態模仿 Prompt
        impersonate_prompt = f"""
你現在要完全扮演群組成員「{real_name}」。
根據資料庫記錄，他的個性與說話風格如下：
- 自我陳述：{target_profile['self_desc']}
- 朋友評價：{target_profile['others_opinion']}
- 綜合特徵：{target_profile['summary']}

請遵循原則：
1. 用他的口吻、習慣用語與邏輯回答問題。
2. 回答保持簡短自然（1-3 句話內），像本人在 LINE 打字。
3. 絕不要使用官腔或機器人式的用語。
"""
        USER_MODES[group_id] = {
            "type": "impersonate",
            "target": real_name,
            "prompt": impersonate_prompt
        }

        # 登場台詞
        try:
            res = safe_generate_content(contents=[impersonate_prompt, "用你本人的風格跟大家打個招呼並說一句話！"])
            intro_msg = res.text.strip()
        except Exception:
            intro_msg = f"大家好，我是 {real_name}！"

        reply_to_line(event.reply_token, f"🎭 【已切換為 {real_name} 模式】\n\n{intro_msg}")
        return

    elif user_text in ["!廢話王", "！廢話王"]:
        USER_MODES[group_id] = "trashtalk"
        reply_to_line(event.reply_token, "🤪 已切換為幽默吐槽模式！講重點不廢話。")
        return

    elif user_text in ["!標準", "！標準", "!恢復", "！恢復", "!重置", "！重置"]:
        USER_MODES[group_id] = "standard"
        reply_to_line(event.reply_token, "🤖 已恢復為標準貼心小幫手模式！")
        return

    # 4. 對話摘要
    if re.match(r"^摘要\s*(\d+)?$", user_text):
        limit = int(re.match(r"^摘要\s*(\d+)?$", user_text).group(1) or 100)
        logs = [f"[{get_user_name(group_id, m['user_id'])}]: {m['text']}" for m in group_chat_history[group_id][-limit:]]
        prompt = f"你是一個高效率的群組對話整理助手。請分類並整理重點與待辦：\n\n" + "\n".join(logs)
        try:
            res = safe_generate_content(contents=prompt)
            reply_to_line(event.reply_token, res.text)
        except Exception:
            reply_to_line(event.reply_token, "😅 摘要生成失敗，AI 伺服器忙碌中（503），請稍後再試！")
        return

    # 5. 廢話王排行榜
    if "廢話王" in user_text and ("今日" in user_text or "@" in user_text or user_text == "廢話王"):
        leaderboard = get_custom_leaderboard(group_id, days=1)
        if not leaderboard:
            reply_to_line(event.reply_token, "今日還沒有足夠的對話紀錄喔！")
        else:
            msg_lines = [f"👑 今日廢話王排行榜 ({datetime.now().strftime('%Y-%m-%d')})："]
            for rank, (uid, count) in enumerate(leaderboard, 1):
                msg_lines.append(f"第 {rank} 名: {get_user_name(group_id, uid)} ({count} 則發言)")
            reply_to_line(event.reply_token, "\n".join(msg_lines))
        return

    if re.match(r"^廢話王\s*(\d+)\s*天?$", user_text):
        days = int(re.match(r"^廢話王\s*(\d+)\s*天?$", user_text).group(1))
        leaderboard = get_custom_leaderboard(group_id, days=days)
        if not leaderboard:
            reply_to_line(event.reply_token, f"近 {days} 天還沒有足夠的紀錄喔！")
        else:
            msg_lines = [f"👑 近 {days} 天廢話王排行榜："]
            for rank, (uid, count) in enumerate(leaderboard, 1):
                msg_lines.append(f"第 {rank} 名: {get_user_name(group_id, uid)} ({count} 則發言)")
            reply_to_line(event.reply_token, "\n".join(msg_lines))
        return

    # 6. 搜尋歷史發言
    if re.match(r"^(搜尋|找|查)\s*(.+)$", user_text):
        keyword = re.match(r"^(搜尋|找|查)\s*(.+)$", user_text).group(2).strip()
        results = search_db_messages(group_id, keyword)
        if not results:
            reply_to_line(event.reply_token, f"🔍 找不到與「{keyword}」相關的歷史留言喔！")
        else:
            msg_lines = [f"🔍 找到與「{keyword}」相關的最新 5 則留言："]
            for uid, msg, created_at in results:
                msg_lines.append(f"• [{created_at.strftime('%m/%d %H:%M')}] {get_user_name(group_id, uid)}: {msg}")
            reply_to_line(event.reply_token, "\n".join(msg_lines))
        return

    # 7. 分帳助手 (!記帳 / ！記帳)
    if re.match(r"^[!！]記帳\s+(\S+)\s+(\d+(\.\d+)?)\s+(.+)$", user_text):
        m = re.match(r"^[!！]記帳\s+(\S+)\s+(\d+(\.\d+)?)\s+(.+)$", user_text)
        if add_expense(group_id, m.group(1), float(m.group(2)), m.group(4)):
            reply_to_line(event.reply_token, f"✅ 已記錄：{m.group(1)} 付了 ${float(m.group(2)):.0f} ({m.group(4)})")
        return
    elif user_text in ["!算帳", "！算帳", "!結帳", "！結帳"]:
        reply_to_line(event.reply_token, calculate_expenses(group_id))
        return
    elif user_text in ["!清空帳目", "！清空帳目"]:
        if clear_expenses(group_id): reply_to_line(event.reply_token, "🗑️ 群組帳務資料已全部清空！")
        return

    # 8. 美食抽籤 (!新增餐廳 / ！新增餐廳)
    if re.match(r"^[!！]新增餐廳\s+(.+)$", user_text):
        r_name = re.match(r"^[!！]新增餐廳\s+(.+)$", user_text).group(1).strip()
        if add_restaurant(group_id, r_name): reply_to_line(event.reply_token, f"🍱 已新增餐廳：「{r_name}」到口袋名單！")
        return
    elif user_text in ["!吃什麼", "！吃什麼", "吃什麼", "抽午餐"]:
        chosen = pick_restaurant(group_id)
        if not chosen:
            reply_to_line(event.reply_token, "口袋名單是空的！請先用 `!新增餐廳 [名稱]` 新增吧！")
        else:
            prompt = f"用幽默方式兩句話介紹「{chosen}」當今天午餐或晚餐選擇。"
            try:
                res = safe_generate_content(contents=prompt)
                reply_to_line(event.reply_token, f"🎲 今天的命定美食是：【{chosen}】！\n\n💡 {res.text}")
            except Exception:
                reply_to_line(event.reply_token, f"🎲 今天的命定美食是：【{chosen}】！\n（AI 忙碌中，先讓你選這一家！）")
        return

    # 9. 黑歷史成語 (!黑歷史 / ！黑歷史)
    if re.match(r"^[!！]黑歷史(\s+.*)?$", user_text):
        m_target = re.match(r"^[!！]黑歷史(\s+.*)?$", user_text).group(1)
        target_name = m_target.strip() if m_target else get_user_name(group_id, user_id)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT message_text FROM message_logs WHERE group_id = %s ORDER BY RANDOM() LIMIT 10', (group_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            reply_to_line(event.reply_token, "歷史發言還不夠多，無法煉製成語！")
        else:
            quotes = "\n".join([f"- {r[0]}" for r in rows])
            prompt = f"請根據以下歷史發言為「{target_name}」創立一個全新的【四字成語】，給出漢語拼音、釋義與典故：\n{quotes}"
            try:
                res = safe_generate_content(contents=prompt)
                reply_to_line(event.reply_token, f"📜 【{target_name} 專屬黑歷史成語】\n\n{res.text}")
            except Exception:
                reply_to_line(event.reply_token, "😅 AI 伺服器忙碌中（503），黑歷史成語產出失敗，請稍後再試！")
        return

    # 10. 默契大考驗 (!出題 / ！出題)
    if user_text in ["!出題", "！出題"]:
        prompt = "請設計一題爆笑且具爭議性的「二選一情境選擇題」，給出題目與 A、B 選項。"
        try:
            res = safe_generate_content(contents=prompt)
            group_games[group_id] = {'question': res.text, 'answers': {}}
            reply_to_line(event.reply_token, f"🎮 【默契大考驗】題目來了！\n\n{res.text}\n\n👉 請輸入 `!回答 A` 或 `！回答 B`！")
        except Exception:
            reply_to_line(event.reply_token, "😅 AI 伺服器忙碌中（503），出題失敗，請稍後再試！")
        return
    elif re.match(r"^[!！]回答\s+(.+)$", user_text):
        ans = re.match(r"^[!！]回答\s+(.+)$", user_text).group(1).strip()
        if group_id not in group_games or 'question' not in group_games[group_id]:
            reply_to_line(event.reply_token, "目前沒有進行中的題目，請先輸入 `!出題` 喔！")
        else:
            u_name = get_user_name(group_id, user_id)
            group_games[group_id]['answers'][u_name] = ans
            ans_dict = group_games[group_id]['answers']
            if len(ans_dict) < 2:
                reply_to_line(event.reply_token, f"✅ {u_name} 已選擇【{ans}】！還需要至少 1 位成員輸入 `!回答`！")
            else:
                ans_summary = "\n".join([f"• {k}: {v}" for k, v in ans_dict.items()])
                prompt = f"評定以下默契指數 (0%~100%) 與講評：\n{ans_summary}"
                try:
                    res = safe_generate_content(contents=prompt)
                    reply_to_line(event.reply_token, f"🎯 【默契結算】\n\n成員選擇：\n{ans_summary}\n\n🤖 裁判講評：\n{res.text}")
                except Exception:
                    reply_to_line(event.reply_token, f"🎯 【默契結算】\n\n成員選擇：\n{ans_summary}\n\n（AI 忙碌中，你們這群人的默契自己心裡有數啦！）")
                del group_games[group_id]
        return

    # 11. AI 智能對話與記憶（群組過濾：必須有 !問/！問 或 提及機器人mention 才會回應）
    is_group = (source_type == "group")
    is_cmd = user_text.startswith("!問") or user_text.startswith("！問") or user_text.startswith("!") or user_text.startswith("！")

    is_mentioned = False
    if hasattr(event.message, 'mention') and event.message.mention:
        is_mentioned = True  

    if is_group and not (is_cmd or is_mentioned):
        return  

    clean_msg = user_text
    if clean_msg.startswith("!問") or clean_msg.startswith("！問"): 
        clean_msg = clean_msg[2:].strip()
    elif clean_msg.startswith("!") or clean_msg.startswith("！"): 
        clean_msg = clean_msg[1:].strip()
    
    if not clean_msg: 
        clean_msg = user_text

    user_name = get_user_name(group_id, user_id)
    reply_text = generate_ai_response(group_id, user_id, user_name, clean_msg)
    reply_to_line(event.reply_token, reply_text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
