import os
import io
import re
import base64
import json
import random
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
    ImageMessage,
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

# 20 art styles for random image generation
ART_STYLES = [
    "Impressionist painting style, soft brushstrokes, vibrant colors like Monet",
    "Japanese anime style, cel-shaded, big expressive eyes",
    "Cyberpunk neon style, futuristic, glowing lights, dark atmosphere",
    "Watercolor illustration, soft edges, flowing colors, artistic",
    "Pop art style like Andy Warhol, bold colors, comic-like",
    "Studio Ghibli animation style, whimsical, detailed backgrounds",
    "Oil painting Renaissance style, dramatic lighting, classical",
    "Minimalist flat design, simple shapes, limited color palette",
    "Steampunk Victorian style, brass gears, vintage machinery",
    "Pixel art retro game style, 8-bit aesthetic, nostalgic",
    "Art Nouveau style, flowing organic lines, decorative patterns",
    "Vaporwave aesthetic, pastel colors, 80s retro, glitch effects",
    "Chinese ink wash painting style, elegant brushwork, traditional",
    "Surrealist style like Salvador Dali, dreamlike, impossible scenes",
    "Low poly 3D style, geometric shapes, modern digital art",
    "Ukiyo-e Japanese woodblock print style, bold outlines, flat colors",
    "Gothic dark fantasy style, mysterious, dramatic shadows",
    "Bauhaus geometric style, primary colors, functional design",
    "Psychedelic 60s style, vibrant swirling patterns, trippy colors",
    "Chibi kawaii style, cute exaggerated proportions, adorable",
]


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


def transform_prompt_with_style(original_prompt: str, new_style: str) -> str:
    """Transform the original prompt to use a new art style."""
    response = openai_client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {
                "role": "user",
                "content": f"""Transform this image prompt to use a completely different art style.

Original prompt:
{original_prompt}

New style to apply:
{new_style}

Requirements:
1. Keep the main subject and composition from the original
2. Replace ALL style-related descriptions with the new style
3. Adapt lighting, colors, and mood to match the new style
4. Keep the prompt concise but descriptive (under 200 words)
5. Output ONLY the new prompt, no explanations

New prompt:""",
            }
        ],
    )
    return response.choices[0].message.content.strip()


def generate_new_image(prompt: str) -> tuple[str, str]:
    """Generate a new image using Google Imagen 4.0. Returns (image_url, model_used) or raises exception."""

    google_ai_key = os.getenv("GOOGLE_AI_API_KEY")

    app.logger.info("Generating image with Imagen 4.0...")

    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/imagen-4.0-generate-001:predict?key={google_ai_key}",
        headers={
            "Content-Type": "application/json",
        },
        json={
            "instances": [
                {"prompt": prompt}
            ],
            "parameters": {
                "sampleCount": 1
            }
        },
        timeout=120,
    )

    if response.status_code != 200:
        error_msg = response.text
        app.logger.error(f"Imagen 4.0 API error: {error_msg}")
        raise Exception(f"Imagen 4.0 生成失敗：{response.status_code} - {error_msg[:200]}")

    result = response.json()
    app.logger.info(f"Imagen 4.0 response: {json.dumps(result)[:500]}")

    # Extract base64 image from response
    if "predictions" in result and len(result["predictions"]) > 0:
        prediction = result["predictions"][0]
        if "bytesBase64Encoded" in prediction:
            image_bytes = base64.b64decode(prediction["bytesBase64Encoded"])
        elif "image" in prediction and "bytesBase64Encoded" in prediction["image"]:
            image_bytes = base64.b64decode(prediction["image"]["bytesBase64Encoded"])
        else:
            raise Exception(f"無法從回應中提取圖片：{json.dumps(prediction)[:200]}")
    else:
        raise Exception(f"回應格式異常：{json.dumps(result)[:200]}")

    # Upload to Google Drive
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"generated_{timestamp}.png"
    image_url = upload_to_google_drive(image_bytes, filename)

    return image_url, "Imagen 4.0"


def download_image_from_url(url: str) -> bytes:
    """Download image from URL and return bytes."""
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def save_image_to_notion(title: str, prompt: str, image_url: str, line_id: str = None, generated_image_url: str = None, style_used: str = None, new_prompt: str = None):
    """Save image entry to Notion with original and generated images."""
    today = datetime.now().strftime("%Y-%m-%d")

    # Build image files list - include both original and generated if available
    image_files = [{"type": "external", "name": f"{title} (原圖)", "external": {"url": image_url}}]
    if generated_image_url:
        image_files.append({"type": "external", "name": f"{title} (AI生成)", "external": {"url": generated_image_url}})

    page_data = {
        "parent": {"database_id": notion_database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Image": {"files": image_files},
            "Prompt": {"rich_text": [{"text": {"content": prompt}}]},
            "Content": {"rich_text": [{"text": {"content": style_used or ""}}]},
            "Summary": {"rich_text": [{"text": {"content": new_prompt or ""}}]},
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


def detect_social_platform(url: str) -> str | None:
    """Detect if URL is from Facebook or Threads. Returns 'facebook', 'threads', or None."""
    url_lower = url.lower()
    if "facebook.com" in url_lower or "fb.com" in url_lower or "fb.watch" in url_lower:
        return "facebook"
    elif "threads.com" in url_lower or "threads.net" in url_lower:
        return "threads"
    return None


def fetch_social_content_with_apify(url: str, platform: str) -> dict:
    """Fetch social media content using Apify. Returns dict with title and content."""
    apify_key = os.getenv("APIFY_API_KEY")
    if not apify_key:
        raise Exception("未設置 APIFY_API_KEY 環境變數")

    # Select the appropriate Apify actor
    if platform == "facebook":
        actor_id = "apify/facebook-posts-scraper"
    elif platform == "threads":
        actor_id = "futurizerush/meta-threads-scraper"
    else:
        raise Exception(f"不支援的平台：{platform}")

    app.logger.info(f"Fetching {platform} content with Apify actor: {actor_id}")

    # Convert actor ID format from "user/actor" to "user~actor" for API
    api_actor_id = actor_id.replace("/", "~")

    # Run the Apify actor synchronously with standard input format
    response = requests.post(
        f"https://api.apify.com/v2/acts/{api_actor_id}/run-sync-get-dataset-items",
        headers={
            "Authorization": f"Bearer {apify_key}",
            "Content-Type": "application/json",
        },
        json={
            "startUrls": [{"url": url}],
            "maxResults": 1,
        },
        timeout=180,  # Longer timeout for all platforms
    )

    # Accept both 200 (OK) and 201 (Created) as success
    if response.status_code not in [200, 201]:
        error_msg = response.text
        app.logger.error(f"Apify API error: {error_msg}")
        raise Exception(f"Apify 爬取失敗：{response.status_code} - {error_msg[:200]}")

    results = response.json()
    app.logger.info(f"Apify response: {json.dumps(results)[:500]}")

    if not results or len(results) == 0:
        raise Exception("Apify 未能爬取到任何內容")

    post = results[0]

    # Extract content based on platform
    if platform == "facebook":
        # Get author name from user object
        author_name = post.get("user", {}).get("name") or post.get("userName") or "某用戶"
        title = f"{author_name}的Facebook貼文"
        content = post.get("text") or post.get("message") or ""
        if post.get("link"):
            content += f"\n\n連結：{post.get('link')}"
    elif platform == "threads":
        # Get author username
        author_username = post.get("ownerUsername") or post.get("username") or "某用戶"
        title = f"{author_username}的Threads貼文"
        content = post.get("text") or post.get("caption") or ""

    return {"title": title, "content": content, "raw": post}


def save_social_to_notion(title: str, summary: str, original_content: str, platform: str, line_id: str = None):
    """Save social media content to Notion with platform-specific Type tag."""
    today = datetime.now().strftime("%Y-%m-%d")

    # Set type based on platform
    if platform == "facebook":
        note_type = "Facebook"
    elif platform == "threads":
        note_type = "Threads"
    else:
        note_type = "社交媒體"

    # Split content into chunks of 1900 characters (Notion limit is 2000)
    chunks = [original_content[i:i+1900] for i in range(0, len(original_content), 1900)]

    page_data = {
        "parent": {"database_id": notion_database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Content": {"rich_text": [{"text": {"content": ""}}]},
            "Summary": {"rich_text": [{"text": {"content": summary}}]},
            "Date": {"date": {"start": today}},
            "Type": {"select": {"name": note_type}},
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
        ] if chunks else []
    }

    # Add lineID if provided
    if line_id:
        page_data["properties"]["lineID"] = {"rich_text": [{"text": {"content": line_id}}]}

    notion_client.pages.create(**page_data)


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


def clean_text_for_notion(text: str) -> str:
    """Remove invalid Unicode characters that Notion can't handle."""
    # Remove null bytes and other problematic control characters
    cleaned = text.replace('\x00', '')
    # Remove other control characters except newline, tab, carriage return
    cleaned = ''.join(char for char in cleaned if ord(char) >= 32 or char in '\n\t\r')
    # Ensure valid encoding
    cleaned = cleaned.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
    return cleaned


def save_webpage_to_notion(title: str, summary: str, original_content: str, line_id: str = None):
    """Save webpage summary to Notion with Type '網頁摘要' and lineID."""
    today = datetime.now().strftime("%Y-%m-%d")

    # Clean text to remove invalid Unicode characters
    title = clean_text_for_notion(title)
    summary = clean_text_for_notion(summary)
    original_content = clean_text_for_notion(original_content)

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

                # Check if it's a social media URL (Facebook or Threads)
                social_platform = detect_social_platform(detected_url)

                if social_platform == "facebook":
                    # Use Apify to fetch Facebook content
                    app.logger.info(f"Detected Facebook URL, using Apify...")
                    social_data = fetch_social_content_with_apify(detected_url, social_platform)
                    title = social_data["title"]
                    content = social_data["content"]
                    app.logger.info(f"Fetched Facebook content: {title}, content length: {len(content)}")

                    if not content:
                        raise Exception("未能從貼文中提取到文字內容")

                    # Summarize content
                    summary = summarize_webpage(content)
                    app.logger.info(f"Generated summary: {summary[:100]}...")

                    # Save to Notion with platform-specific tag
                    save_social_to_notion(
                        title=title,
                        summary=summary,
                        original_content=content,
                        platform=social_platform,
                        line_id=user_line_id
                    )

                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=f"✅ 已儲存 Facebook 貼文到 Notion\n\n📌 來源：{title}\n\n📝 摘要：{summary}")],
                        )
                    )
                elif social_platform == "threads":
                    # Threads not yet supported
                    app.logger.info(f"Detected Threads URL, but not yet supported")
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=f"抱歉，暫時還未支援 Threads 內容抓取。\n\n目前支援：\n✅ Facebook 貼文\n✅ 一般網頁")],
                        )
                    )
                else:
                    # Regular webpage - use existing logic
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

            # 3. Upload original to Google Drive
            image_url = upload_to_google_drive(image_content, filename)
            app.logger.info(f"Uploaded original to Google Drive: {image_url}")

            # 4. Base64 encode and analyze with AI
            image_base64 = base64.b64encode(image_content).decode("utf-8")
            result = generate_image_prompt(image_base64)
            title = result["title"]
            prompt = result["prompt"]
            app.logger.info(f"Generated title: {title}, prompt: {prompt[:100]}...")

            # 5. Randomly select a new style and transform prompt
            selected_style = random.choice(ART_STYLES)
            app.logger.info(f"Selected style: {selected_style}")
            new_prompt = transform_prompt_with_style(prompt, selected_style)
            app.logger.info(f"Transformed prompt: {new_prompt[:100]}...")

            # 6. Try to generate new image
            style_name = selected_style.split(",")[0]
            app.logger.info("Generating new image with AI...")

            try:
                generated_image_url, model_used = generate_new_image(new_prompt)
                app.logger.info(f"Generated image URL: {generated_image_url}, model: {model_used}")

                # Save both images to Notion
                save_image_to_notion(
                    title=title,
                    prompt=prompt,
                    image_url=image_url,
                    line_id=user_line_id,
                    generated_image_url=generated_image_url,
                    style_used=selected_style,
                    new_prompt=new_prompt
                )

                # Reply with success message and generated image
                reply_text = f"""收到你張相啦！📸

睇落係「{title}」嚟嘅～ 我已經幫你收藏咗去 Notion 喇！

順便幫你用「{style_name}」風格重新繪製咗一張，希望你鍾意啦 🎨✨

🤖 生成模型：{model_used}"""

                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(text=reply_text),
                            ImageMessage(
                                original_content_url=generated_image_url,
                                preview_image_url=generated_image_url
                            )
                        ],
                    )
                )

            except Exception as gen_error:
                # Image generation failed, but still save original to Notion
                app.logger.error(f"Image generation failed: {str(gen_error)}")

                save_image_to_notion(
                    title=title,
                    prompt=prompt,
                    image_url=image_url,
                    line_id=user_line_id,
                    style_used=selected_style,
                    new_prompt=new_prompt
                )

                # Reply with partial success message
                reply_text = f"""收到你張相啦！📸

睇落係「{title}」嚟嘅～ 我已經幫你收藏咗去 Notion 喇！

本來想幫你用「{style_name}」風格重新繪製，但係生成圖片時出咗啲問題 😅

❌ 錯誤：{str(gen_error)[:150]}"""

                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)],
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
    # Use PORT environment variable for Zeabur/Railway, default to 8000 for local
    port = int(os.getenv("PORT", 8000))
    print(f"=" * 50)
    print(f"Starting LINE Bot Secretary on port {port}")
    print(f"Environment check:")
    print(f"  LINE_CHANNEL_ACCESS_TOKEN: {'✓' if os.getenv('LINE_CHANNEL_ACCESS_TOKEN') else '✗ MISSING'}")
    print(f"  NOTION_TOKEN: {'✓' if os.getenv('NOTION_TOKEN') else '✗ MISSING'}")
    print(f"  GOOGLE_AI_API_KEY: {'✓' if os.getenv('GOOGLE_AI_API_KEY') else '✗ MISSING'}")
    print(f"=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
