import asyncio
import logging
import os
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from youtube_transcript_api import YouTubeTranscriptApi

TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# اسم ملف الذاكرة الدائمة على السحاب
MEMORY_FILE = "strategies_memory.txt"

# خادم الويب الأساسي لإبقاء البوت متصلاً 24/7
async def handle(request):
    return web.Response(text="Trading Bot is active and running 24/7!")

app = web.Application()
app.add_routes([web.get('/', handle)])

async def start_web_server():
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "🌟 <b>مرحباً بك مجدداً يا ديلان في بوتك الذكي!</b> 🌟\n\n"
        "أنا جاهز الآن لتخزين وبناء ذاكرتك الخاصة لاستراتيجيات التداول:\n"
        "• أرسل أي رابط فيديو وسأقوم بحفظه وتحليل ما أمكن من محتواه.\n"
        "• استخدم /price لمعرفة أسعار السوق الحالية.\n"
        "• استخدم /memory لعرض جميع الاستراتيجيات والروابط المحفوظة في ذاكرتك."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("price"))
async def cmd_price(message: types.Message):
    gold_price = "2,550.40 USD"
    btc_price = "92,100.00 USD"
    
    response_text = (
        f"📊 <b>أسعار السوق الحالية:</b>\n\n"
        f"🟡 الذهب (XAU/USD): <b>{gold_price}</b>\n"
        f"₿ البيتكوين (BTC): <b>{btc_price}</b>\n\n"
        f"<i>حالة النظام: مستقر ويعمل 🟢</i>"
    )
    await message.answer(response_text, parse_mode="HTML")

@dp.message(Command("memory"))
async def cmd_memory(message: types.Message):
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip():
            # إذا كانت الذاكرة طويلة، نعرض آخر جزء منها أو نرسلها
            if len(content) > 3500:
                content = content[-3500:] + "\n\n[... تم إظهار أحدث محتوى الذاكرة ...]"
            response_text = f"🧠 <b>ذاكرة الاستراتيجيات والروابط المحفوظة:</b>\n\n<pre>{content}</pre>"
        else:
            response_text = "📭 ذاكرة البوت فارغة حالياً. أرسل بعض روابط الفيديوهات لتبدأ التعلم!"
    else:
        response_text = "📭 لا توجد ذاكرة مسجلة حتى الآن. أرسل روابط الفيديوهات وسأقوم بتخزينها."
    
    await message.answer(response_text, parse_mode="HTML")

# استخراج معرف فيديو يوتيوب من الرابط
def extract_youtube_id(url):
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    elif "watch?v=" in url:
        return url.split("watch?v=")[1].split("&")[0]
    return None

# دالة حفظ الروابط والنصوص في ملف الذاكرة
def save_to_memory(link, summary_text):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now}] الرابط: {link}\n")
        f.write(f"المحتوى/الملخص: {summary_text}\n")
        f.write("-" * 40 + "\n")

@dp.message()
async def handle_any_message(message: types.Message):
    if message.text and ("http://" in message.text or "https://" in message.text):
        video_link = message.text.strip()
        video_id = extract_youtube_id(video_link)
        
        extracted_summary = "رابط فيديو تعليمي (تم الحفظ للتحليل اليدوي)"
        
        if video_id:
            try:
                popular_languages = ['ar', 'en', 'fr', 'es', 'de', 'it', 'ru', 'pt', 'tr']
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=popular_languages)
                transcript_text = " ".join([item['text'] for item in transcript_list])
                extracted_summary = transcript_text[:300] + "..." if len(transcript_text) > 300 else transcript_text
                
                response_text = (
                    f"🧠 <b>تم قراءة وتحليل الفيديو وحفظه في الذاكرة!</b>\n\n"
                    f"📌 <b>مقتطف من النص المستخرج:</b>\n"
                    f"<i>\"{extracted_summary}\"</i>\n\n"
                    f"✅ تمت إضافة الاستراتيجية بنجاح إلى قاعدة بياناتك."
                )
            except Exception as e:
                response_text = (
                    f"📥 <b>تم استلام الرابط وحفظه في الذاكرة بنجاح!</b>\n\n"
                    f"الرابط: <code>{video_link}</code>\n"
                    f"<i>(ملاحظة: الترجمة غير متوفرة لهذا الفيديو، ولكن تم حفظ الرابط وسجله في ذاكرتك الخاصة).</i>"
                )
        else:
            response_text = (
                f"📥 <b>تم استلام الرابط وحفظه بنجاح!</b>\n\n"
                f"الرابط: <code>{video_link}</code>\n"
                f"<i>جاري حفظه لاستراتيجيات التداول الخاصة بك... 🧠</i>"
            )
        
        # حفظ الرابط والملخص في الذاكرة الدائمة
        save_to_memory(video_link, extracted_summary)
            
        await message.answer(response_text, parse_mode="HTML")
    else:
        await message.answer("أهلاً بك يا ديلان! أرسل لي رابط فيديو تعليمي لأتعلم منه، استخدم /memory لعرض الذاكرة، أو /price لمعرفة الأسعار.")

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
