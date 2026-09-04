```python
import os
import re
import time
import json
import logging
import asyncio
import threading

import yt_dlp
import static_ffmpeg

from groq import Groq
from youtube_transcript_api import YouTubeTranscriptApi

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from http.server import BaseHTTPRequestHandler, HTTPServer


# =========================================================
# إعداد التسجيل
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# مفاتيح البيئة
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN غير موجود في Environment Variables")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY غير موجود في Environment Variables")


# =========================================================
# Groq
# =========================================================

groq_client = Groq(api_key=GROQ_API_KEY)

# النموذج الجديد للتلخيص
SUMMARY_MODEL = "openai/gpt-oss-120b"

# Whisper
WHISPER_MODEL = "whisper-large-v3"


# =========================================================
# FFmpeg
# =========================================================

try:
    static_ffmpeg.add_paths(weak=True)
    logger.info("FFmpeg جاهز.")
except Exception as e:
    logger.warning(f"تعذر تجهيز FFmpeg عند البداية: {e}")


# =========================================================
# Health Check - Render
# =========================================================

class HealthCheckHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8080))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthCheckHandler
    )

    logger.info(f"Health server started on port {port}")

    server.serve_forever()


# =========================================================
# استخراج YouTube ID
# =========================================================

def extract_youtube_id(url: str) -> str | None:

    url = url.strip()

    patterns = [
        r"(?:v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/v/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/live/)([A-Za-z0-9_-]{11})",
    ]

    for pattern in patterns:

        match = re.search(pattern, url)

        if match:
            return match.group(1)

    # إذا أرسل المستخدم الـ ID مباشرة
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url

    return None


# =========================================================
# YouTube Transcript
# =========================================================

def get_youtube_transcript_api(url: str) -> str:

    video_id = extract_youtube_id(url)

    if not video_id:
        raise Exception(
            "رابط YouTube غير صحيح أو تعذر استخراج Video ID."
        )

    logger.info(f"YouTube Video ID: {video_id}")

    try:

        ytt_api = YouTubeTranscriptApi()

        # نحاول العربية أولاً ثم الإنجليزية
        transcript = ytt_api.fetch(
            video_id,
            languages=["ar", "en"]
        )

        texts = []

        for item in transcript:

            # youtube-transcript-api الحديثة ترجع snippets
            # قد تكون كائنات وليست dict
            if hasattr(item, "text"):
                text = item.text
            elif isinstance(item, dict):
                text = item.get("text", "")
            else:
                text = str(item)

            if text:
                texts.append(text)

        full_text = " ".join(texts).strip()

        if not full_text:
            raise Exception("تم العثور على الترجمة ولكنها فارغة.")

        logger.info(
            f"YouTube transcript extracted: {len(full_text)} characters"
        )

        return full_text

    except Exception as e:

        logger.exception(
            f"YouTube Transcript Error for {video_id}"
        )

        raise Exception(
            f"تعذر جلب ترجمة YouTube: {str(e)}"
        )


# =========================================================
# تحميل الصوت
# =========================================================

def download_audio(url: str, user_id: str) -> str:

    # التأكد من FFmpeg
    try:
        static_ffmpeg.add_paths(weak=True)
    except Exception:
        pass

    base_name = (
        f"audio_{user_id}_{int(time.time())}"
    )

    output_template = f"{base_name}.%(ext)s"

    ydl_opts = {

        "format": "bestaudio/best",

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "nocheckcertificate": True,

        "geo_bypass": True,

        "http_headers": {
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
        },

        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
    }

    logger.info(f"Downloading audio: {url}")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        ydl.download([url])

    output_file = f"{base_name}.mp3"

    if not os.path.exists(output_file):

        raise Exception(
            "تم تحميل المقطع ولكن ملف الصوت لم يتم إنشاؤه."
        )

    logger.info(
        f"Audio ready: {output_file}"
    )

    return output_file


# =========================================================
# Whisper
# =========================================================

def transcribe_audio(file_path: str) -> str:

    logger.info(
        f"Sending audio to Groq Whisper: {file_path}"
    )

    with open(file_path, "rb") as file:

        result = groq_client.audio.transcriptions.create(

            file=(file_path, file.read()),

            model=WHISPER_MODEL,

            language="ar",

            response_format="text",

            temperature=0.0,
        )

    text = str(result).strip()

    if not text:
        raise Exception(
            "Whisper أعاد نصًا فارغًا."
        )

    logger.info(
        f"Whisper transcription: {len(text)} characters"
    )

    return text


# =========================================================
# تقسيم النص الطويل
# =========================================================

def split_text(text: str, max_chars: int = 12000):

    text = text.strip()

    if len(text) <= max_chars:
        return [text]

    chunks = []

    current = []

    current_length = 0

    paragraphs = re.split(
        r"\n+|(?<=[.!؟])\s+",
        text
    )

    for paragraph in paragraphs:

        paragraph = paragraph.strip()

        if not paragraph:
            continue

        paragraph_length = len(paragraph)

        if (
            current
            and current_length + paragraph_length + 1
            > max_chars
        ):

            chunks.append(
                " ".join(current)
            )

            current = []
            current_length = 0

        current.append(paragraph)

        current_length += paragraph_length + 1

    if current:
        chunks.append(
            " ".join(current)
        )

    return chunks


# =========================================================
# تلخيص جزء
# =========================================================

def summarize_chunk(text: str) -> dict:

    response = groq_client.chat.completions.create(

        model=SUMMARY_MODEL,

        messages=[

            {
                "role": "system",

                "content": (
                    "أنت مساعد عربي محترف في تحليل وتلخيص "
                    "التفريغات الصوتية.\n\n"

                    "اقرأ النص ثم استخرج أهم الأفكار "
                    "والمعلومات دون اختلاق معلومات غير موجودة.\n\n"

                    "أرجع JSON فقط بهذا الشكل:\n"

                    "{"
                    '"title": "عنوان مختصر من 4 إلى 8 كلمات",'
                    '"summary": "نقاط مرتبة ومفيدة تلخص أهم المعلومات"'
                    "}\n\n"

                    "يجب أن تكون الإجابة باللغة العربية."
                ),
            },

            {
                "role": "user",
                "content": text,
            },
        ],

        temperature=0.2,

        response_format={
            "type": "json_object"
        },
    )

    raw = response.choices[0].message.content

    try:

        return json.loads(raw)

    except Exception:

        return {
            "title": "ملخص المحتوى",
            "summary": raw.strip(),
        }


# =========================================================
# تلخيص النص الكامل
# =========================================================

def summarize_text_model(text: str) -> dict:

    chunks = split_text(
        text,
        max_chars=12000
    )

    logger.info(
        f"Text split into {len(chunks)} chunk(s)"
    )

    # إذا كان النص قصيرًا
    if len(chunks) == 1:

        return summarize_chunk(chunks[0])

    # تلخيص كل جزء
    chunk_summaries = []

    for index, chunk in enumerate(chunks, 1):

        logger.info(
            f"Summarizing chunk {index}/{len(chunks)}"
        )

        result = summarize_chunk(chunk)

        chunk_summaries.append(
            result.get("summary", "")
        )

    # تجميع الملخصات
    combined = "\n\n".join(
        chunk_summaries
    )

    # ملخص نهائي
    final_prompt = f"""
لديك ملخصات متعددة لأجزاء من تفريغ صوتي عربي.

قم بدمجها في ملخص واحد متماسك.

مهم جدًا:
- لا تحذف المعلومات المهمة.
- لا تكرر نفس الفكرة.
- رتب الأفكار منطقيًا.
- استخدم نقاطًا واضحة.
- لا تضف معلومات غير موجودة.
- أعطني عنوانًا مناسبًا.

الملخصات:

{combined}
"""

    response = groq_client.chat.completions.create(

        model=SUMMARY_MODEL,

        messages=[

            {
                "role": "system",

                "content": (
                    "أنت محرر عربي محترف. "
                    "أعد النتيجة JSON فقط بالشكل:\n"
                    '{"title":"عنوان مختصر","summary":"ملخص منظم بنقاط"}'
                ),
            },

            {
                "role": "user",
                "content": final_prompt,
            },
        ],

        temperature=0.2,

        response_format={
            "type": "json_object"
        },
    )

    raw = response.choices[0].message.content

    try:

        return json.loads(raw)

    except Exception:

        return {
            "title": "ملخص المحتوى",
            "summary": raw.strip(),
        }


# =========================================================
# تنظيف اسم الملف
# =========================================================

def sanitize_filename(
    name: str,
    max_length: int = 60
) -> str:

    name = re.sub(
        r'[\\/:*?"<>|\n\r\t]',
        "",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    ).strip()

    return (
        name[:max_length].strip()
        or "Podcast_Markdown"
    )


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "👋 مرحباً بك!\n\n"
        "أرسل رابط YouTube أو TikTok "
        "وسأقوم باستخراج النص وتلخيصه "
        "وإرسال ملف Markdown مرتب لك."
    )


# =========================================================
# معالجة الرابط
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    url = update.message.text.strip()

    if not url.startswith("http"):
        return

    status_message = await update.message.reply_text(
        "📥 جاري فحص الرابط..."
    )

    audio_file = None
    md_filename = None

    try:

        loop = asyncio.get_running_loop()

        # -------------------------------------------------
        # تحديد المنصة
        # -------------------------------------------------

        is_youtube = any(
            domain in url.lower()
            for domain in [
                "youtube.com",
                "youtu.be",
            ]
        )

        # -------------------------------------------------
        # YouTube
        # -------------------------------------------------

        if is_youtube:

            await status_message.edit_text(
                "📥 جاري محاولة استخراج ترجمة YouTube مباشرة..."
            )

            try:

                text_result = await loop.run_in_executor(
                    None,
                    get_youtube_transcript_api,
                    url
                )

                logger.info(
                    "YouTube transcript succeeded."
                )

            except Exception as transcript_error:

                # -------------------------------------------------
                # Fallback:
                # إذا لم توجد ترجمة، ننزل الصوت ونستخدم Whisper
                # -------------------------------------------------

                logger.warning(
                    f"YouTube transcript failed. "
                    f"Fallback to Whisper: {transcript_error}"
                )

                await status_message.edit_text(
                    "🎙️ لا توجد ترجمة مناسبة، "
                    "جاري استخراج الصوت وتحويله إلى نص..."
                )

                audio_file = await loop.run_in_executor(
                    None,
                    download_audio,
                    url,
                    str(update.effective_user.id)
                )

                text_result = await loop.run_in_executor(
                    None,
                    transcribe_audio,
                    audio_file
                )

        # -------------------------------------------------
        # TikTok / Other
        # -------------------------------------------------

        else:

            await status_message.edit_text(
                "📥 جاري تحميل الصوت من المقطع..."
            )

            audio_file = await loop.run_in_executor(
                None,
                download_audio,
                url,
                str(update.effective_user.id)
            )

            await status_message.edit_text(
                "🎙️ جاري تحويل الصوت إلى نص..."
            )

            text_result = await loop.run_in_executor(
                None,
                transcribe_audio,
                audio_file
            )

        # -------------------------------------------------
        # التحقق من النص
        # -------------------------------------------------

        if not text_result or len(text_result.strip()) < 10:

            raise Exception(
                "النص المستخرج قصير جدًا أو فارغ."
            )

        # -------------------------------------------------
        # التلخيص
        # -------------------------------------------------

        await status_message.edit_text(
            "🤖 جاري تحليل المحتوى وتلخيصه..."
        )

        ai_result = await loop.run_in_executor(
            None,
            summarize_text_model,
            text_result
        )

        title = ai_result.get(
            "title",
            "ملخص المحتوى"
        )

        summary_result = ai_result.get(
            "summary",
            ""
        )

        # -------------------------------------------------
        # إنشاء Markdown
        # -------------------------------------------------

        now = time.strftime(
            "%Y-%m-%d_%H-%M"
        )

        safe_title = sanitize_filename(
            title
        )

        md_filename = (
            f"{safe_title}_"
            f"{int(time.time())}.md"
        )

        display_filename = (
            f"{safe_title} - {now}.md"
        )

        with open(
            md_filename,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(
                f"# 📝 {title}\n\n"
            )

            f.write(
                f"**تاريخ المعالجة:** "
                f"{time.strftime('%Y-%m-%d %H:%M')}\n\n"
            )

            f.write(
                f"**الرابط الأصلي:** {url}\n\n"
            )

            f.write(
                "---\n\n"
            )

            f.write(
                "## 📌 زبدة الكلام\n\n"
            )

            f.write(
                f"{summary_result}\n\n"
            )

            f.write(
                "---\n\n"
            )

            f.write(
                "## 📜 النص الكامل المستخرج\n\n"
            )

            f.write(
                f"{text_result}\n"
            )

        # -------------------------------------------------
        # إرسال الملف
        # -------------------------------------------------

        await status_message.edit_text(
            "✅ اكتمل استخراج النص والتلخيص بنجاح."
        )

        with open(
            md_filename,
            "rb"
        ) as f:

            await update.message.reply_document(
                document=f,
                filename=display_filename
            )

    except Exception as e:

        logger.exception(
            "Processing error"
        )

        error_text = str(e)

        # لا نرسل تفاصيل حساسة للمستخدم
        await status_message.edit_text(
            "❌ حدث خطأ أثناء معالجة الرابط.\n\n"
            f"🔎 السبب: {error_text[:700]}"
        )

    finally:

        # حذف الصوت المؤقت
        if audio_file and os.path.exists(
            audio_file
        ):

            try:
                os.remove(audio_file)

            except Exception:
                pass

        # حذف ملف Markdown بعد الإرسال
        if md_filename and os.path.exists(
            md_filename
        ):

            try:
                os.remove(md_filename)

            except Exception:
                pass


# =========================================================
# Main
# =========================================================

def main():

    # تشغيل Health Check
    threading.Thread(
        target=run_health_server,
        daemon=True
    ).start()

    application = (
        Application
        .builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    logger.info(
        "Telegram bot starting..."
    )

    application.run_polling(
        close_loop=False
    )


if __name__ == "__main__":
    main()
```
