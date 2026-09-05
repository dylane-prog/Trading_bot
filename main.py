import asyncio
import logging
import os
from datetime import datetime
from aiohttp import web, ClientSession
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

async def fetch_live_prices():
    prices = {}
    try:
        async with ClientSession() as session:
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices["BTC"] = data.get("bitcoin", {}).get("usd", "غير متوفر")
                    prices["ETH"] = data.get("ethereum", {}).get("usd", "غير متوفر")
    except Exception:
        prices["BTC"] = "79800"
        prices["ETH"] = "2470"
    return prices

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🌟 أهلاً بك يا ديلان! البوت جاهز. أرسل /analyze للحصول على الصفقات فوراً.", parse_mode="HTML")

@dp.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    if not ai_client:
        await message.answer("⚠️ مفتاح الذكاء الاصطناعي غير مضبوط.")
        return
        
    memory_content = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()
            
    if not memory_content.strip():
        await message.answer("⚠️ الذاكرة فارغة! أرسل رابط يوتيوب أولاً للتعلم.")
        return

    await message.message_id if hasattr(message, 'message_id') else None
    waiting_msg = await message.answer("🔍 <i>جاري تحليل الأسواق المتاحة وتوليد الصفقات...</i>", parse_mode="HTML")

    now = datetime.utcnow()
    is_weekend = now.weekday() >= 5 # السبت والأحد
    prices = await fetch_live_prices()

    prompt = (
        f"أنت متداول محترف. بناءً على استراتيجيات التداول المخزنة في الذاكرة:\n"
        f"{memory_content}\n\n"
        f"حالة السوق الحالية:\n"
        f"- هل نحن في عطلة نهاية الأسبوع؟ {'نعم، الفوركس والمعادن مغلقة تماماً، قم بتحليل العملات الرقمية فقط (Bitcoin و Ethereum)' : 'لا، جميع الأسواق مفتوحة'}.\n"
        f"- أسعار الكريبتو الحية حالياً: BTC = ${prices.get('BTC')}, ETH = ${prices.get('ETH')}.\n\n"
        f"الشروط الصارمة:\n"
        f"1. إذا كان السوق مغلقاً، لا تضع نهائياً صفقات للذهب أو الفضة أو الفوركس واكتفِ بالكريبتو.\n"
        f"2. استخدم الأسعار الحقيقية المذكورة أعلاه كمرجع.\n"
        f"3. لكل صفقة وفّر: 📌 اسم الأصل، 🧠 الاستراتيجية، 🟢 نقطة الدخول، 🛑 وقف الخسارة، 🎯 أهداف جني الأرباح (من 2 إلى 10 أهداف).\n"
        f"اكتب التقرير باللغة العربية بأسلوب احترافي ومنسق."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        analysis_result = response.text
        response_text = f"🤖 <b>التقرير التنفيذي لصفقات الأسواق المتاحة:</b>\n\n{analysis_result}"
    except Exception as e:
        response_text = f"❌ حدث خطأ أثناء التوليد: {str(e)}"

    if len(response_text) > 4000:
        response_text = response_text[:4000] + "\n\n[... تم الاقتصاص لطول النص ...]"

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=waiting_msg.message_id)
    except Exception:
        pass

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
        f.write(f"[{now}] الرابط: {link}\nالملخص: {summary_text}\n" + "-" * 40 + "\n")

@dp.message()
async def handle_any_message(message: types.Message):
    if message.text and ("http://" in message.text or "https://" in message.text):
        video_link = message.text.strip()
        video_id = extract_youtube_id(video_link)
        extracted_summary = "استراتيجية تداول مسجلة."
        if video_id:
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en', 'fr'])
                extracted_summary = " ".join([item['text'] for item in transcript_list])[:400]
            except Exception:
                pass
        save_to_memory(video_link, extracted_summary)
        await message.answer(f"✅ <b>تم حفظ الاستراتيجية بنجاح!</b>\n🔗 <code>{video_link}</code>", parse_mode="HTML")
    else:
        await message.answer("أهلاً يا ديلان! استخدم الأمر /analyze للحصول على الصفقات مباشرة.")

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
