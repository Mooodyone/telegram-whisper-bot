import os
import re
import time
import json
import logging
import asyncio
from groq import Groq
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

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

def extract_youtube_id(url: str) -> str:
    """استخراج معرف الفيديو (Video ID) من أي رابط يوتيوب بدقة"""
    url = url.strip()
    patterns = [
        r'(?:v=|\/|embed\/|youtu\.be\/|\/v\/|\/e\/|shorts\/)([^#\&\?^\s]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if len(url) == 11:
        return url
    return None

def get_youtube_transcript_api(url: str) -> str:
    """سحب النص برمجياً عبر الـ API المباشر لتخطي حجب السيرفر وحماية البيانات"""
    video_id = extract_youtube_id(url)
    if not video_id:
        raise Exception("رابط يوتيوب غير صحيح أو تعذر استخراج معرف الفيديو.")
    
    try:
        # جلب قائمة التراجم المتوفرة واختيار اللغة المفضلة تلقائياً
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en'])
        full_text = " ".join([item['text'] for item in transcript_list])
        return full_text.strip()
    except Exception as e:
        logger.error(f"Transcript API Error: {e}")
        raise Exception("تعذر جلب النص التلقائي لهذا الفيديو من خوادم يوتيوب.")

def download_audio_light(url: str, user_id: str) -> str:
    """تحميل الصوت للمنصات الوجيزة كـ تيك توك لتفادي التأثير عليها وثباتها 100%"""
    base_name = f"audio_{user_id}_{int(time.time())}"
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        },
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '64'}]
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_filename

def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as file:
        return groq_client.audio.transcriptions.create(
            file=(file_path, file.read()), model="whisper-large-v3", language="ar", response_format="text", temperature=0.0
        )

def summarize_text_model(text: str) -> dict:
    """إرسال النص لنموذج الإنتاج المعتمد والمتاح حالياً من جروق لمنع توقف التلخيص"""
    text_input = text[:15000] 
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile", # تم تثبيت أقوى نموذج مستقر ومدعوم رسمياً من Groq
        messages=[
            {
                "role": "system",
                "content": (
                    "أنت مساعد محترف وخبير في تلخيص وهيكلة النصوص المفرغة من الصوت. "
                    "مهمتك: اقرأ النص المرسل إليك (وهو تفريغ صوتي بالعربية)، ثم أرجع "
                    "النتيجة بصيغة JSON فقط بدون أي مقدمات أو نصوص إضافية، بالشكل التالي بالضبط:\n"
                    '{"title": "عنوان قصير ومعبّر عن الموضوع الرئيسي (4-8 كلمات، بدون علامات ترقيم زائدة)", '
                    '"summary": "ملخص زبدة الكلام والأفكار الرئيسية على شكل نقاط موجزة ومنظمة ومريحة للقراءة"}\n'
                    "لا تكتب أي شيء خارج كائن الـ JSON."
                )
            },
            {"role": "user", "content": text_input}
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    
    raw = response.choices[0].message.content
    try: 
        return json.loads(raw)
    except: 
        return {"title": "بودكاست مفرغ تلقائياً", "summary": raw.strip()}

def sanitize_filename(name: str, max_length: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_length].strip() or "Podcast_Markdown"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 مرحباً بك! أرسل روابط يوتيوب الطويلة (اقتصادي بدون تحميل) أو روابط تيك توك وسأجلب ملخصاتها فوراً.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"): return
    
    status_message = await update.message.reply_text("📥 جاري فحص المنصة واستخراج النص ذكياً...")
    audio_file = None
    md_filename = None
    try:
        loop = asyncio.get_running_loop()
        
        # فرز ذكي لحماية المنصات ومعالجتها بشكل مستقل
        is_youtube = any(domain in url.lower() for domain in ["youtube.com", "youtu.be", "youtube"])
        
        if is_youtube:
            await status_message.edit_text("📥 جاري سحب النص التلقائي برمجياً من يوتيوب (اقتصادي)...")
            text_result = await loop.run_in_executor(None, get_youtube_transcript_api, url)
        else:
            await status_message.edit_text("📥 جاري معالجة مقطع تيك توك وتفريغ الصوت...")
            audio_file = await loop.run_in_executor(None, download_audio_light, url, str(update.effective_user.id))
            text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)
            
        await status_message.edit_text("🤖 جاري قراءة وتحليل المحتوى وصياغة ملف Markdown...")
        ai_result = await loop.run_in_executor(None, summarize_text_model, text_result)
        title = ai_result["title"]
        summary_result = ai_result["summary"]
        
        now = time.strftime("%Y-%m-%d_%H-%M")
        safe_title = sanitize_filename(title)
        md_filename = f"{safe_title}_{int(time.time())}.md"
        display_filename = f"{safe_title} - {now}.md"
        
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write(f"# 📝 {title}\n\n**تاريخ الصيد المعرفي:** {time.strftime('%Y-%m-%d %H:%M')}\n\n**الرابط الأصلي:** {url}\n\n---\n\n## 📌 زبدة الكلام\n\n{summary_result}\n\n---\n\n## 📜 النص الكامل المستخرج\n\n{text_result}")
            
        await status_message.edit_text("✅ تم اقتناص صيد المعلومة برمجياً واقتصادياً بنجاح!")
        with open(md_filename, "rb") as f:
            await update.message.reply_document(document=f, filename=display_filename)
            
        if os.path.exists(md_filename): os.remove(md_filename)
    except Exception as e:
        logger.error(f"Error: {e}")
        await status_message.edit_text(
            "❌ تعذر صيد النص وتلخيصه تلقائياً.\n"
            "تأكد أن المقطع متاح للعامة ويحتوي على تفريغ نصي مفعّل إذا كان لـ يوتيوب."
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
    application.run_polling(close_loop=False)

if __name__ == '__main__': main()
