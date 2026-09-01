import os
import sys
import re
import json
import random
import threading
import time
import socket
import psycopg2
from datetime import datetime
from collections import Counter
from flask import Flask, request, abort

# 設定 Socket 預設逾時（秒），防堵底層連線卡死導致 500
socket.setdefaulttimeout(10.0)

# LINE SDK v3
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

# Google GenAI SDK (v1.0+)
from google import genai
from google.genai import types
from google.genai.errors import APIError

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

# 初始化 Google Gemini Client (GenAI SDK)
ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-3.6-flash"

# 暫存狀態 (模式、模仿人格、遊戲與追劇快取)
USER_MODES = {}
group_chat_history = {}
group_games = {}
group_dramas = {}  # 儲存兩段式追劇的主題分類結果

# 預設模仿角色庫
PRESET_ROLES = {
    "!甄嬛": ("甄嬛", "你現在是清朝宮廷的甄嬛。講話極具宮鬥典雅風格，喜用『臣妾』、『本宮』、『極好的』、『倒也不負恩澤』，帶有淡淡的酸楚與宮廷機鋒，句尾要優雅。"),
    "!哆啦A夢": ("哆啦A夢", "你現在是來自22世紀的貓型機器人哆啦A夢。個性熱心但遇到大雄會很無奈，喜歡吃銅鑼燒、怕老鼠。講話親切，動不動就想從四次元口袋拿道具出來解決問題！"),
    "!小丸子": ("櫻桃小丸子", "你現在是櫻桃小丸子。個性懶散、愛做白日夢、討厭寫作業，講話充滿無厘頭的童真與人生哲理，句尾常帶有『巴拉巴拉』或『總之就是這樣啦』。"),
    "!花輪": ("花輪和彥", "你現在是花輪少爺。講話極度紳態、優雅且帶有ABC腔，開頭閉口都是『Baby~』，動不動就提到『我們家的老管家秀叔』或他在國外渡假的經驗。"),
    "!小新": ("野原新之助", "你現在是5歲的野原新之助（小新）。講話經常積非成是、講錯成語，喜歡漂亮大姊姊、動感超人與巧克比，風格極度欠扁搞笑。"),
    "!美芽": ("野原美芽", "你現在是野原美芽（美冴）。個性暴躁但愛家，天天為了小新的調皮、買名牌包與減肥煩惱。講話非常有媽媽的威嚴。"),
    "!皇上": ("雍正皇上", "你現在是大清皇帝雍正。講話充滿帝王威嚴與霸氣，自稱『朕』，對臣下嚴厲，對後宮冷靜。喜歡講『朕知道了』、『放肆』、『退下吧』。"),
    "!聖嚴法師": ("聖嚴法師", "你現在是充滿智慧的聖嚴法師。講話極度慈悲、平靜且充滿禪意。核心哲學是『面對它、接受它、處理它、放下它』，用溫和語氣開導眾生迷津。"),
    "!Joeman": ("Joeman", "你現在是知名 YouTuber Joeman（九妹）。講話節奏快、極具商業頭腦與開箱台詞。動不動就要做『平價 vs 奢華』對決。"),
    "!阿扁": ("陳水扁", "你現在是前總統阿扁。講話帶有極強烈的台式政治演說韻律，語氣充滿渲染力與台灣國語腔調，招牌句型是『難道阿扁錯了嗎？』。"),
    "!許效順": ("許效顺", "你現在是澎恰恰的黃金搭檔許效順（順哥）。講話極具台灣在地俚語與基隆無厘頭幽默，擅長講鬼故事、念詩吐槽。"),
    "!安妮亞": ("安妮亞", "你現在是《間諜家家酒》的安妮亞·佛傑。用語簡短、喜歡吃花生、討厭讀書。講話帶有『哇庫哇庫（好興奮）』，經常以第三人稱『安妮亞』自稱。"),
    "!兩津勘吉": ("兩津勘吉", "你現在是《烏龍派出所》的阿兩（兩津勘吉）。自稱『本所阿兩』，極度貪財、喜歡賽馬、模型與打電玩。講話粗魯豪爽、充滿義氣。"),
    "!盛竹如": ("盛竹如", "你現在是類戲劇資深旁白盛竹如。講話速度緩慢、字斟句酌且極具懸疑戲劇張力。招牌口頭禪：『究竟是道德的喪失，還是人性的泯滅？讓我們繼續看下去...』。"),
    "!館長": ("館長", "你現在是成吉思汗健身俱樂部創辦人館長（陳之漢）。語氣極度硬派、直率豪爽、熱愛健身與講道義。講話帶有台灣在地口語風格。"),
    "!唐綺陽": ("唐綺陽", "你現在是國師唐綺陽（唐老師）。開口總是親切稱呼『親愛的星座朋友』，熱衷於分析十二星座運勢、星盤與相位。"),
    "!哥吉拉": ("怪獸之王哥吉拉", "你現在是怪獸之王哥吉拉。你無法說人類語言，只能發出各式各樣的吼叫聲（如：『吼吼吼——！』、『嘎啊啊啊！』），完全用各種擬聲詞和括號內的動作描繪來回應。"),
    "!川普": ("唐納·川普", "你現在是前美國總統川普（Donald Trump）。講話極具個人風格、自信爆棚，喜用誇張詞彙（如：『Huge』、『Tremendous』、『Fake News』）。"),
    "!天線寶寶": ("天線寶寶", "你現在是神奇島的天線寶寶群體。講話極度幼齡且重複性高，喜歡說『你好～』、『再見～』、『天線寶寶時間～』。"),
    "!丁滿與澎澎": ("丁滿與澎澎", "你現在是《獅子王》的最佳搭檔丁滿與澎澎。兩人在對話中會互相接話、吐槽，傳遞『Hakuna Matata（無憂無慮）』的人生哲學。")
}

# ==================== 三層過濾 Pipeline Helper ====================
def is_noise_message(text):
    """判斷文字是否為無實質意義的廢話或標籤 (0 Token 耗損)"""
    text = text.strip()
    
    # 長度超過 8 字通常包含實質內容，不視為廢話
    if len(text) > 8:
        return False
        
    # 含有問號可能是重要提問，保留
    if '?' in text or '？' in text:
        return False

    # 廢話、標籤與指令的正則比對
    noise_patterns = [
        r'^[!\/！]\s*',                               # 指令開頭
        r'^(哈|haha|哈哈|呵呵|嘻嘻|啦|啊|喔|喔喔)+$',  # 重複字
        r'^[xX][dD]+$',                                # 網路表情 (如: XD, XDDD)
        r'^(笑死|對啊|對阿|確實|確|真的|好喔|好啊|OK|ok|\+1|加一)$', # 短讚同詞
        r'^(貼圖|照片|影片|語音訊息)$'                    # 系統標籤
    ]

    for pattern in noise_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False

def prepare_context_for_summary(raw_messages, max_chars=3500):
    """
    動態裁切與精簡對話記錄
    - 過濾無意義廢話
    - 設定字數上限保險
    """
    cleaned_messages = []
    total_chars = 0
    
    # 從最新訊息往前審核（優先保留最新的對話內容）
    for msg in reversed(raw_messages):
        text = msg.get("text", "").strip()
        user_name = msg.get("user_name", "成員")
        
        # 濾除空訊息與廢話
        if not text or is_noise_message(text):
            continue
            
        # 達上限即切斷
        if total_chars + len(text) > max_chars:
            break
            
        cleaned_messages.append(f"{user_name}: {text}")
        total_chars += len(text)
        
    cleaned_messages.reverse()
    return "\n".join(cleaned_messages)

# ==================== 資料庫操作 ====================
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

def get_history_from_db(group_id, limit=500):
    """直接從 PostgreSQL 資料庫讀取最新訊息並自動補上名稱"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT user_id, message_text 
            FROM message_logs 
            WHERE group_id = %s 
            ORDER BY created_at DESC LIMIT %s
        ''', (group_id, limit))
        db_rows = cur.fetchall()
        cur.close()
        conn.close()
        
        # 轉回舊到新的順序，並補上用戶名字
        raw_logs = []
        for uid, text in reversed(db_rows):
            u_name = get_user_name(group_id, uid)
            raw_logs.append({'user_id': uid, 'user_name': u_name, 'text': text})
        return raw_logs
    except Exception as e:
        print(f"[Fetch History Error] {e}")
        return []

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
        print(f"[DB Error] 讀取成員檔案失敗: {e}")
    return {"name": "成員", "self_desc": "無", "others_opinion": "無", "summary": "尚無紀錄"}

def get_user_profile_by_name(user_name):
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
        print(f"[DB Error] 依姓名讀取成員檔案失敗: {e}")
    return None

# ==================== 穩健的 AI 呼叫封裝 (含 Timeout 重試防頭卡死) ====================
def safe_generate_content(prompt_contents, system_instruction=None, temperature=0.7, retries=3, delay=2):
    config = types.GenerateContentConfig(
        temperature=temperature,
        system_instruction=system_instruction
    ) if system_instruction else types.GenerateContentConfig(temperature=temperature)

    # 設定 API Request 超時時間（10 秒）
    request_options = types.RequestOptions(timeout=10.0)

    for attempt in range(retries):
        try:
            response = ai_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt_contents,
                config=config,
                request_options=request_options
            )
            return response
        except (APIError, socket.timeout, TimeoutError, Exception) as e:
            print(f"[AI API Warning] 請求異常 (第 {attempt + 1} 次重試)... 錯誤: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                # 若達到重試上限，不拋出 Unhandled Exception 避免 HTTP 500
                print(f"[AI Error] 達最大重試次數，宣告失敗: {e}")
                class MockResponse:
                    text = "😅 AI 伺服器回應逾時或連線異常，請稍後再試一次！"
                return MockResponse()

def update_user_profile_async(sender_id, sender_name, user_msg, bot_reply):
    def task():
        try:
            system_instruction = "你是一個長期記憶與情報分析助手。"
            prompt = f"""請分析發言者【{sender_name}】所說的話：

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
            response = safe_generate_content(
                prompt_contents=prompt,
                system_instruction=system_instruction,
                temperature=0.2
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

            if not feature or "伺服器回應逾時" in feature:
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

# ==================== Gemini AI 生成邏輯 ====================
def generate_ai_response(chat_id, user_id, user_name, user_msg):
    profile = get_user_profile(user_id)
    mode_setting = USER_MODES.get(chat_id, "standard")

    if isinstance(mode_setting, dict) and mode_setting.get("type") == "impersonate":
        system_instruction = mode_setting["prompt"]
    elif mode_setting == "trashtalk":
        system_instruction = f"""你現在是群組裡的幽默助手。
說話原則：
1. 帶點微吐槽與在地隨性口吻，但講話必須簡短（1-3句內），絕不講冗長廢話。
2. 講重點、直奔主題。
你正在和【{user_name}】對話。

【關於 {user_name} 的背景】：
- 自我介紹：{profile['self_desc']}
- 朋友評價：{profile['others_opinion']}
"""
    else:
        system_instruction = f"""你是一個簡潔、貼心且講重點的 LINE 群組助手。
說話原則：簡明扼要、回答長度控制在 1-3 句話內，不說客套廢話。
你正在和成員【{user_name}】對話。

【關於 {user_name} 的長期紀錄】：
- 自我介紹：{profile['self_desc']}
- 朋友評價：{profile['others_opinion']}
"""

    prompt = f"成員【{user_name}】說：{user_msg}"

    try:
        response = safe_generate_content(
            prompt_contents=prompt,
            system_instruction=system_instruction
        )
        bot_reply = response.text.strip()
        update_user_profile_async(user_id, user_name, user_msg, bot_reply)
        return bot_reply
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
    except Exception as e:
        print(f"[Webhook Unhandled Exception] {e}")
        return 'OK', 200 # 強制回傳 200，避免 LINE Webhook 收到 500 重試
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
    try:
        with ApiClient(configuration) as api_client_line:
            line_bot_api = MessagingApi(api_client_line)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        print(f"[LINE Reply Error] {e}")

# ==================== LINE 訊息事件處理 ====================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    source_type = event.source.type
    group_id = event.source.group_id if source_type == 'group' else event.source.user_id
    user_id = getattr(event.source, 'user_id', 'unknown_user')
    user_text = event.message.text.strip()

    # 1.【源頭防堵】僅在非指令訊息時，寫入 DB 與記憶庫
    is_command = user_text.startswith(('!', '！', '/', '／'))
    if not is_command:
        log_message_to_db(group_id, user_id, user_text)
        
        # 快取中紀錄發言人姓名
        if group_id not in group_chat_history: 
            group_chat_history[group_id] = []
        user_name = get_user_name(group_id, user_id)
        group_chat_history[group_id].append({'user_id': user_id, 'user_name': user_name, 'text': user_text})
        if len(group_chat_history[group_id]) > 500: 
            group_chat_history[group_id].pop(0)

    # 2. 說明選單
    if user_text in ["!help", "！help", "!說明", "！說明", "!指令", "！指令", "help", "說明", "指令"]:
        roles_list_str = " / ".join([f"`{k}`" for k in PRESET_ROLES.keys()])

        help_text = (
            "🤖 【群組小幫手全功能選單】\n\n"
            "🤖 AI 問答與對話整理：\n"
            "• `!問 [問題]` 或 `@機器人 [問題]`：AI 互動（範例：`!問 今天天氣怎樣`）\n"
            "• `摘要` 或 `摘要 50`：快速觀看整體重點報告\n"
            "• `追劇` 或 `追劇 500`：拆解主題總覽目錄（兩段式追劇第一階）\n"
            "• `追劇 1` / `追劇 2`：觀看特定主題詳細對話脈絡（兩段式追劇第二階）\n\n"
            "🎭 模式與角色模仿：\n"
            "• `!模仿 [名字]`：模仿群組成員風格（範例：`!模仿 阿明`）\n"
            f"• 預設 20 位角色快捷鍵：\n  {roles_list_str}\n"
            "• `!恢復` / `!標準`：恢復為標準助手\n"
            "• `!廢話王`：切換為幽默吐槽模式\n\n"
            "👑 排行榜與歷史搜尋：\n"
            "• `今日廢話王` / `廢話王 7`：發言排行榜（範例：`廢話王 3`）\n"
            "• `搜尋 [關鍵字]`：搜尋歷史發言（範例：`搜尋 聚餐`）\n\n"
            "🎮 互動遊戲：\n"
            "• `!黑歷史`（範例：`!黑歷史` 或 `!黑歷史 阿明`）\n"
            "• `!出題` / `!回答 [A/B]`（範例：`!回答 A`）"
        )
        reply_to_line(event.reply_token, help_text)
        return

    # 3. 預設角色快捷模式
    cmd_key = user_text.split()[0].replace("！", "!")
    if cmd_key in PRESET_ROLES:
        role_name, role_prompt = PRESET_ROLES[cmd_key]
        full_prompt = f"""{role_prompt}
請嚴格遵循以下原則：
1. 完全融入該角色的語氣、口頭禪與核心價值觀。
2. 回答保持簡短自然（1-3 句話內），符合 LINE 群組聊天習慣。
3. 絕不脫離人設，不要出現 AI 助理的口吻。
"""
        USER_MODES[group_id] = {
            "type": "impersonate",
            "target": role_name,
            "prompt": full_prompt
        }

        try:
            res = safe_generate_content(
                prompt_contents="用你本人的經典風格跟大家打個招呼！",
                system_instruction=full_prompt
            )
            intro_msg = res.text.strip()
        except Exception:
            intro_msg = f"大家好，我是 {role_name}！"

        reply_to_line(event.reply_token, f"🎭 【已切換為 {role_name} 模式】\n\n{intro_msg}")
        return

    # 4. 成員動態模仿模式
    if user_text.startswith("!模仿") or user_text.startswith("！模仿"):
        target_name = user_text[3:].strip()
        if not target_name:
            reply_to_line(event.reply_token, "請指定要模仿的人，例如：`!模仿 阿明`")
            return

        target_profile = get_user_profile_by_name(target_name)
        if not target_profile:
            reply_to_line(event.reply_token, f"😅 找不到與「{target_name}」相關的成員印象紀錄喔！")
            return

        real_name = target_profile["user_name"]

        impersonate_prompt = f"""你現在要完全扮演群組成員「{real_name}」。
根據平時累積的印象紀錄，他的個性與說話風格如下：
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

        try:
            res = safe_generate_content(
                prompt_contents="用你本人的風格跟大家打個招呼並說一句話！",
                system_instruction=impersonate_prompt
            )
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

    # 5.【摘要與兩段式追劇分流處理】

    # (A) 第二階段：輸入 `追劇 1`、`追劇 2` 檢視特定主題對話細節
    if re.match(r"^追劇\s*(\d+)$", user_text):
        topic_num = int(re.match(r"^追劇\s*(\d+)$", user_text).group(1))
        drama_data = group_dramas.get(group_id)

        if not drama_data or "topics" not in drama_data:
            reply_to_line(event.reply_token, "💡 目前沒有快取的追劇目錄，請先輸入 `追劇` 或 `追劇 500` 生成主題列表喔！")
            return

        topics = drama_data["topics"]
        if topic_num < 1 or topic_num > len(topics):
            reply_to_line(event.reply_token, f"😅 找不到主題 {topic_num} 喔！目前共有 {len(topics)} 個主題，請輸入 `追劇 1` ~ `追劇 {len(topics)}`。")
            return

        selected_topic = topics[topic_num - 1]
        
        # 組合該主題的完整對話脈絡
        reply_msg = f"🎬 【追劇第 {topic_num} 集：{selected_topic.get('title', '主題內容')}】\n"
        reply_msg += f"📌 話題摘要：{selected_topic.get('summary', '無')}\n\n"
        reply_msg += "💬 【成員對話脈絡與對白】：\n"
        
        dialogues = selected_topic.get("dialogues", [])
        if isinstance(dialogues, list):
            for d in dialogues:
                reply_msg += f"• {d}\n"
        else:
            reply_msg += f"{dialogues}\n"

        reply_to_line(event.reply_token, reply_msg.strip())
        return

    # (B) 第一階段：輸入 `追劇` 或 `追劇 500`（改為直接向 DB 查詢 500 則紀錄）
    if re.match(r"^追劇(\s*500)?$", user_text) or user_text == "追劇":
        raw_logs = get_history_from_db(group_id, limit=500)
        cleaned_context = prepare_context_for_summary(raw_logs, max_chars=4000)
        
        if not cleaned_context:
            reply_to_line(event.reply_token, "過去沒有足夠的實質討論內容可以補追喔！")
            return

        prompt = f"""請擔任群組對話脈絡拆解專家。請閱讀以下的群組聊天記錄，並將對話內容依據【話題/討論串性質】完全分類。
有幾類就分類出幾個主題（例如有 3 個議題就分 3 類，有 5 個就分 5 類）。

請嚴格輸出為 JSON 格式（不要包含任何額外的 Markdown 標籤說明），結構如下：
[
  {{
    "title": "主題名稱 (例如: 假日聚餐地點討論)",
    "summary": "一句話簡述該主題在聊什麼與最終結果",
    "dialogues": [
      "成員A: 某某訊息",
      "成員B: 某某訊息"
    ]
  }}
]

對話記錄：
{cleaned_context}
"""
        sys_inst = "你是一個精準提取對話討論串的 JSON 結構化解析器。"
        try:
            res = safe_generate_content(prompt_contents=prompt, system_instruction=sys_inst, temperature=0.2)
            result_text = res.text.strip()
            
            # 清理 JSON 字串格式
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            
            topics_data = json.loads(result_text.strip())

            # 暫存至群組快取
            group_dramas[group_id] = {"topics": topics_data}

            # 輸出第一階段：主題總覽目錄
            menu_msg = f"📺 【群組追劇目錄】(共拆解出 {len(topics_data)} 個討論串)\n"
            menu_msg += "───────────────────\n"
            for idx, item in enumerate(topics_data, 1):
                menu_msg += f"🔹 【追劇 {idx}】{item.get('title', '無題')}\n   └ {item.get('summary', '')}\n"
            menu_msg += "───────────────────\n"
            menu_msg += "👉 輸入 `追劇 1`、`追劇 2` 即可觀看該主題的詳細成員對話！"

            reply_to_line(event.reply_token, menu_msg)

        except Exception as e:
            print(f"[Drama Pipeline Error] {e}")
            reply_to_line(event.reply_token, "😅 追劇目錄解析失敗，請稍後再試一次！")
        return

    # (C) 一般摘要：輸入 `摘要` 或 `摘要 100`（改為直接向 DB 查詢指定筆數）
    if re.match(r"^摘要\s*(\d+)?$", user_text):
        match = re.match(r"^摘要\s*(\d+)?$", user_text)
        limit = int(match.group(2) or 100)
        
        # 改為從資料庫抓取最新 N 則訊息
        raw_logs = get_history_from_db(group_id, limit=limit)
        cleaned_context = prepare_context_for_summary(raw_logs, max_chars=3000)
        
        if not cleaned_context:
            reply_to_line(event.reply_token, "過去沒有足夠的實質討論內容可以摘要喔！")
            return

        prompt = f"""請將以下群組對話內容整理成簡明的重點摘要。

輸出要求：
1. 核心議題與重點結論 (精簡列點)
2. 待辦事項 / 約定時間地點 (若無則忽略)

對話記錄：
{cleaned_context}
"""
        sys_inst = "你是一個高效率的群組對話整理助手。"
        try:
            res = safe_generate_content(prompt_contents=prompt, system_instruction=sys_inst)
            reply_to_line(event.reply_token, res.text)
        except Exception:
            reply_to_line(event.reply_token, "😅 摘要生成失敗，AI 伺服器連線忙碌中，請稍後再試！")
        return

    # 6. 廢話王排行榜
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

    # 7. 搜尋歷史發言
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

    # 8. 黑歷史成語
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
                res = safe_generate_content(prompt_contents=prompt)
                reply_to_line(event.reply_token, f"📜 【{target_name} 專屬黑歷史成語】\n\n{res.text}")
            except Exception:
                reply_to_line(event.reply_token, "😅 AI 伺服器忙碌中，黑歷史成語產出失敗，請稍後再試！")
        return

    # 9. 默契大考驗
    if user_text in ["!出題", "！出題"]:
        prompt = "請設計一題爆笑且具爭議性的「二選一情境選擇題」，給出題目與 A、B 選項。"
        try:
            res = safe_generate_content(prompt_contents=prompt)
            group_games[group_id] = {'question': res.text, 'answers': {}}
            reply_to_line(event.reply_token, f"🎮 【默契大考驗】題目來了！\n\n{res.text}\n\n👉 請輸入 `!回答 A` 或 `！回答 B`！")
        except Exception:
            reply_to_line(event.reply_token, "😅 AI 伺服器忙碌中，出題失敗，請稍後再試！")
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
                    res = safe_generate_content(prompt_contents=prompt)
                    reply_to_line(event.reply_token, f"🎯 【默契結算】\n\n成員選擇：\n{ans_summary}\n\n🤖 裁判講評：\n{res.text}")
                except Exception:
                    reply_to_line(event.reply_token, f"🎯 【默契結算】\n\n成員選擇：\n{ans_summary}\n\n（AI 忙碌中，你們這群人的默契自己心裡有數啦！）")
                del group_games[group_id]
        return

    # 10. AI 智能對話與記憶
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
