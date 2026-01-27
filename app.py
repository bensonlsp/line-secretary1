import os
import base64
import json
from datetime import datetime
from flask import Flask, request, abort
from dotenv import load_dotenv
from openai import OpenAI
from notion_client import Client as NotionClient
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, AudioMessageContent
from linebot.v3.exceptions import InvalidSignatureError

load_dotenv()

app = Flask(__name__)

configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
openai_client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)
notion_client = NotionClient(auth=os.getenv("NOTION_TOKEN"))
notion_database_id = os.getenv("NOTION_DATABASE_ID")


def correct_cantonese_text(text: str) -> str:
    """Use AI to correct and refine Cantonese transcription."""
    response = openai_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {
                "role": "user",
                "content": f"""請校正以下廣東話轉錄文字，確保使用正確的廣東話用字。

常見校正規則：
- 「的」→「嘅」
- 「他/她」→「佢」
- 「他們/她們」→「佢哋」
- 「我們」→「我哋」
- 「你們」→「你哋」
- 「沒有」→「冇」
- 「不」→「唔」
- 「是」→「係」
- 「這/這個」→「呢個」
- 「那/那個」→「嗰個」
- 「什麼」→「咩」/「乜嘢」
- 「東西」→「嘢」
- 「了」→「咗」（完成式）
- 「著」→「緊」（進行式）
- 「給」→「畀」
- 「看」→「睇」
- 「說」→「講」
- 「想」→「諗」
- 「知道」→「知」
- 「回去」→「返去」
- 「過來」→「過嚟」
- 「一些」→「啲」
- 「很/非常」→「好」
- 「這樣」→「咁」
- 「怎樣」→「點」

請保留所有語氣詞（啦、囉、喎、吖、嘛、啊、呀、喇、咩、嘞、㗎、嚟、喺等）。

原文：
{text}

只輸出校正後的廣東話文字，不要任何解釋：""",
            }
        ],
    )
    return response.choices[0].message.content.strip()


def generate_summary_and_title(text: str) -> dict:
    """Use AI to generate a summary in formal written language and a short title."""
    response = openai_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {
                "role": "user",
                "content": f"""請根據以下內容，完成兩個任務：

1. 將內容改寫成書面語的摘要（保留重點，使用正式的書面語言）
2. 為這段內容取一個簡短的標題（10字以內）

原始內容：
{text}

請用以下 JSON 格式回覆（只輸出 JSON，不要其他內容）：
{{"title": "標題", "summary": "書面語摘要"}}""",
            }
        ],
    )
    result_text = response.choices[0].message.content.strip()
    # Remove markdown code block if present
    if result_text.startswith("```"):
        result_text = result_text.split("\n", 1)[1]
        result_text = result_text.rsplit("```", 1)[0]
    return json.loads(result_text)


def generate_cantonese_summary_and_title(text: str) -> dict:
    """Use AI to generate a Cantonese summary and a short title."""
    response = openai_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {
                "role": "user",
                "content": f"""請根據以下內容，完成兩個任務：

1. 將內容摘要成廣東話（粵語），使用口語化的廣東話表達，例如：
   - 使用「嘅」而非「的」
   - 使用「係」而非「是」
   - 使用「唔」而非「不」
   - 使用「冇」而非「沒有」
   - 使用「佢」而非「他/她」
   - 使用「啲」而非「一些」
   - 保留適當的語氣詞如：啦、喎、㗎、嘅等

2. 為這段內容取一個簡短的標題（10字以內，可用書面語）

原始內容：
{text}

請用以下 JSON 格式回覆（只輸出 JSON，不要其他內容）：
{{"title": "標題", "summary": "廣東話摘要"}}""",
            }
        ],
    )
    result_text = response.choices[0].message.content.strip()
    # Remove markdown code block if present
    if result_text.startswith("```"):
        result_text = result_text.split("\n", 1)[1]
        result_text = result_text.rsplit("```", 1)[0]
    return json.loads(result_text)


def save_to_notion(title: str, content: str, summary: str, note_type: str = "語音助手", page_content: str = None):
    """Save to Notion with Name, Content, Summary, Date, Type fields and optional page body."""
    today = datetime.now().strftime("%Y-%m-%d")

    page_data = {
        "parent": {"database_id": notion_database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Content": {"rich_text": [{"text": {"content": content}}]},
            "Summary": {"rich_text": [{"text": {"content": summary}}]},
            "Date": {"date": {"start": today}},
            "Type": {"select": {"name": note_type}},
        }
    }

    # Add page body content if provided
    if page_content:
        # Split content into chunks of 2000 characters (Notion limit)
        chunks = [page_content[i:i+2000] for i in range(0, len(page_content), 2000)]
        page_data["children"] = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                }
            }
            for chunk in chunks
        ]

    notion_client.pages.create(**page_data)


def truncate_content(text: str, max_length: int = 30) -> str:
    """Truncate text to max_length characters and add ellipsis."""
    # Remove extra whitespace and newlines
    clean_text = " ".join(text.split())
    if len(clean_text) <= max_length:
        return clean_text
    return clean_text[:max_length] + "......"


@app.route("/")
def home():
    return "ok"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info(
            "Invalid signature. Please check your channel access token/channel secret."
        )
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        user_text = event.message.text

        # Check if message starts with "/a "
        if not user_text.startswith("/a "):
            # Echo back and offer help in a friendly way
            reply_text = f"收到！你話：「{user_text}」\n\n有咩可以幫到你？\n\n💡 小提示：\n• 傳送語音 → 幫你轉成文字筆記\n• 輸入 /a 加文章 → 幫你摘要成廣東話"
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )
            return

        # Remove "/a " prefix
        article_text = user_text[3:].strip()

        if not article_text:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請在 /a 後面貼上文章內容")],
                )
            )
            return

        try:
            app.logger.info(f"Received article: {article_text[:100]}...")

            # Generate Cantonese summary and title
            result = generate_cantonese_summary_and_title(article_text)
            title = result["title"]
            summary = result["summary"]
            app.logger.info(f"Generated title: {title}, summary: {summary}")

            # Truncate content to ~30 characters
            content = truncate_content(article_text, 30)

            # Save to Notion with type "文字摘要" and original article in page body
            save_to_notion(
                title=title,
                content=content,
                summary=summary,
                note_type="文字摘要",
                page_content=article_text
            )

            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"✅ 已儲存到 Notion\n\n📌 標題：{title}\n\n📝 廣東話摘要：{summary}")],
                )
            )
        except Exception as e:
            app.logger.error(f"Text processing error: {str(e)}")
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"處理文字時發生錯誤：{str(e)}")],
                )
            )


@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)

        try:
            audio_content = line_bot_blob_api.get_message_content(event.message.id)
            audio_base64 = base64.b64encode(audio_content).decode("utf-8")
            app.logger.info(f"Audio size: {len(audio_content)} bytes")

            # Step 1: Transcribe audio to Cantonese text
            response = openai_client.chat.completions.create(
                model="google/gemini-2.5-flash",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:audio/mp4;base64,{audio_base64}",
                                },
                            },
                            {
                                "type": "text",
                                "text": """這是一段廣東話（粵語）語音訊息。請仔細聆聽並逐字轉錄成廣東話書寫文字。

重要要求：
1. 使用廣東話專用字詞，例如：
   - 嘅（的）、唔（不）、係（是）、咁（這樣）、嗰（那）
   - 佢（他/她）、佢哋（他們）、我哋（我們）、你哋（你們）
   - 冇（沒有）、啲（一些）、嘢（東西）、咗（了）、緊（著）
   - 畀（給）、睇（看）、講（說）、諗（想）、喺（在）
   - 返（回）、嚟（來）、去、過嚟（過來）

2. 完整保留所有語氣詞和句末助詞：
   啦、囉、喎、吖、嘛、啊、呀、喇、咩、嘞、㗎、嚟、喺、噃、啩、嘎、咋、喂、哇、唉

3. 保持口語化表達，不要轉換成書面語

只輸出轉錄的廣東話文字，不要任何解釋或說明。""",
                            },
                        ],
                    }
                ],
            )
            raw_transcription = response.choices[0].message.content.strip()
            app.logger.info(f"Raw transcription: {raw_transcription}")

            # Step 2: Correct Cantonese characters
            transcribed_text = correct_cantonese_text(raw_transcription)
            app.logger.info(f"Corrected transcription: {transcribed_text}")

            # Generate summary and title using AI
            result = generate_summary_and_title(transcribed_text)
            title = result["title"]
            summary = result["summary"]
            app.logger.info(f"Generated title: {title}, summary: {summary}")

            save_to_notion(title=title, content=transcribed_text, summary=summary)

            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"✅ 已儲存到 Notion\n\n📌 標題：{title}\n\n📝 摘要：{summary}")],
                )
            )
        except Exception as e:
            app.logger.error(f"Audio processing error: {str(e)}")
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"處理語音時發生錯誤：{str(e)}")],
                )
            )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
