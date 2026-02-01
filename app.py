import os
import io
import re
import base64
import json
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from dotenv import load_dotenv
from openai import OpenAI
from notion_client import Client as NotionClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, AudioMessageContent, ImageMessageContent
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

# Allowed LINE user IDs (whitelist)
ALLOWED_LINE_USER_IDS = {"Uca76be212cf92a65ad706eac60503cc2"}
UNAUTHORIZED_MESSAGE = "抱歉，這是私人 Line Bot，未授權的用戶無法使用。"


def is_chinese_text(text: str) -> bool:
    """Check if text is primarily Chinese (including Traditional and Simplified)."""
    # Count Chinese characters (CJK Unified Ideographs range)
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    # Count total non-whitespace characters
    total_chars = sum(1 for c in text if not c.isspace())
    if total_chars == 0:
        return False
    # Consider text as Chinese if more than 20% are Chinese characters
    return (chinese_chars / total_chars) > 0.2


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
    """Use AI to generate a Cantonese summary and a short title, translating if needed."""
    # Check if content is Chinese
    needs_translation = not is_chinese_text(text)

    if needs_translation:
        prompt = f"""請根據以下外語內容，完成兩個任務：

1. 將內容翻譯並摘要成廣東話（粵語），使用口語化的廣東話表達，例如：
   - 使用「嘅」而非「的」
   - 使用「係」而非「是」
   - 使用「唔」而非「不」
   - 使用「冇」而非「沒有」
   - 使用「佢」而非「他/她」
   - 使用「啲」而非「一些」
   - 保留適當的語氣詞如：啦、喎、㗎、嘅等

2. 為這段內容取一個簡短的標題（10字以內，可用書面語）

原始內容（外語）：
{text}

請用以下 JSON 格式回覆（只輸出 JSON，不要其他內容）：
{{"title": "標題", "summary": "廣東話摘要"}}"""
    else:
        prompt = f"""請根據以下內容，完成兩個任務：

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
{{"title": "標題", "summary": "廣東話摘要"}}"""

    response = openai_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )
    result_text = response.choices[0].message.content.strip()
    # Remove markdown code block if present
    if result_text.startswith("```"):
        result_text = result_text.split("\n", 1)[1]
        result_text = result_text.rsplit("```", 1)[0]
    return json.loads(result_text)


def save_to_notion(title: str, content: str, summary: str, note_type: str = "語音助手", page_content: str = None, line_id: str = None):
    """Save to Notion with Name, Content, Summary, Date, Type, lineID fields and optional page body."""
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

    # Add lineID if provided
    if line_id:
        page_data["properties"]["lineID"] = {"rich_text": [{"text": {"content": line_id}}]}

    # Add page body content if provided
    if page_content:
        # Split content into chunks of 1900 characters (Notion limit is 2000)
        chunks = [page_content[i:i+1900] for i in range(0, len(page_content), 1900)]
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


def get_google_drive_service():
    """Initialize Google Drive API client with OAuth2 credentials."""
    credentials = Credentials(
        token=None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    )
    return build("drive", "v3", credentials=credentials)


def upload_to_google_drive(image_content: bytes, filename: str) -> str:
    """Upload image to Google Drive and return thumbnail URL."""
    drive_service = get_google_drive_service()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    # Upload file
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    media = MediaIoBaseUpload(
        io.BytesIO(image_content),
        mimetype="image/jpeg",
        resumable=True
    )
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()

    file_id = file.get("id")

    # Set public read permission
    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()

    # Return thumbnail URL
    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w2000"


def generate_image_prompt(image_base64: str) -> dict:
    """Analyze image with AI and generate English prompt describing style and content."""
    response = openai_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": """Analyze this image and provide:

1. A detailed English prompt that could be used to recreate this image. Include:
   - Art style (photography, illustration, painting, digital art, etc.)
   - Subject matter and composition
   - Lighting and mood
   - Color palette
   - Notable details and textures

2. A short title (5 words max) describing the main subject

Respond in JSON format only:
{"prompt": "detailed English prompt here", "title": "Short Title"}""",
                    },
                ],
            }
        ],
    )
    result_text = response.choices[0].message.content.strip()
    # Remove markdown code block if present
    if result_text.startswith("```"):
        result_text = result_text.split("\n", 1)[1]
        result_text = result_text.rsplit("```", 1)[0]
    return json.loads(result_text)


def save_image_to_notion(title: str, prompt: str, image_url: str, line_id: str = None):
    """Save image entry to Notion with Image, Prompt, Type, Date, and lineID fields."""
    today = datetime.now().strftime("%Y-%m-%d")

    page_data = {
        "parent": {"database_id": notion_database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Image": {"files": [{"type": "external", "name": title, "external": {"url": image_url}}]},
            "Prompt": {"rich_text": [{"text": {"content": prompt}}]},
            "Content": {"rich_text": [{"text": {"content": ""}}]},
            "Summary": {"rich_text": [{"text": {"content": ""}}]},
            "Type": {"select": {"name": "圖片助手"}},
            "Date": {"date": {"start": today}},
        }
    }

    # Add lineID if provided
    if line_id:
        page_data["properties"]["lineID"] = {"rich_text": [{"text": {"content": line_id}}]}

    notion_client.pages.create(**page_data)


def truncate_content(text: str, max_length: int = 30) -> str:
    """Truncate text to max_length characters and add ellipsis."""
    # Remove extra whitespace and newlines
    clean_text = " ".join(text.split())
    if len(clean_text) <= max_length:
        return clean_text
    return clean_text[:max_length] + "......"


def detect_url(text: str) -> str | None:
    """Detect URL in text and return the first match."""
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    match = re.search(url_pattern, text)
    return match.group(0) if match else None


def fetch_webpage_content(url: str) -> dict:
    """Fetch webpage and extract title and main content."""
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
    session.headers.update(headers)
    response = session.get(url, timeout=30, allow_redirects=True, stream=True)

    # Check content length to avoid downloading huge files
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > 5 * 1024 * 1024:  # 5MB limit
        raise ValueError("網頁內容過大，無法處理")

    # Read content with size limit
    content_bytes = b""
    for chunk in response.iter_content(chunk_size=8192):
        content_bytes += chunk
        if len(content_bytes) > 5 * 1024 * 1024:  # 5MB limit
            raise ValueError("網頁內容過大，無法處理")

    # Some sites like Medium return 403 but still include content
    if response.status_code == 403 and len(content_bytes) > 1000:
        pass  # Continue processing - content is likely present
    elif response.status_code >= 400:
        response.raise_for_status()

    # Check Content-Type - reject non-HTML content
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" not in content_type and "text/plain" not in content_type:
        # Check if it might be a PDF or other binary file
        if "application/pdf" in content_type:
            raise ValueError("此網址為 PDF 檔案，暫不支援 PDF 摘要")
        if not content_type.startswith("text/"):
            raise ValueError(f"此網址非網頁內容 (Content-Type: {content_type})")

    # Try to detect encoding properly
    # Priority: 1. HTTP header charset, 2. HTML meta charset, 3. apparent_encoding
    encoding = None

    # Check HTTP header for charset
    if "charset=" in content_type:
        encoding = content_type.split("charset=")[-1].split(";")[0].strip()

    # If no charset in header, check HTML meta tag
    if not encoding:
        # Parse raw bytes to find meta charset
        raw_content = content_bytes[:2048]  # Check first 2KB
        meta_match = re.search(rb'charset=["\']?([^"\'\s>]+)', raw_content, re.IGNORECASE)
        if meta_match:
            encoding = meta_match.group(1).decode("ascii", errors="ignore")

    # Fallback to detected encoding or utf-8
    if not encoding:
        # Try to detect encoding from content using charset_normalizer (bundled with requests)
        from charset_normalizer import from_bytes
        detected = from_bytes(content_bytes[:10000]).best()
        encoding = detected.encoding if detected else "utf-8"

    # Validate encoding - try to decode and check for garbled text
    try:
        text = content_bytes.decode(encoding)
        # Check if decoded text looks like binary garbage
        # Binary data often has many replacement characters or control chars
        control_chars = sum(1 for c in text[:1000] if ord(c) < 32 and c not in '\n\r\t')
        replacement_chars = text[:1000].count('\ufffd')
        if control_chars > 50 or replacement_chars > 50:
            # Try UTF-8 as fallback
            text = content_bytes.decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, LookupError):
        # Fallback to UTF-8 with error handling
        text = content_bytes.decode("utf-8", errors="ignore")

    soup = BeautifulSoup(text, "html.parser")

    # Extract title
    title = ""
    if soup.title:
        title = soup.title.string.strip() if soup.title.string else ""

    # Remove script, style, nav, footer, header elements
    for element in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        element.decompose()

    # Try to find main content area
    main_content = None
    for selector in ["article", "main", '[role="main"]', ".content", "#content", ".post", ".article"]:
        main_content = soup.select_one(selector)
        if main_content:
            break

    # Fallback to body if no main content found
    if not main_content:
        main_content = soup.body if soup.body else soup

    # Extract text
    text = main_content.get_text(separator="\n", strip=True)

    # Clean up text: remove excessive newlines
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    clean_text = "\n".join(lines)

    return {"title": title, "content": clean_text}


def summarize_webpage(content: str) -> str:
    """Use AI to summarize webpage content in Traditional Chinese, translating if needed."""
    # Limit content length to avoid token limits
    max_content_length = 10000
    if len(content) > max_content_length:
        content = content[:max_content_length] + "..."

    # Check if content is Chinese
    needs_translation = not is_chinese_text(content)

    if needs_translation:
        prompt = f"""請閱讀以下外語網頁內容，將其翻譯並摘要成繁體中文。

要求：
1. 使用繁體中文
2. 摘要應涵蓋主要重點
3. 保持簡潔，約 100-300 字
4. 使用書面語
5. 確保翻譯準確，保留原文的核心意思

網頁內容：
{content}

請只輸出繁體中文摘要內容，不要加入任何標題或前綴："""
    else:
        prompt = f"""請閱讀以下網頁內容，並用繁體中文撰寫一份摘要。

要求：
1. 使用繁體中文
2. 摘要應涵蓋主要重點
3. 保持簡潔，約 100-300 字
4. 使用書面語

網頁內容：
{content}

請只輸出摘要內容，不要加入任何標題或前綴："""

    response = openai_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )
    return response.choices[0].message.content.strip()


def save_webpage_to_notion(title: str, summary: str, original_content: str, line_id: str = None):
    """Save webpage summary to Notion with Type '網頁摘要' and lineID."""
    today = datetime.now().strftime("%Y-%m-%d")

    # Split content into chunks of 1900 characters (Notion limit is 2000)
    chunks = [original_content[i:i+1900] for i in range(0, len(original_content), 1900)]

    page_data = {
        "parent": {"database_id": notion_database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Content": {"rich_text": [{"text": {"content": ""}}]},
            "Summary": {"rich_text": [{"text": {"content": summary}}]},
            "Date": {"date": {"start": today}},
            "Type": {"select": {"name": "網頁摘要"}},
        },
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                }
            }
            for chunk in chunks
        ]
    }

    # Add lineID if provided
    if line_id:
        page_data["properties"]["lineID"] = {"rich_text": [{"text": {"content": line_id}}]}

    notion_client.pages.create(**page_data)


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
        user_line_id = event.source.user_id

        # Check if user is authorized
        if user_line_id not in ALLOWED_LINE_USER_IDS:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=UNAUTHORIZED_MESSAGE)],
                )
            )
            return

        # Check if message contains a URL
        detected_url = detect_url(user_text)
        if detected_url:
            try:
                app.logger.info(f"Detected URL: {detected_url}")

                # Fetch webpage content
                webpage = fetch_webpage_content(detected_url)
                title = webpage["title"] or "無標題網頁"
                content = webpage["content"]
                app.logger.info(f"Fetched webpage: {title}, content length: {len(content)}")
                app.logger.info(f"Content preview: {content[:200]}...")

                # Summarize content
                summary = summarize_webpage(content)
                app.logger.info(f"Generated summary: {summary[:100]}...")

                # Save to Notion
                save_webpage_to_notion(title=title, summary=summary, original_content=content, line_id=user_line_id)

                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"✅ 已儲存到 Notion\n\n📌 標題：{title}\n\n📝 摘要：{summary}")],
                    )
                )
            except Exception as e:
                app.logger.error(f"URL processing error: {str(e)}")
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"處理網址時發生錯誤：{str(e)}")],
                    )
                )
            return

        # Check if message starts with "/a "
        if not user_text.startswith("/a "):
            # Echo back and offer help in a friendly way
            reply_text = f"收到！你話：「{user_text}」\n\n有咩可以幫到你？\n\n💡 小提示：\n• 傳送語音 → 幫你轉成文字筆記\n• 輸入 /a 加文章 → 幫你摘要成廣東話\n• 貼上網址 → 幫你摘要網頁內容"
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
                page_content=article_text,
                line_id=user_line_id
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
        user_line_id = event.source.user_id

        # Check if user is authorized
        if user_line_id not in ALLOWED_LINE_USER_IDS:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=UNAUTHORIZED_MESSAGE)],
                )
            )
            return

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

            save_to_notion(title=title, content=transcribed_text, summary=summary, line_id=user_line_id)

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


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        user_line_id = event.source.user_id

        # Check if user is authorized
        if user_line_id not in ALLOWED_LINE_USER_IDS:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=UNAUTHORIZED_MESSAGE)],
                )
            )
            return

        try:
            # 1. Download image
            image_content = line_bot_blob_api.get_message_content(event.message.id)
            app.logger.info(f"Image size: {len(image_content)} bytes")

            # 2. Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"line_image_{timestamp}.jpg"

            # 3. Upload to Google Drive
            image_url = upload_to_google_drive(image_content, filename)
            app.logger.info(f"Uploaded to Google Drive: {image_url}")

            # 4. Base64 encode and analyze with AI
            image_base64 = base64.b64encode(image_content).decode("utf-8")
            result = generate_image_prompt(image_base64)
            title = result["title"]
            prompt = result["prompt"]
            app.logger.info(f"Generated title: {title}, prompt: {prompt[:100]}...")

            # 5. Save to Notion
            save_image_to_notion(title=title, prompt=prompt, image_url=image_url, line_id=user_line_id)

            # 6. Reply to user
            prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"✅ 已儲存到 Notion\n\n📌 標題：{title}\n\n🎨 Prompt：{prompt_preview}")],
                )
            )
        except Exception as e:
            app.logger.error(f"Image processing error: {str(e)}")
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"處理圖片時發生錯誤：{str(e)}")],
                )
            )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
