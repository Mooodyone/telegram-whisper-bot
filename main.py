import os
import re
import time
import json
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from groq import Groq
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp
from telegram import Update
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- الإعدادات

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise SystemExit("خطأ: المتغيران TELEGRAM_TOKEN و GROQ_API_KEY مطلوبان في بيئة التشغيل.")

# أوقفت Groq الموديل llama-3.1-8b-instant بتاريخ 2026-08-16.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_MODEL_FALLBACKS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
]

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3")

# لغات الترجمة المفضلة بالترتيب. اتركه فارغاً لقبول أي لغة متاحة.
TRANSCRIPT_LANGS = [
    lang.strip()
    for lang in os.environ.get("TRANSCRIPT_LANGS", "ar,en").split(",")
    if lang.strip()
]

# بروكسي سكني — يوتيوب يحجب عناوين مراكز البيانات (Render / Railway / Fly).
# الخيار الأول: حساب Webshare.
WEBSHARE_USER = os.environ.get("WEBSHARE_USER")
WEBSHARE_PASS = os.environ.get("WEBSHARE_PASS")
# الخيار الثاني: بروكسي عام، مثال: http://user:pass@host:port
YT_PROXY = os.environ.get("YT_PROXY")

# السقوط إلى تحميل الصوت وتفريغه إذا فشل سحب الترجمة من يوتيوب.
# مفيد محلياً، وغالباً بلا فائدة على Render لأن yt-dlp يُحجب أيضاً.
YT_AUDIO_FALLBACK = os.environ.get("YT_AUDIO_FALLBACK", "0") == "1"

MAX_TEXT_FOR_MODEL = 15000
MAX_AUDIO_BYTES = 24 * 1024 * 1024  # حد Groq تقريباً 25 ميغابايت

groq_client = Groq(api_key=GROQ_API_KEY)

SUMMARY_SYSTEM_PROMPT = (
    "أنت مساعد محترف وخبير في تلخيص وهيكلة النصوص المفرغة من الصوت. "
    "مهمتك: اقرأ النص المرسل إليك، ثم أرجع النتيجة بصيغة JSON فقط "
    "بدون أي مقدمات أو نصوص إضافية، بالشكل التالي بالضبط:\n"
    '{"title": "عنوان قصير ومعبّر عن الموضوع الرئيسي (4-8 كلمات)", '
    '"summary": "ملخص زبدة الكلام والأفكار الرئيسية على شكل نقاط موجزة ومنظمة"}\n'
    "لا تكتب أي شيء خارج كائن الـ JSON."
)

# شرح مبسّط لأنواع أخطاء يوتيوب الشائعة
YT_ERROR_HINTS = {
    "IpBlocked": "يوتيوب يحجب عنوان هذا الخادم. الحل: بروكسي سكني.",
    "RequestBlocked": "يوتيوب يحجب عنوان هذا الخادم. الحل: بروكسي سكني.",
    "TranscriptsDisabled": "صاحب الفيديو عطّل الترجمة النصية.",
    "NoTranscriptFound": "لا توجد ترجمة باللغات المطلوبة لهذا الفيديو.",
    "VideoUnavailable": "الفيديو خاص أو محذوف أو محجوب جغرافياً.",
    "AgeRestricted": "الفيديو مقيّد بالعمر ويتطلب تسجيل دخول.",
}


# ------------------------------------------------- خادم فحص الصحة (Render)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, fmt, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


# --------------------------------------------------------------- يوتيوب

# النمط القديم كان يلتقط "/" من "https://" فيعيد معرفاً خاطئاً دائماً.
YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^&\s]*&)*v=|embed/|shorts/|live/|v/|e/))"
    r"([A-Za-z0-9_-]{11})"
)


class TranscriptError(RuntimeError):
    """خطأ في جلب الترجمة، يحمل نوع الاستثناء الأصلي للتشخيص."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


def safe_remove(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.warning(f"تعذر حذف {path}: {e}")


def extract_youtube_id(url: str):
    url = url.strip()
    match = YOUTUBE_ID_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return None


def _build_transcript_api():
    """يدعم الإصدار 1.0+ (كائني) والإصدارات الأقدم (ساكن)."""
    if not hasattr(YouTubeTranscriptApi, "fetch"):
        return None  # إصدار قديم من المكتبة

    if WEBSHARE_USER and WEBSHARE_PASS:
        try:
            from youtube_transcript_api.proxies import WebshareProxyConfig
            logger.info("تفعيل بروكسي Webshare السكني.")
            return YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(
                    proxy_username=WEBSHARE_USER,
                    proxy_password=WEBSHARE_PASS,
                )
            )
        except Exception as e:
            logger.warning(f"تعذر تفعيل Webshare: {e}")

    if YT_PROXY:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            logger.info("تفعيل البروكسي العام.")
            return YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(
                    http_url=YT_PROXY, https_url=YT_PROXY
                )
            )
        except Exception as e:
            logger.warning(f"تعذر تفعيل البروكسي العام: {e}")

    return YouTubeTranscriptApi()


def get_youtube_transcript_api(url: str) -> str:
    video_id = extract_youtube_id(url)
    if not video_id:
        raise TranscriptError("InvalidURL", "رابط يوتيوب غير صحيح أو تعذر استخراج المعرف.")

    try:
        api = _build_transcript_api()
        if api is None:
            items = YouTubeTranscriptApi.get_transcript(
                video_id, languages=TRANSCRIPT_LANGS or ["ar", "en"]
            )
            parts = [item["text"] for item in items]
        else:
            if TRANSCRIPT_LANGS:
                fetched = api.fetch(video_id, languages=TRANSCRIPT_LANGS)
            else:
                # أي لغة متاحة
                listing = api.list(video_id)
                fetched = next(iter(listing)).fetch()
            parts = [snippet.text for snippet in fetched]
    except TranscriptError:
        raise
    except Exception as e:
        kind = type(e).__name__
        hint = YT_ERROR_HINTS.get(kind, "")
        logger.error(f"Transcript API [{video_id}] {kind}: {e}")
        raise TranscriptError(kind, f"تعذر جلب النص من يوتيوب ({kind}). {hint}".strip())

    full_text = " ".join(p.strip() for p in parts if p and p.strip())
    if not full_text:
        raise TranscriptError("EmptyTranscript", "التفريغ النصي لهذا الفيديو فارغ.")
    return full_text


# ----------------------------------------------------- تحميل وتفريغ الصوت

def download_audio_light(url: str, user_id: str) -> str:
    base_name = f"audio_{user_id}_{int(time.time())}"
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{base_name}.%(ext)s",
        "nocheckcertificate": True,
        "geo_bypass": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
    }
    if YT_PROXY:
        ydl_opts["proxy"] = YT_PROXY

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    output_filename = f"{base_name}.mp3"
    if not os.path.exists(output_filename):
        candidates = [f for f in os.listdir(".") if f.startswith(base_name)]
        if not candidates:
            raise RuntimeError("فشل تحميل الملف الصوتي من المنصة.")
        output_filename = candidates[0]

    size = os.path.getsize(output_filename)
    if size > MAX_AUDIO_BYTES:
        safe_remove(output_filename)
        raise RuntimeError(
            f"حجم المقطع {size // (1024 * 1024)} ميغابايت — أكبر من حد خدمة التفريغ."
        )

    return output_filename


def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=(os.path.basename(file_path), f.read()),
            model=WHISPER_MODEL,
            response_format="text",
            temperature=0.0,
        )
    text = result if isinstance(result, str) else getattr(result, "text", "")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("لم يتم استخراج أي كلام من المقطع الصوتي.")
    return text


# ------------------------------------------------------------- التلخيص

def _call_groq_chat(model: str, text_input: str) -> str:
    response = groq_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": text_input},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    # choices قائمة وليست كائناً — الكود الأصلي كان يرمي AttributeError هنا.
    return response.choices[0].message.content or ""


def summarize_text_model(text: str) -> dict:
    text_input = text[:MAX_TEXT_FOR_MODEL]
    fallback = {"title": "مقطع مفرغ تلقائياً", "summary": text_input[:1000]}

    candidates = [GROQ_MODEL] + [m for m in GROQ_MODEL_FALLBACKS if m != GROQ_MODEL]
    raw = None

    for model in candidates:
        try:
            raw = _call_groq_chat(model, text_input)
            if model != candidates[0]:
                logger.warning(f"تم التحويل إلى الموديل البديل: {model}")
            break
        except Exception as e:
            status = getattr(e, "status_code", None)
            logger.error(f"Groq [{model}] {type(e).__name__}: {e}")
            if status in (400, 404):
                continue  # الموديل غير متاح أو موقوف → جرّب التالي
            return fallback  # مشكلة شبكة أو حصة → لا فائدة من التكرار

    if raw is None:
        logger.error("فشلت كل الموديلات المرشحة للتلخيص.")
        return fallback

    try:
        data = json.loads(raw)
        return {
            "title": (data.get("title") or fallback["title"]).strip(),
            "summary": (data.get("summary") or "").strip() or fallback["summary"],
        }
    except (json.JSONDecodeError, AttributeError):
        return {
            "title": fallback["title"],
            "summary": raw.strip() or fallback["summary"],
        }


# --------------------------------------------------------------- أدوات

def sanitize_filename(name: str, max_length: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_length].strip() or "Podcast_Markdown"


# ------------------------------------------------------------ المعالجات

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 مرحباً بك!\n"
        "أرسل رابط يوتيوب (سحب النص مباشرة بدون تحميل) "
        "أو رابط تيك توك (تحميل وتفريغ صوتي) وسأعيد لك ملف Markdown مُلخّصاً."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        return

    status_message = await update.message.reply_text("📥 جاري فحص المنصة واستخراج النص...")
    audio_file = None
    md_filename = None

    try:
        loop = asyncio.get_running_loop()
        is_youtube = any(d in url.lower() for d in ("youtube.com", "youtu.be"))

        if is_youtube:
            await status_message.edit_text("📥 جاري سحب النص التلقائي من يوتيوب...")
            try:
                text_result = await loop.run_in_executor(
                    None, get_youtube_transcript_api, url
                )
            except TranscriptError as te:
                if not YT_AUDIO_FALLBACK:
                    raise
                logger.warning(f"سقوط إلى التفريغ الصوتي بسبب: {te.kind}")
                await status_message.edit_text(
                    "🎧 لا توجد ترجمة جاهزة — جاري تحميل الصوت وتفريغه..."
                )
                audio_file = await loop.run_in_executor(
                    None, download_audio_light, url, str(update.effective_user.id)
                )
                text_result = await loop.run_in_executor(
                    None, transcribe_audio, audio_file
                )
        else:
            await status_message.edit_text("📥 جاري تحميل المقطع وتفريغ الصوت...")
            audio_file = await loop.run_in_executor(
                None, download_audio_light, url, str(update.effective_user.id)
            )
            text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)

        await status_message.edit_text("🤖 جاري تحليل المحتوى وصياغة ملف Markdown...")
        ai_result = await loop.run_in_executor(None, summarize_text_model, text_result)

        title = ai_result.get("title", "مقطع مفرغ تلقائياً")
        summary_result = ai_result.get("summary", "")

        now = time.strftime("%Y-%m-%d %H:%M")
        safe_title = sanitize_filename(title)
        md_filename = f"{safe_title}_{int(time.time())}.md"
        display_filename = f"{safe_title} - {time.strftime('%Y-%m-%d_%H-%M')}.md"

        with open(md_filename, "w", encoding="utf-8") as f:
            f.write(
                f"# 📝 {title}\n\n"
                f"**تاريخ الصيد المعرفي:** {now}\n\n"
                f"**الرابط الأصلي:** {url}\n\n"
                f"---\n\n## 📌 زبدة الكلام\n\n{summary_result}\n\n"
                f"---\n\n## 📜 النص الكامل المستخرج\n\n{text_result}\n"
            )

        await status_message.edit_text("✅ تم اقتناص المعلومة بنجاح!")
        with open(md_filename, "rb") as f:
            await update.message.reply_document(document=f, filename=display_filename)

    except Exception as e:
        logger.exception("فشلت معالجة الطلب")
        try:
            await status_message.edit_text(f"❌ تعذر إتمام العملية.\n{str(e)[:300]}")
        except Exception:
            pass
    finally:
        safe_remove(audio_file)
        safe_remove(md_filename)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        # نسخة أخرى تسحب التحديثات — يحدث عادة أثناء تداخل النشر ويزول وحده.
        logger.warning("Conflict: نسخة أخرى من البوت تعمل بنفس التوكن.")
        return
    if isinstance(err, NetworkError):
        logger.warning(f"NetworkError مؤقت: {err}")
        return
    logger.error("خطأ غير معالج", exc_info=err)


def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_error_handler(error_handler)

    logger.info(
        f"البوت يعمل — التلخيص: {GROQ_MODEL} | "
        f"لغات الترجمة: {TRANSCRIPT_LANGS or 'أي لغة'} | "
        f"بروكسي: {'نعم' if (WEBSHARE_USER or YT_PROXY) else 'لا'}"
    )
    application.run_polling(close_loop=False, drop_pending_updates=True)


if __name__ == "__main__":
    main()
