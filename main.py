import os
import re
import time
import json
import logging
import asyncio
from groq import Groq
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# إعدادات المراقبة والـ Logs
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# جلب المفاتيح البيئية بأمان
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), HealthCheckHandler)
    server.serve_forever()

def get_youtube_transcript(url: str) -> str:
    """
    سحب النص التلقائي الجاهز من يوتيوب مباشرة بدون تحميل أي ملف صوتي.
    توفير 100% لبيانات باقة المستخدم وسيرفر Render.
    """
    ydl_opts = {
        'writeautomaticsub': True,  # طلب الترجمة التلقائية المكتوبة ذكياً بالخلفية
        'subtitlesformat': 'srt',
        'skip_download': True,      # منع تحميل الفيديو أو الصوت نهائياً لحماية الباقة
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        subtitles = info.get('subtitles') or info.get('automatic_captions')
        
        # البحث عن النص باللغة العربية (سواء مرفوع يدوياً أو مولد تلقائياً)
        target_lang = None
        if subtitles:
            if 'ar' in subtitles:
                target_lang = 'ar'
            elif 'ar-sa' in subtitles:
                target_lang = 'ar-sa'
                
        if target_lang and subtitles[target_lang]:
            # الحصول على رابط ملف النص الخفيف جداً
            sub_url = subtitles[target_lang][0]['url']
            import requests
            response = requests.get(sub_url)
            
            # تنظيف نص الـ SRT من التوقيتات والأرقام البرمجية لجعله نصاً مقروءاً
            clean_text = response.text
            clean_text = re.sub(r'\d+\n\d\d:\d\d:\d\d.*\n', '', clean_text)
            clean_text = re.sub(r'<[^>]*>', '', clean_text)
            clean_text = re.sub(r'^\s*$', '', clean_text, flags=re.MULTILINE)
            # دمج السطور المتكررة الناتجة عن محاذاة الفيديو
            lines = clean_text.split('\n')
            final_lines = []
            for line in lines:
                line = line.strip()
                if line and (not final_lines or final_lines[-1] != line):
                    final_lines.append(line)
            
            return " ".join(final_lines).strip()
            
    raise Exception("لم يتم العثور على نص تلقائي أو ترجمة جاهزة باللغة العربية في خوادم يوتيوب لهذا المقطع.")

def download_audio_light(url: str, user_id: str) -> str:
    """تحميل الصوت للمنصات الأخرى الوجيزة (مثل تيك توك) فقط لأن حجمها صغير جداً"""
    base_name = f"audio_{user_id}_{int(time.time())}"
    output_filename = f"{base_name}.mp3"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': base_name,
        'quiet': True,
        'no_warnings': True,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '64'}]
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_filename

def transcribe_audio(file_path: str) -> str:
    """تفريغ ملفات الصوت القصيرة عبر Whisper"""
    with open(file_path, "rb") as file:
        return groq_client.audio.transcriptions.create(
            file=(file_path, file.read()), model="whisper-large-v3", language="ar", response_format="text", temperature=0.0
        )

def summarize_and_title(text: str) -> dict:
    """إرسال النص إلى Llama 3.1 لاستخراج كائن التلخيص والعنوان منسقاً"""
    # حماية حد كائن الـ Context بقراءة أول 15000 حرف من النص كحد أقصى للبودكاست الطويل
    text_input = text[:15000] 
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {
                "role": "system",
                "content": (
                    "أنت مساعد محترف وخبير في تلخيص وهيكلة النصوص المفرغة من الصوت. "
                    "مهمتك: اقرأ النص المرسل إليك (وهو تفريغ صوتي لـ بودكاست بالعربية)، ثم أرجع "
                    "النتيجة بصيغة JSON فقط بدون أي نص إضافي، بالشكل التالي بالضبط:\n"
                    '{"title": "عنوان قصير ومعبّر عن الموضوع الرئيسي (4-8 كلمات، بدون علامات ترقيم زائدة)", '
                    '"summary": "ملخص زبدة الكلام والأفكار والفوائد الرئيسية المقتنصة على شكل نقاط موجزة ومنظمة جداً بمظهر مريح ومحفز للقراءة"}\n'
                    "لا تكتب أي شيء خارج كائن الـ JSON نهائياً."
                )
            },
            {"role": "user", "content": text_input}
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    try: 
        return json.loads(response.choices[0].message.content)
    except: 
        # حل بديل إذا تضرر الـ JSON
        return {"title": "بودكاست مفرغ نصياً برمجياً", "summary": response.choices[0].message.content}

def sanitize_filename(name: str, max_length: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_length].strip() or "Podcast_File"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بك في نظام 'صيد المعلومة الاقتصادي' المتطور!\n\n"
        "هذا الإصدار مصمم خصيصاً لحماية باقة إنترنت جوالك وسيرفر Render. "
        "عند إرسال رابط يوتيوب طويل، لن يتم تحميل أي ملفات صوتية ثقيلة (0% استهلاك بيانات)، "
        "بل سيسحب البوت النص الجاهز من يوتيوب برمجياً ويلخصه لك في ملف Markdown منسق فوراً وبثوانٍ!"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http"): 
        await update.message.reply_text("⚠️ من فضلك أرسل رابطاً صحيحاً يبدأ بـ http.")
        return
    
    status_message = await update.message.reply_text("⚡ جاري قراءة الرابط واستخراج النص برمجياً من خوادم يوتيوب (اقتصادي)...")
    audio_file = None
    md_filename = None
    try:
        loop = asyncio.get_running_loop()
        
        # الفرز الذكي لحماية الباقة وتفادي الحظر
        if "youtube.com" in url or "youtu.be" in url:
            # يوتيوب طويل: نسحب الملف النصي الصغير الجاهز فوراً وبـ 0% تحميل صوتي
            text_result = await loop.run_in_executor(None, get_youtube_transcript, url)
        else:
            # المنصات القصيرة مثل تيك توك: نعتمد التحميل الصوتي الوجيز الخفيف
            await status_message.edit_text("📥 جاري معالجة مقطع تيك توك الخفيف وتحميله...")
            audio_file = await loop.run_in_executor(None, download_audio_light, url, str(update.effective_user.id))
            text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)
            
        await status_message.edit_text("🤖 جاري تحليل النص وصياغة التلخيص التنفيذي والعنوان تلقائياً...")
        ai_result = await loop.run_in_executor(None, summarize_and_title, text_result)
        title = ai_result["title"]
        summary_result = ai_result["summary"]
        
        # بناء وهيكلة مستند المارك داون (.md)
        now = time.strftime("%Y-%m-%d_%H-%M")
        safe_title = sanitize_filename(title)
        md_filename = f"{safe_title}_{int(time.time())}.md"
        display_filename = f"{safe_title} - {now}.md"
        
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write(f"# 📝 {title}\n\n")
            f.write(f"**تاريخ الصيد المعرفي:** {time.strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write(f"**الرابط المصدر:** {url}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📌 زبدة الكلام (الملخص التنفيذي)\n\n")
            f.write(f"{summary_result}\n\n")
            f.write(f"---\n\n")
            f.write(f"## 📜 النص الكامل المستخرج برمجياً\n\n")
            f.write(f"{text_result}\n")
            
        await status_message.edit_text("✅ تم اقتناص صيد المعلومة برمجياً بنجاح وبدون استهلاك بياناتك!")
        with open(md_filename, "rb") as f:
            await update.message.reply_document(
                document=f, 
                filename=display_filename,
                caption="📊 صيدك الثمين جاهز! الملخص المنسق في الأعلى والنص الكامل بالأسفل لحفظه بـ Notion."
            )
            
        if os.path.exists(md_filename): os.remove(md_filename)
    except Exception as e:
        logger.error(f"Error: {e}")
        await status_message.edit_text(
            "❌ عذراً، تعذر صيد النص تلقائياً.\n"
            "تأكد أن المقطع يحتوي على تفريغ نصي أو ترجمة تلقائية (Subtitles/Transcript) مفعّلة في يوتيوب."
        )
    finally:
        if audio_file and os.path.exists(audio_file): 
            try: os.remove(audio_file)
            except: pass

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🚀 البوت الاقتصادي الشامل يعمل بنجاح ومستقر...")
    application.run_polling(close_loop=False)

if __name__ == '__main__': 
    main()
