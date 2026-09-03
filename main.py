import os
import logging
import asyncio
import static_ffmpeg
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

def download_audio(url: str, output_filename="audio.mp3") -> str:
    if os.path.exists(output_filename):
        try: os.remove(output_filename)
        except: pass
            
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audio',
        'no_check_certificate': True,
        'geo_bypass': True,
        'extractor_args': {'tiktok': {'app_version': ['20.2.1'], 'manifest_app_version': ['20.2.1']}},
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        },
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_filename

def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(file_path, file.read()),
            model="whisper-large-v3",
            response_format="text",
            temperature=0.0
        )
    return transcription

# دالة جديدة كلياً لتلخيص النص باستخدام نموذج Llama 3 القوي والمجاني عبر Groq
def summarize_text(text: str) -> str:
    response = groq_client.chat.completions.create(
        model="llama3-8b-8192",
        messages=[
            {
                "role": "system",
                "content": "أنت مساعد خبير في تلخيص النصوص. قم بتلخيص النص المرسل إليك باللغة العربية بدقة، واستخرج أهم الأفكار الرئيسية على شكل نقاط موجزة ومنظمة ومريحة للقراءة."
            },
            {"role": "user", "content": text}
        ],
        temperature=0.3
    )
    return response.choices[0].message.content

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 أهلاً بك! أرسل لي رابط فيديو وسأقوم بتفريغه، تلخيصه، وتحويله لملف Markdown فوراً مجاناً.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("⚠️ من فضلك أرسل رابطاً صحيحاً.")
        return
        
    status_message = await update.message.reply_text("📥 جاري تحميل المقطع الصوتي...")
    try:
        loop = asyncio.get_running_loop()
        
        # 1. تحميل الصوت
        audio_file = await loop.run_in_executor(None, download_audio, url)
        
        # 2. التفريغ الصوتي
        await status_message.edit_text("⚡ جاري تفريغ الصوت عبر Whisper...")
        text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)
        
        # تنظيف ملف الصوت فوراً لتوفير مساحة السيرفر
        if os.path.exists(audio_file):
            try: os.remove(audio_file)
            except: pass
            
        # 3. التلخيص الذكي
        await status_message.edit_text("🤖 جاري قراءة النص وتلخيصه عبر ذكاء Groq...")
        summary_result = await loop.run_in_executor(None, summarize_text, text_result)
        
        # 4. إنشاء ملف المارك داون (.md) محلياً
        md_filename = "Summary_and_Transcription.md"
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write(f"# 📝 تفريغ وتلخيص مقطع مرئي\n\n")
            f.write(f"**الرابط الأصلي:** {url}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📌 زبدة الكلام (الملخص التنفيذي)\n\n")
            f.write(f"{summary_result}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📜 النص الكامل المفرغ\n\n")
            f.write(f"{text_result}\n")
            
        # 5. إرسال الملخص كنص مباشر للمعاينة السريعة
        await status_message.edit_text(f"📊 **الملخص السريع:**\n\n{summary_result}\n\n⏳ جاري رفع ملف الـ Markdown الكامل...")
        
        # 6. إرسال ملف المارك داون للمستخدم
        with open(md_filename, "rb") as f:
            await update.message.reply_document(document=f, filename=md_filename, caption="✅ تم تجهيز ملف Markdown يحتوي على التفريغ الكامل والتلخيص المنظم!")
            
        # تنظيف ملف المارك داون من السيرفر
        if os.path.exists(md_filename):
            os.remove(md_filename)
            
    except Exception as e:
        logger.error(f"Error occurred: {e}")
        await status_message.edit_text("❌ عذراً، حدث خطأ أثناء معالجة هذا الرابط.")

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 البوت المطور يعمل بنجاح...")
    application.run_polling(close_loop=False)

if __name__ == '__main__':
    main()
