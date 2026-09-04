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
    """استخراج معرف الفيديو (Video ID) من أي رابط يوتيوب"""
    pattern = r'(?:v=|\/|embed\/|youtu\.be\/|\/v\/|\/e\/|watch\?v_=|&v=)([^#\&\?]*华?)'
    match = re.search(pattern, url)
    if match and len(match.group(1)) == 11:
        return match.group(1)
    # محاولة إضافية للروابط المختصرة أو غير التقليدية
    parsed = re.findall(r'v([^#\&\?]{11})', url)
    if parsed: return parsed[0]
    parsed_shorts = re.findall(r'shorts\/([^#\&\?]{11})', url)
    if parsed_shorts: return parsed_shorts[0]
    return None

def get_youtube_transcript_api(url: str) -> str:
    """سحب النص برمجياً عبر الـ API المباشر لتخطي حجب السيرفر وحماية الباقة"""
    video_id = extract_youtube_id(url)
    if not video_id:
        raise Exception("رابط يوتيوب غير صحيح أو تعذر استخراج معرف الفيديو.")
    
    try:
        # طلب قائمة النصوص المتوفرة للفيديو
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        # محاولة جلب النص باللغة العربية أولاً
        try:
            transcript = transcript_list.find_transcript(['ar'])
        except:
            # إذا لم تكن العربية متوفرة، جلب النص التلقائي وترجمته للعربية برمجياً فوراً
            transcript = transcript_list.find_transcript(['en']).translate('ar')
            
        data = transcript.fetch()
        full_text = " ".join([item['text'] for item in data])
        return full_text.strip()
    except Exception as e:
        logger.error(f"Transcript API Error: {e}")
        raise Exception("تعذر جلب النص التلقائي لهذا الفيديو من خوادم يوتيوب.")

def download_audio_light(url: str, user_id: str) -> str:
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
    with open(file_path, "rb") as file:
        return groq_client.audio.transcriptions.create(
            file=(file_path, file.read()), model="whisper-large-v3", language="ar", response_format="text", temperature=0.0
        )

def summarize_and_title(text: str) -> dict:
    text_input = text[:15000] 
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {
                "role": "system",
                "content": "أرجع النتيجة بصيغة JSON فقط بدون أي مقدمات: {\"title\": \"عنوان البودكاست الأصلي\", \"summary\": \"الملخص التنفيذي المنظم في نقاط ومريح للقراءة\"}"
            },
            {"role": "user", "content": text_input}
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    try: return json.loads(response.choices.message.content)
    except: return {"title": "بودكاست مفرغ برمجياً", "summary": response.choices.message.content}

def sanitize_filename(name: str, max_length: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_length].strip() or "Podcast_Markdown"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 مرحباً بك! أرسل روابط يوتيوب الطويلة وسأجلب نصوصها وملخصاتها فوراً دون استهلاك باقتك وبحماية كاملة من الحظر.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http"): return
    
    status_message = await update.message.reply_text("📥 جاري فحص الرابط واستخراج النص الذكي (اقتصادي بدون حظر)...")
    audio_file = None
    md_filename = None
    try:
        loop = asyncio.get_running_loop()
        
        # إذا كان يوتيوب، نستخدم الـ API المباشر لكسر الحظر وتوفير البيانات
        if "youtube.com" in url or "youtu.be" in url:
            text_result = await loop.run_in_executor(None, get_youtube_transcript_api, url)
        else:
            # المنصات الأخرى (تيك توك): إذا كانت محظورة في ريندر، يمكنك رفع مقطع الصوت مباشرة للبوت
            audio_file = await loop.run_in_executor(None, download_audio_light, url, str(update.effective_user.id))
            text_result = await loop.run_in_executor(None, transcribe_audio, audio_file)
            
        await status_message.edit_text("🤖 جاري تحليل النص وصياغة التلخيص التنفيذي كـ ملف Markdown...")
        ai_result = await loop.run_in_executor(None, summarize_and_title, text_result)
        title = ai_result["title"]
        summary_result = ai_result["summary"]
        
        now = time.strftime("%Y-%m-%d_%H-%M")
        safe_title = sanitize_filename(title)
        md_filename = f"{safe_title}_{int(time.time())}.md"
        display_filename = f"{safe_title} - {now}.md"
        
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write(f"# 📝 {title}\n\n**تاريخ الصيد:** {time.strftime('%Y-%m-%d %H:%M')}\n\n**الرابط:** {url}\n\n---\n\n## 📌 زبدة الكلام\n\n{summary_result}\n\n---\n\n## 📜 النص المستخرج كاملاً\n\n{text_result}")
            
        await status_message.edit_text("✅ تم اقتناص صيد المعلومة برمجياً واقتصادياً!")
        with open(md_filename, "rb") as f:
            await update.message.reply_document(document=f, filename=display_filename)
            
        if os.path.exists(md_filename): os.remove(md_filename)
    except Exception as e:
        logger.error(f"Error: {e}")
        await status_message.edit_text(
            "❌ تعذر صيد النص تلقائياً من يوتيوب.\n"
            "تأكد أن المقطع يحتوي على تفريغ نصي أو ترجمة مفعّلة في موقع يوتيوب."
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
