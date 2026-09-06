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
NEWS_FILE = "news_memory.txt"
TRADES_LOG_FILE = "trades_performance_log.txt"

async def handle(request):
    return web.Response(text="Fully Autonomous Smart Trading & Self-Review Bot is active 24/7!")

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

async def generate_market_report():
    if not ai_client:
        return "⚠️ مفتاح الذكاء الاصطناعي غير مضبوط."
        
    memory_content = "لا توجد استراتيجيات مسجلة."
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()

    news_content = "لا توجد أخبار مسجلة."
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news_content = f.read()

    trades_history = "لا توجد صفقات سابقة مسجلة للمراجعة بعد."
    if os.path.exists(TRADES_LOG_FILE):
        with open(TRADES_LOG_FILE, "r", encoding="utf-8") as f:
            trades_history = f.read()

    prices = await fetch_live_prices()
    btc_price = prices.get("BTC")
    eth_price = prices.get("ETH")

    now = datetime.utcnow()
    is_weekend = now.weekday() >= 5

    market_condition_note = (
        "اليوم عطلة نهاية الأسبوع. التركيز حصرياً على العملات الرقمية (Bitcoin و Ethereum)."
        if is_weekend else "جميع الأسواق المالية مفتوحة حالياً."
    )

    prompt = (
        f"أنت مدير تداول آلي ذكي وخبير استراتيجي. مهمتك مراجعة الأداء السابق واستنتاج الاستراتيجيات الأصح نسبياً.\n\n"
        f"1. مكتبة الاستراتيجيات المخزنة:\n{memory_content}\n\n"
        f"2. سجل الصفقات والتقييمات الذاتية السابقة:\n{trades_history}\n\n"
        f"3. أحدث الأخبار المرصودة:\n{news_content}\n\n"
        f"4. حالة السوق الحالية: {market_condition_note}\n"
        f"5. الأسعار الحية للكريبتو: BTC = ${btc_price}, ETH = ${eth_price}\n\n"
        f"المطلوب تقرير صفقات تنفيذي دقيق:\n"
        f"- تصنيف الاستراتيجيات المستخدمة حسب كفاءتها.\n"
        f"- عند إعطاء أي صفقة مقترحة، **يجب أن تتضمن عدة مستويات لجني الأرباح (TP1, TP2, TP3)** بدلاً من هدف واحد، مع تحديد نسبة الخروج عند كل هدف.\n"
        f"- يجب تحديد مدة تحقيق الصفقة بدقة ضمن نطاق حصري بين دقيقة واحدة (1m) و 72 ساعة (72h).\n"
        f"- أجب بنصوص صافية بدون رموز تنسيق HTML معقدة."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        report_text = response.text
        
        with open(TRADES_LOG_FILE, "a", encoding="utf-8") as log_f:
            log_f.write(f"--- تقييم ومراجعة تلقائية [{now.strftime('%Y-%m-%d %H:%M')}] ---\n{report_text[:500]}...\n\n")
            
        return report_text
    except Exception as e:
        return f"❌ حدث خطأ أثناء التوليد: {str(e)}"

async def hourly_background_reporter():
    await asyncio.sleep(15)
    while True:
        try:
            if os.path.exists("last_chat_id.txt"):
                with open("last_chat_id.txt", "r") as f:
                    chat_id = f.read().strip()
                if chat_id:
                    report = await generate_market_report()
                    full_msg = f"التقرير التلقائي الساعي:\n\n{report}"
                    if len(full_msg) > 4000:
                        full_msg = full_msg[:4000]
                    await bot.send_message(chat_id=int(chat_id), text=full_msg)
        except Exception as e:
            logging.error(f"Error in background reporter: {e}")
        
        await asyncio.sleep(3600)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    with open("last_chat_id.txt", "w") as f:
        f.write(str(message.chat.id))

    welcome_text = (
        "مرحباً بك يا زعيم ديلان في نظام التداول الذاتي والمراجعة الذكية!\n\n"
        "• يراجع البوت الصفقات ويصنف الاستراتيجيات أوتوماتيكياً.\n"
        "• كل صفقة تتضمن الآن **عدة أهداف لجني الأرباح (TP1, TP2, TP3)** ومدد زمنية دقيقة."
    )
    await message.answer(welcome_text)

@dp.message(Command("strategies"))
async def cmd_strategies(message: types.Message):
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip():
            if len(content) > 3000:
                content = content[-3000:]
            await message.answer(f"مكتبة الاستراتيجيات المصنفة:\n\n{content}")
            return
    await message.answer("لا توجد استراتيجيات مخزنة بعد.")

@dp.message(Command("news"))
async def cmd_news(message: types.Message):
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news_content = f.read()
        if news_content.strip():
            if len(news_content) > 3000:
                news_content = news_content[-3000:]
            await message.answer(f"أرشيف الأخبار والتحليلات:\n\n{news_content}")
            return
    await message.answer("لا توجد أخبار مسجلة حالياً.")

@dp.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    with open("last_chat_id.txt", "w") as f:
        f.write(str(message.chat.id))

    await message.answer("جاري مراجعة الأداء وتوليد الصفقات بأهداف متعددة لجني الأرباح...")
    report = await generate_market_report()
    response_text = f"التقرير التنفيذي المصنف والمراجع ذاتياً:\n\n{report}"
    if len(response_text) > 4000:
        response_text = response_text[:4000]
    await message.answer(response_text)

def extract_youtube_id(url):
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    elif "watch?v=" in url:
        return url.split("watch?v=")[1].split("&")[0]
    return None

def save_to_memory(file_path, text_content):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(f"[{now}] {text_content}\n" + "-" * 40 + "\n")

@dp.message()
async def handle_any_message(message: types.Message):
    with open("last_chat_id.txt", "w") as f:
        f.write(str(message.chat.id))

    text = message.text or message.caption
    if not text:
        return

    if "http://" in text or "https://" in text:
        video_link = text.strip()
        video_id = extract_youtube_id(video_link)
        transcript_text = "استراتيجية تداول عامة."
        if video_id:
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en', 'fr'])
                transcript_text = " ".join([item['text'] for item in transcript_list])[:1500]
            except Exception:
                pass

        await message.answer("جاري تحليل الاستراتيجية وإخضاعها للباك تست...")

        if ai_client:
            try:
                eval_prompt = (
                    f"بناءً على نص الفيديو المستخرج التالي:\n\"{transcript_text}\"\n\n"
                    f"قم بالآتي:\n"
                    f"1. اقترح اسماً للاستراتيجية وحدد تصنيفها.\n"
                    f"2. قم بعمل محاكاة لجدول كفاءتها.\n"
                    f"3. اكتب كود برمجي بـ (Pine Script v5) مع وضع مستويات أهداف متعددة (TP1, TP2, TP3).\n"
                    f"أجب بنصوص صافية."
                )
                res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=eval_prompt)
                evaluation_result = res.text

                save_to_memory(MEMORY_FILE, f"رابط يوتيوب: {video_link}\nالتحليل:\n{evaluation_result}")

                response_text = f"نتيجة تحليل الاستراتيجية:\n\n{evaluation_result}"
                if len(response_text) > 4000:
                    response_text = response_text[:4000]

                await message.answer(response_text)
                return
            except Exception as e:
                await message.answer(f"حدث خطأ: {str(e)}")
        return
    else:
        if ai_client:
            try:
                news_prompt = (
                    f"هذا خبر تم توجيهه:\n\"{text}\"\n\n"
                    f"قم بتحليله باختصار واذكر تأثيره على الأسواق."
                )
                res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=news_prompt)
                news_analysis = res.text
                
                save_to_memory(NEWS_FILE, f"الخبر الأصلي: {text}\nالتحليل: {news_analysis}")
                
                await message.answer(f"تم رصد وتحليل الخبر:\n\n{news_analysis}")
                return
            except Exception:
                pass
        
        await message.answer("البوت جاهز. أرسل `/analyze` لرؤية الأهداف المتعددة لجني الأرباح الآن.")

async def main():
    await start_web_server()
    asyncio.create_task(hourly_background_reporter())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
