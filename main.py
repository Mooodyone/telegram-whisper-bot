import os
import logging
import asyncio
import static_ffmpeg
# تفعيل مسارات FFmpeg مسبقاً لمنع أي تداخل في السيرفر
static_ffmpeg.add_paths()

import yt_dlp
from groq import Groq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# إعدادات المراقبة والـ Logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# جلب المفاتيح من بيئة العمل السحابية بأمان
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

# خادم وهمي (Health Check) لإبقاء سيرفر ريندر مستيقظاً ومستقراً
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

# دالة تحميل الصوت المطورة لتخطي جדרان الحماية والحجب السحابي
def download_audio(url: str, output_filename="audio.mp3") -> str:
    if os.path.exists(output_filename):
        try:
            os.remove(output_filename)
        except:
            pass
            
    # إعدادات قصوى لتخطي حجب السيرفرات ومحاكاة متصفح حقيقي بالكامل
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audio',
        'no_check_certificate': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
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

# دالة تحويل الصوت إلى نص عبر Whisper
def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(file_path, file.read()),
            model="whisper-large-v3",
            response_format="text",
            temperature=0.0
        )
    return transcription

# دالة التلخيص الذكي باستخدام النموذج الجديد المعتمد Llama 3.1
def summarize_text(text: str) -> str:
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
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
    return response.choices.message.content

# أمر البدء للبوت (/start)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بك في نظام التفريغ والتلخيص الذكي المطور!\n\n"
        "أرسل لي رابط مقطع مرئي (يوتيوب، تيك توك، إلخ) وسأقوم بالآتي تلقائياً:\n"
        "1️⃣ تحميل المقطع وتفريغه صوتياً بدقة.\n"
        "2️⃣ كتابة ملخص تنفيذي ذكي لأهم الأفكار.\n"
        "3️⃣ تجهيز ملف Markdown (.md) كامل ومرتب لحفظه في مفكرتك."
    )

# معالجة الرسائل والروابط الواردة
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("⚠️ من فضلك أرسل رابطاً صحيحاً يبدأ بـ http أو https.")
        return
        
    status_message = await update.message.reply_text("📥 جاري تحميل المقطع الصوتي... يرجى الانتظار.")
    try:
        loop = asyncio.get_running_loop()
        
        # المرحلة 1: تحميل ملف الصوت واختراق القيود
        audio_file = await loop.run_in_executor(None, download_audio, url)
        
        # المرحلة 2: تحويل الصوت لنص كامل
        await status_message.edit_text("⚡ جاري تفريغ الصوت وتحويله إلى نص بدقة عبر Whisper...")
        text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)
        
        # تنظيف ملف الصوت من السيرفر فوراً لتوفير مساحة الذاكرة
        if os.path.exists(audio_file):
            try: os.remove(audio_file)
            except: pass
            
        # المرحلة 3: تلخيص النص المستخرج
        await status_message.edit_text("🤖 جاري قراءة وتحليل النص وصياغة التلخيص الذكي عبر نموذج Llama 3.1...")
        summary_result = await loop.run_in_executor(None, summarize_text, text_result)
        
        # المرحلة 4: إنشاء وهيكلة ملف المارك داون (.md)
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
            
        # المرحلة 5: إرسال النص الملخص على التليجرام كمعاينة فورية سريعة
        await status_message.edit_text(f"📊 **الملخص الذكي السريع:**\n\n{summary_result}\n\n⏳ جاري رفع وتجهيز ملف الـ Markdown الكامل لحفظه...")
        
        # المرحلة 6: إرسال ملف المارك داون القابل للحفظ والتخزين
        with open(md_filename, "rb") as f:
            await update.message.reply_document(
                document=f, 
                filename=md_filename, 
                caption="✅ تم تجهيز الملف بنجاح! يحتوي على التفريغ الكامل والتلخيص المنظم لتخزينه في Notion أو Obsidian."
            )
            
        # حذف الملف المحلي للمارك داون لتنظيف السيرفر
        if os.path.exists(md_filename):
            os.remove(md_filename)
            
    except Exception as e:
        logger.error(f"Error occurred: {e}")
        await status_message.edit_text(
            "❌ عذراً، حدث خطأ أثناء معالجة هذا الرابط.\n"
            "تأكد من صلاحية المقطع الصوتي وأن الرابط متاح للعامة."
        )

# الدالة الأساسية لتشغيل البوت
def main():
    # تشغيل الخادم الوهمي في الخلفية للـ Health Check
    threading.Thread(target=run_health_server, daemon=True).start()
    
    # بناء تطبيق البوت وضبط المعالجات
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🚀 البوت المطور يعمل بنجاح ومستعد لاستقبال الروابط...")
    application.run_polling(close_loop=False)

if __name__ == '__main__':
    main()
