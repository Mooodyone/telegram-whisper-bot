import os
import logging
import asyncio
import static_ffmpeg
# تفعيل مسارات FFmpeg مسبقاً لضمان عمل التحميل
static_ffmpeg.add_paths()

import yt_dlp
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

# قاموس مؤقت في ذاكرة السيرفر لحفظ النصوص المفرغة مؤقتاً لغرض التلخيص
user_transcriptions = {}

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
            response_format="text",
            temperature=0.0
        )
    return transcription

def summarize_text(text: str) -> str:
    # التحديث إلى نموذج الإنتاج المستقر والمعتمد حالياً من جروق
    response = groq_client.chat.completions.create(
        model="qwen/qwen3.6-27b",
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 أهلاً بك! أرسل لي رابط فيديو وسأقوم بتفريغه لك فوراً، مع خيار التلخيص والحفظ بصيغة Markdown لاحقاً.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("⚠️ من فضلك أرسل رابطاً صحيحاً.")
        return
        
    status_message = await update.message.reply_text("📥 جاري تحميل المقطع الصوتي وتحويله لنص...")
    try:
        loop = asyncio.get_running_loop()
        
        audio_file = await loop.run_in_executor(None, download_audio, url)
        text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)
        
        if os.path.exists(audio_file):
            try: os.remove(audio_file)
            except: pass
        
        user_id = update.effective_user.id
        user_transcriptions[user_id] = {"text": text_result, "url": url}
        
        keyboard = [[InlineKeyboardButton("📊 تلخيص النص وتحميل ملف MD", callback_data=f"sum_{user_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
                
        if len(text_result) > 4000:
            await status_message.edit_text("✅ تم التفريغ بنجاح! نظراً لطول النص سأرسله على أجزاء:")
            for i in range(0, len(text_result), 4000):
                if i + 4000 >= len(text_result):
                    await update.message.reply_text(text_result[i:i+4000], reply_markup=reply_markup)
                else:
                    await update.message.reply_text(text_result[i:i+4000])
        else:
            await status_message.edit_text(f"📝 **النص المفرغ:**\n\n{text_result}", reply_markup=reply_markup)
            
    except Exception as e:
        logger.error(f"Error: {e}")
        await status_message.edit_text("❌ عذراً، حدث خطأ أثناء معالجة هذا الرابط. تأكد من صلاحية المقطع.")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # فك النص البرمي للـ Callback المحدث
    try:
        data_parts = query.data.split("_")
        user_id = int(data_parts[1])
    except Exception as e:
        logger.error(f"Parsing error in callback data: {e}")
        await query.message.reply_text("❌ حدث خطأ في معالجة طلب الزر التفاعلي.")
        return
    
    if user_id not in user_transcriptions:
        try: await query.edit_message_reply_markup(reply_markup=None)
        except: pass
        await query.message.reply_text("⚠️ عذراً، انتهت صلاحية الجلسة. يرجى إعادة إرسال الرابط مجدداً.")
        return
        
    status_prompt = await query.message.reply_text("🤖 جاري صياغة التلخيص وإنشاء ملف Markdown... انتظر قليلاً.")
    
    try:
        loop = asyncio.get_running_loop()
        text_data = user_transcriptions[user_id]["text"]
        original_url = user_transcriptions[user_id]["url"]
        
        summary_result = await loop.run_in_executor(None, summarize_text, text_data)
        
        md_filename = f"Summary_{user_id}.md"
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write(f"# 📝 تفريغ وتلخيص مقطع مرئي\n\n")
            f.write(f"**الرابط الأصلي:** {original_url}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📌 زبدة الكلام (الملخص التنفيذي)\n\n")
            f.write(f"{summary_result}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📜 النص الكامل المفرغ\n\n")
            f.write(f"{text_data}\n")
            
        await status_prompt.edit_text(f"📊 **الملخص التنفيذي:**\n\n{summary_result}")
        
        with open(md_filename, "rb") as f:
            await query.message.reply_document(
                document=f, 
                filename="Summary_and_Transcription.md", 
                caption="✅ تم تجهيز ملف Markdown بنجاح!"
            )
            
        if os.path.exists(md_filename): os.remove(md_filename)
        user_transcriptions.pop(user_id, None)
        
        try: await query.edit_message_reply_markup(reply_markup=None)
        except: pass
        
    except Exception as e:
        logger.error(f"Callback Error: {e}")
        await status_prompt.edit_text("❌ حدث خطأ غير متوقع أثناء توليد التلخيص؛ يرجى المحاولة لاحقاً.")

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    print("🚀 البوت المستقر والكامل يعمل بنجاح...")
    application.run_polling(close_loop=False)

if __name__ == '__main__':
    main()
