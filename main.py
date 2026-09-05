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
    try:
        async with ClientSession() as session:
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "BTC": data.get("bitcoin", {}).get("usd", 79800),
                        "ETH": data.get("ethereum", {}).get("usd", 2470)
                    }
    except Exception:
        pass
    return {"BTC": 79800, "ETH": 2470}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🌟 أهلاً يا ديلان! البوت جاهز. أرسل /analyze للحصول على التقرير الفوري للكريبتو.", parse_mode="HTML")

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

    await message.answer("🔍 <i>جاري تحليل أسواق الكريبتو المتاحة بناءً على استراتيجيات SMC...</i>", parse_mode="HTML")

    prices = await fetch_live_prices()
    btc_price = prices.get("BTC")
    eth_price = prices.get("ETH")

    prompt = (
        f"بناءً على استراتيجيات التداول المخزنة هنا:\n{memory_content}\n\n"
        f"بما أن اليوم عطلة نهاية الأسبوع والذهب والفوركس مغلقان، قم فقط بتحليل عملتي **Bitcoin (BTC)** و **Ethereum (ETH)** بناءً على الأسعار الحية الحالية:\n"
        f"- Bitcoin (BTC) السعر الحالي: ${btc_price}\n"
        f"- Ethereum (ETH) السعر الحالي: ${eth_price}\n\n"
        f"اكتب تقريراً احترافياً ومباشراً باللغة العربية يتضمن:\n"
        f"1. اسم الأصل والاتجاه (Buy/Sell).\n"
        f"2. نقطة الدخول بناءً على السعر الحالي.\n"
        f"3. وقف الخسارة.\n"
        f"4. أهداف جني الأرباح (TP1 حتى TP5 على الأقل).\n"
        f"اجعل التنسيق واضحاً ومباشراً بدون مقدمات طويلة."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        analysis_result = response.text
        response_text = f"🤖 <b>التقرير التنفيذي للكريبتو (عطلة نهاية الأسبوع):</b>\n\n{analysis_result}"
    except Exception as e:
        response_text = f"❌ حدث خطأ أثناء التوليد: {str(e)}"

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
        extracted_summary = "استراتيجية تداول SMC مسجلة."
        if video_id:
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en', 'fr'])
                extracted_summary = " ".join([item['text'] for item in transcript_list])[:300]
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
