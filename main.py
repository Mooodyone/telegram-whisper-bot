import os
import logging
import asyncio
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
        try:
            os.remove(output_filename)
        except:
            pass
            
    # إعدادات متطورة لدعم الروابط وتجاوز الحظر
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audio',
        'update_trusted_packages': True, # تحديث الحزم الأمنية تلقائياً
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 أهلاً بك! أرسل لي رابط فيديو وسأقوم بتفريغه لك فوراً مجاناً.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("⚠️ من فضلك أرسل رابطاً صحيحاً يبدأ بـ http أو https.")
        return
        
    status_message = await update.message.reply_text("📥 جاري تحميل المقطع الصوتي... يرجى الانتظار.")
    try:
        loop = asyncio.get_running_loop()
        audio_file = await loop.run_in_executor(None, download_audio, url)
        
        await status_message.edit_text("⚡ جاري معالجة الصوت وتفريغه عبر Groq API...")
        text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)
        
        if os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except:
                pass
                
        if len(text_result) > 4000:
            await status_message.edit_text("✅ تم التفريغ بنجاح! نظراً لطول النص سأرسله على أجزاء:")
            for i in range(0, len(text_result), 4000):
                await update.message.reply_text(text_result[i:i+4000])
        else:
            await status_message.edit_text(f"📝 **النص المفرغ:**\n\n{text_result}")
    except Exception as e:
        logger.error(f"Error occurred: {e}")
        await status_message.edit_text("❌ عذراً، حدث خطأ أثناء معالجة هذا الرابط. تأكد من صلاحية المقطع الصوتي.")

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 البوت مستعد ويعمل بنجاح...")
    application.run_polling(close_loop=False)

if __name__ == '__main__':
    main()
