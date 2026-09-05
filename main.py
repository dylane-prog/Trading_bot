import asyncio
import logging
import os
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from youtube_transcript_api import YouTubeTranscriptApi
import google.genai as genai

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

MEMORY_FILE = "strategies_memory.txt"

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
        "🌟 <b>مرحباً بك يا ديلان في بوت الصفقات الذكي المستقل!</b> 🌟\n\n"
        "أنا جاهز لتحويل استراتيجيات اليوتيوب إلى صفقات دقيقة:\n"
        "• أرسل روابط استراتيجيات اليوتيوب لحفظها.\n"
        "• استخدم /analyze للحصول على صفقات مفصلة لكل سوق (ذهب، فضة، فوركس، كريبتو) مع نقاط الدخول، وقف الخسارة، وأهداف جني الأرباح (من 2 إلى 10 أهداف)."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("price"))
async def cmd_price(message: types.Message):
    response_text = (
        f"📊 <b>حالة الأسواق الحالية:</b>\n\n"
        f"🟡 الذهب / الفضة (Forex/Metals): عطلة نهاية الأسبوع (جاهز للتحليل المسبق)\n"
        f"₿ العملات الرقمية (Crypto): نشطة ومفتوحة 24/7\n\n"
        f"<i>النظام يعمل بكفاءة 🟢</i>"
    )
    await message.answer(response_text, parse_mode="HTML")

@dp.message(Command("memory"))
async def cmd_memory(message: types.Message):
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip():
            if len(content) > 3500:
                content = content[-3500:] + "\n\n[... أحدث الذاكرة ...]"
            response_text = f"🧠 <b>الاستراتيجيات المخزنة:</b>\n\n<pre>{content}</pre>"
        else:
            response_text = "📭 الذاكرة فارغة. أرسل روابط يوتيوب للتعلم!"
    else:
        response_text = "📭 لا توجد استراتيجيات مسجلة بعد."
    
    await message.answer(response_text, parse_mode="HTML")

@dp.message(lambda message: message.text and message.text.startswith("/analyze"))
async def cmd_analyze(message: types.Message):
    if not ai_client:
        await message.answer("⚠️ مفتاح `GEMINI_API_KEY` غير مضبوط في رندر.")
        return
        
    memory_content = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()
            
    if not memory_content.strip():
        await message.answer("⚠️ الذاكرة فارغة! أرسل استراتيجيات أولاً.")
        return

    await message.answer("🔍 <i>جاري تحليل أسواق الذهب، الفضة، الفوركس، والكريبتو وتوليد الصفقات بالتفصيل...</i>", parse_mode="HTML")

    market_data = (
        "1. الذهب (XAU/USD)\n"
        "2. الفضة (XAG/USD)\n"
        "3. الفوركس (EUR/USD, GBP/USD)\n"
        "4. العملات الرقمية (BTC/USD, ETH/USD - مفتوح 24/7)"
    )

    prompt = (
        f"أنت متداول محترف وخبير في الأسواق المالية. بناءً على استراتيجيات التداول المخزنة في الذاكرة:\n"
        f"{memory_content}\n\n"
        f"وقم بتطبيقها على الأسواق التالية:\n"
        f"{market_data}\n\n"
        f"الشروط المطلوبة للتقرير:\n"
        f"- لكل سوق/أصل مالي، قم بفصله في قسم مستقل بذاته.\n"
        f"- اكتب لكل صفقة العناصر التالية بدقة شديدة وبدون إخلال:\n"
        f"  * 📌 **اسم السوق / الزوج**\n"
        f"  * 🧠 **الاستراتيجية المطبقة** (من الذاكرة)\n"
        f"  * 🟢 **نقطة الدخول (Entry Point)**\n"
        f"  * 🛑 **نقطة وقف الخسارة (Stop Loss)**\n"
        f"  * 🎯 **أهداف جني الأرباح (Take Profit)**: قم بتوفير من 2 إلى 10 أهداف تداول تصاعدية (TP1, TP2, TP3 ... وصولاً حتى TP10 إن أمكن).\n"
        f"اكتب التقرير باللغة العربية بأسلوب احترافي جداً ومنسق للمتداولين."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        analysis_result = response.text
        response_text = f"🤖 <b>توصيات الصفقات الذكية لكل الأسواق:</b>\n\n{analysis_result}"
    except Exception as e:
        response_text = f"❌ خطأ في الاتصال بالذكاء الاصطناعي: {str(e)}"

    if len(response_text) > 4000:
        response_text = response_text[:4000] + "\n\n[... تم اقتصاص التقرير لطوله ...]"

    await message.answer(response_text, parse_mode="HTML")

def extract_youtube_id(url):
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    elif "watch?v=" in url:
        return url.split("watch?v=")[1].split("&")[0]
    return None

def save_to_memory(link, summary_text):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now}] الرابط: {link}\n")
        f.write(f"الملخص: {summary_text}\n")
        f.write("-" * 40 + "\n")

@dp.message()
async def handle_any_message(message: types.Message):
    if message.text and ("http://" in message.text or "https://" in message.text):
        video_link = message.text.strip()
        video_id = extract_youtube_id(video_link)
        
        extracted_summary = ""
        success_mode = False
        
        if video_id:
            try:
                popular_languages = ['ar', 'en', 'fr', 'es', 'de', 'it', 'ru', 'pt', 'tr']
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=popular_languages)
                transcript_text = " ".join([item['text'] for item in transcript_list])
                extracted_summary = transcript_text[:500] + "..." if len(transcript_text) > 500 else transcript_text
                success_mode = True
            except Exception:
                pass
        
        if not success_mode and ai_client:
            try:
                ai_prompt = f"المستخدم أرسل رابط يوتيوب: {video_link}. استخلص منه استراتيجية تداول احترافية (مثل SMC أو Price Action) يمكن حفظها في الذاكرة."
                ai_res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=ai_prompt)
                extracted_summary = ai_res.text[:500]
                success_mode = True
            except Exception:
                extracted_summary = "استراتيجية تداول مسجلة للتحليل الأوتوماتيكي."

        save_to_memory(video_link, extracted_summary)
        
        response_text = (
            f"🤖🧠 <b>تم استيعاب الاستراتيجية وحفظها بنجاح!</b>\n\n"
            f"🔗 <code>{video_link}</code>\n"
            f"📌 <b>الملخص:</b> <i>\"{extracted_summary}\"</i>"
        )
        await message.answer(response_text, parse_mode="HTML")
    else:
        await message.answer("أهلاً يا ديلان! أرسل رابط يوتيوب جديد، أو استخدم /analyze للحصول على الصفقات التفصيلية.")

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
