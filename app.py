import os
import re
import json
import threading
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, GroupSource

# ---------------------------------------------------------------------------
# 初始化 Flask 與 LINE Messaging API
# ---------------------------------------------------------------------------
app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 記憶體快取（以群組 ID 為 Key 儲存最近追劇拆解的結果）
group_dramas = {}

# ---------------------------------------------------------------------------
# LINE API 輔助函式
# ---------------------------------------------------------------------------
def reply_to_line(reply_token, text):
    """免費回覆訊息 (使用 Reply Token)"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    replyToken=reply_token,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        print(f"[LINE Reply Error] {e}")

def push_to_line(to_id, text):
    """主動推播訊息至指定群組 (使用 Push Message，每個月前 200 則免費)"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=to_id,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        print(f"[LINE Push Error] {e}")

# ---------------------------------------------------------------------------
# Webhook 進入點
# ---------------------------------------------------------------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# ---------------------------------------------------------------------------
# 文字訊息處理 handler
# ---------------------------------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    source = event.source

    # 取得群組 ID（如果在個人私訊，以 user_id 代替）
    group_id = source.group_id if isinstance(source, GroupSource) else source.user_id

    # -----------------------------------------------------------------------
    # 指令：追劇 / 追劇 500 (方案 1：Async + Push 整合)
    # -----------------------------------------------------------------------
    if re.match(r"^追劇(\s*500)?$", user_text) or user_text == "追劇":
        # 1. 0.5 秒內秒回成員，避免大家以為卡死或重複發送
        reply_to_line(
            event.reply_token,
            "🎬 收到追劇指令！正在為大家拆解近期的熱門討論串，請稍候 10~15 秒... ⏳"
        )

        # 2. 定義背景處理任務
        def process_drama_task(target_group_id):
            try:
                # 抓取資料庫聊天紀錄 (請替換為你的 DB 查詢函式)
                raw_logs = get_history_from_db(target_group_id, limit=500)
                cleaned_context = prepare_context_for_summary(raw_logs, max_chars=4000)

                if not cleaned_context:
                    push_to_line(target_group_id, "過去沒有足夠的實質討論內容可以補追喔！")
                    return

                # Prompt 建立與 Gemini AI 呼叫
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
                
                # 呼叫 Gemini (請替換為你的 AI 生成函式)
                res = safe_generate_content(
                    prompt_contents=prompt, 
                    system_instruction=sys_inst, 
                    temperature=0.2
                )
                
                result_text = res.text.strip()

                # 清理 Markdown JSON 標記
                if result_text.startswith("```json"):
                    result_text = result_text[7:]
                if result_text.startswith("```"):
                    result_text = result_text[3:]
                if result_text.endswith("```"):
                    result_text = result_text[:-3]

                topics_data = json.loads(result_text.strip())

                # 暫存結果至群組快取
                group_dramas[target_group_id] = {"topics": topics_data}

                # 組合追劇目錄文字
                menu_msg = f"📺 【群組追劇目錄】(共拆解出 {len(topics_data)} 個討論串)\n"
                menu_msg += "───────────────────\n"
                for idx, item in enumerate(topics_data, 1):
                    menu_msg += f"🔹 【追劇 {idx}】{item.get('title', '無題')}\n   └ {item.get('summary', '')}\n"
                menu_msg += "───────────────────\n"
                menu_msg += "👉 輸入 `追劇 1`、`追劇 2` 即可觀看該主題的詳細成員對話！"

                # 3. 背景運算完成，發送 Push 推播訊息至群組
                push_to_line(target_group_id, menu_msg)

            except Exception as e:
                print(f"[Drama Pipeline Async Error] {e}")
                push_to_line(target_group_id, "😅 追劇目錄解析失敗，請稍後再試一次！")

        # 啟動 Thread 背景非同步處理
        threading.Thread(target=process_drama_task, args=(group_id,)).start()
        return

    # -----------------------------------------------------------------------
    # 指令：追劇 1, 追劇 2... (觀看個別話題細節)
    # -----------------------------------------------------------------------
    match = re.match(r"^追劇\s*(\d+)$", user_text)
    if match:
        idx = int(match.group(1)) - 1
        drama_data = group_dramas.get(group_id)

        if not drama_data or "topics" not in drama_data:
            reply_to_line(event.reply_token, "⚠️ 目前沒有快取的追劇目錄，請先輸入 `追劇` 產生最新目錄！")
            return

        topics = drama_data["topics"]
        if 0 <= idx < len(topics):
            target_topic = topics[idx]
            detail_msg = f"🎬 【話題 {idx+1}】{target_topic.get('title', '')}\n"
            detail_msg += f"📝 摘要：{target_topic.get('summary', '')}\n"
            detail_msg += "───────────────────\n"
            detail_msg += "\n".join(target_topic.get('dialogues', []))

            # 訊息較短且直接對應使用者的請求，使用免費 reply 回覆
            reply_to_line(event.reply_token, detail_msg)
        else:
            reply_to_line(event.reply_token, f"❌ 找不到第 {idx+1} 個主題，請輸入 `追劇` 重新查看目錄！")
        return

# ---------------------------------------------------------------------------
# 啟動 Server
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
