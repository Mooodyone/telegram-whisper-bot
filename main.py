import os
import time
import logging
import asyncio
import static_ffmpeg
# تفعيل مسارات FFmpeg مسبقاً لضمان عمل التحميل في خوادم ريندر
static_ffmpeg.add_paths()

import yt_dlp
from groq import Groq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

def download_audio(url: str, user_id: str) -> str:
    """
    تحميل الصوت مع اسم ملف فريد لكل طلب (مبني على معرف المستخدم + الوقت)
    لتفادي تعارض الملفات لو أكثر من شخص أرسل رابط بنفس الوقت تقريباً.
    """
    base_name = f"audio_{user_id}_{int(time.time() * 1000)}"
    output_filename = f"{base_name}.mp3"

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': base_name,
        'no_check_certificate': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        },
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_filename

def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(file_path, file.read()),
            model="whisper-large-v3",
            language="ar",
            response_format="text",
            temperature=0.0
        )
    return transcription

def summarize_text(text: str) -> str:
    # نموذج مستقر مدعوم حالياً (llama-3.1-8b-instant تم إيقافه من Groq بتاريخ 16 أغسطس 2026)
    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {
                "role": "system",
                "content": (
                    "أنت مساعد محترف وخبير في تلخيص وهيكلة النصوص المفرغة من الصوت. "
                    "قم بتلخيص النص المرسل إليك باللغة العربية بدقة، واستخرج 'زبدة الكلام' "
                    "والأفكار والفوائد الرئيسية على شكل نقاط موجزة، منسقة ومنظمة بشكل مريح جداً للقراءة."
                )
            },
            {"role": "user", "content": text}
        ],
        temperature=0.3
    )
    return response.choices[0].message.content

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بك! أرسل لي رابط فيديو وسأرسل لك ملف Markdown يحتوي على الملخص والنص الكامل مباشرة."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("⚠️ من فضلك أرسل رابطاً صحيحاً.")
        return

    user_id = str(update.effective_user.id)
    status_message = await update.message.reply_text("📥 جاري تحميل المقطع الصوتي وتحويله لنص...")
    audio_file = None
    md_filename = None
    try:
        loop = asyncio.get_running_loop()

        # 1) تحميل الصوت
        audio_file = await loop.run_in_executor(None, download_audio, url, user_id)

        # 2) تفريغ الصوت إلى نص
        text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)

        # 3) تلخيص النص تلقائياً بدون أي تدخل من المستخدم
        await status_message.edit_text("🤖 جاري تلخيص النص وتجهيز ملف Markdown...")
        summary_result = await loop.run_in_executor(None, summarize_text, text_result)

        # 4) بناء ملف Markdown: الملخص أولاً، ثم النص الكامل أسفله
        md_filename = f"Summary_{user_id}_{int(time.time() * 1000)}.md"
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write(f"# 📝 تفريغ وتلخيص مقطع مرئي\n\n")
            f.write(f"**الرابط الأصلي:** {url}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📌 زبدة الكلام (الملخص التنفيذي)\n\n")
            f.write(f"{summary_result}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📜 النص الكامل المفرغ\n\n")
            f.write(f"{text_result}\n")

        # 5) إرسال ملف الـ Markdown مباشرة، بدون أزرار وبدون أي ضغط من المستخدم
        await status_message.edit_text("✅ تم! هذا ملف التفريغ والتلخيص:")
        with open(md_filename, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="Summary_and_Transcription.md",
                caption="📊 يحتوي الملف على الملخص التنفيذي أعلى الصفحة، والنص الكامل المفرغ أسفله."
            )

    except Exception as e:
        logger.error(f"Error: {e}")
        await status_message.edit_text("❌ عذراً، حدث خطأ أثناء معالجة هذا الرابط. تأكد من صلاحية المقطع.")
    finally:
        if audio_file and os.path.exists(audio_file):
            try: os.remove(audio_file)
            except: pass
        if md_filename and os.path.exists(md_filename):
            try: os.remove(md_filename)
            except: pass

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 البوت المستقر والكامل يعمل بنجاح...")
    application.run_polling(close_loop=False)

if __name__ == '__main__':
    main()
