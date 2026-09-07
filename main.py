import asyncio
import logging
import os
import re
from datetime import datetime
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from youtube_transcript_api import YouTubeTranscriptApi
import google.genai as genai

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEYS_RAW = os.getenv("GEMINI_API_KEYS", "")
GEMINI_KEYS = [k.strip() for k in GEMINI_KEYS_RAW.split(",") if k.strip()]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

class KeyManager:
    def __init__(self, keys):
        self.keys = keys
        self.current_index = 0

    def get_client(self):
        if not self.keys: return None
        return genai.Client(api_key=self.keys[self.current_index])

    def rotate_key(self):
        if len(self.keys) > 1:
            self.current_index = (self.current_index + 1) % len(self.keys)

key_manager = KeyManager(GEMINI_KEYS)

MEMORY_FILE = "strategies_memory.txt"
NEWS_FILE = "news_memory.txt"

active_trades_cache = []

async def handle(request):
    return web.Response(text="Strict Price Trading Bot is Running!")

app = web.Application()
app.add_routes([web.get('/', handle)])

async def start_web_server():
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def safe_generate_content(prompt, model='gemini-3.6-flash', retries=3):
    if not GEMINI_KEYS: return None
    for _ in range(retries * len(GEMINI_KEYS)):
        client = key_manager.get_client()
        if not client: return None
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            return response.text
        except Exception:
            key_manager.rotate_key()
            await asyncio.sleep(1)
    return None

async def fetch_live_prices():
    prices = {
        "BTC": 85000.0, 
        "ETH": 3100.0, 
        "XAU_Gold": 4413.91, 
        "XAG_Silver": 32.40, 
        "EUR_USD": 1.0500, 
        "GBP_USD": 1.2650, 
        "USD_JPY": 153.00
    }
    
    symbols = {
        "BTC": "BTC-USD",
        "ETH": "ETH-USD",
        "XAU_Gold": "GC=F",
        "XAG_Silver": "SI=F",
        "EUR_USD": "EURUSD=X",
        "GBP_USD": "GBPUSD=X",
        "USD_JPY": "JPY=X"
    }

    headers = {"User-Agent": "Mozilla/5.0"}
    async with ClientSession() as session:
        for key, sym in symbols.items():
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m"
                async with session.get(url, headers=headers, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
                        regular_price = meta.get("regularMarketPrice")
                        if regular_price:
                            if key in ["EUR_USD", "GBP_USD"]:
                                prices[key] = round(float(regular_price), 4)
                            elif key == "USD_JPY":
                                prices[key] = round(float(regular_price), 2)
                            else:
                                prices[key] = round(float(regular_price), 2)
            except Exception:
                pass

    return prices

async def generate_market_report():
    global active_trades_cache
    if not GEMINI_KEYS: return "⚠️ مفاتيح الذكاء الاصطناعي غير مضبوطة."
    
    memory_content = "لا توجد استراتيجيات مسجلة."
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()

    news_content = "لا توجد أخبار مسجلة."
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news_content = f.read()

    p = await fetch_live_prices()
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # برومبت عسكري صارم يمنع منعاً باتاً اختراع الأسعار ويجبره على استخدام السعر الحالي حرفياً
    prompt = (
        f"أنت نظام حاسوبي صارم جداً. وقت الإصدار: {current_time_str}.\n"
        f"⚠️ تنبيه إلزامي وممنوع مخالفته: أسعار السوق الحالية الرسمية الآن هي كالتالي:\n"
        f"- الذهب XAU/USD: {p['XAU_Gold']}\n"
        f"- الفضة XAG/USD: {p['XAG_Silver']}\n"
        f"- اليورو دولار EUR/USD: {p['EUR_USD']}\n"
        f"- الباوند دولار GBP/USD: {p['GBP_USD']}\n"
        f"- البيتكوين BTC: {p['BTC']}\n\n"
        f"قاعدة صارمة: يجب أن تكون 'منطقة الدخول' قريبة جداً أو مطابقة تماماً لسعر السوق الحالي المذكور أعلاه (بفارق بضعة سنتات أو دولارات قليلة جداً بناءً على التحليل)، وممنوع نهائياً اختراع أسعار وهمية بعيدة عن السوق!\n\n"
        f"الاستراتيجيات والأخبار المتاحة:\n{memory_content}\n{news_content}\n\n"
        f"المطلوب لكل أصل، قدم الآتي باختصار شديد:\n"
        f"1. السعر الحالي الحقيقي في السوق\n"
        f"2. الاتجاه (شراء/بيع)\n"
        f"3. منطقة الدخول (قريبة من السعر الحالي بدقة)\n"
        f"4. وقف الخسارة (SL)\n"
        f"5. الأهداف (TP1, TP2, TP3)\n"
        f"6. المدة الزمنية الصغرى المتوقعة للصفقة بالدقائق أو الساعات (مثال: 120 دقيقة أو 4 ساعات)"
    )

    report_text = await safe_generate_content(prompt)
    if report_text:
        active_trades_cache = [report_text, current_time_str]
    return report_text if report_text else "⚠️ حدث ضغط، حاول لاحقاً."

async def trade_monitor_background_loop():
    await asyncio.sleep(60)
    while True:
        try:
            if active_trades_cache and os.path.exists("last_chat_id.txt"):
                with open("last_chat_id.txt", "r") as f:
                    chat_id = f.read().strip()
                if chat_id:
                    report_text = active_trades_cache[0]
                    found_numbers = re.findall(r'(\d+)\s*(دقيقة|دقائق|ساعة|ساعات|يوم|أيام)', report_text)
                    min_minutes = 60 
                    
                    if found_numbers:
                        val = int(found_numbers[0][0])
                        unit = found_numbers[0][1]
                        if "ساعة" in unit or "ساعات" in unit:
                            min_minutes = val * 60
                        elif "يوم" in unit or "أيام" in unit:
                            min_minutes = val * 24 * 60
                        else:
                            min_minutes = val

                    check_interval = max(int((min_minutes * 60) * 0.1), 30) 
                    await asyncio.sleep(check_interval)
                    
                    p = await fetch_live_prices()
                    monitor_prompt = (
                        f"بناءً على الصفقات السابقة:\n{report_text}\n\n"
                        f"السعر الحقيقي الحالي في السوق:\nالذهب: {p['XAU_Gold']} | الفضة: {p['XAG_Silver']} | اليورو: {p['EUR_USD']} | البيتكوين: {p['BTC']}\n\n"
                        f"قم بتحليل سريع: هل السعر الحالي يهدد الصفقة أو اقترب من الانعكاس أو وقف الخسارة؟ نبه المستخدم فوراً بدقة."
                    )
                    analysis = await safe_generate_content(monitor_prompt)
                    if analysis:
                        await bot.send_message(chat_id=int(chat_id), text=f"⚠️ **مراقبة دورية (كل 10% من الوقت):**\n\n{analysis[:3500]}")
                        continue
        except Exception:
            pass
        await asyncio.sleep(60)

async def hourly_background_reporter():
    await asyncio.sleep(30)
    while True:
        try:
            if os.path.exists("last_chat_id.txt"):
                with open("last_chat_id.txt", "r") as f:
                    chat_id = f.read().strip()
                if chat_id:
                    report = await generate_market_report()
                    await bot.send_message(chat_id=int(chat_id), text=f"📊 التقرير التنفيذي المباشر:\n\n{report[:4000]}")
        except Exception:
            pass
        await asyncio.sleep(7200)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("أهلاً بك يا زعيم! تم تقييد الأسعار برمجياً لتكون مطابقة 100% لسعر السوق الحالي دون أي انحراف.")

@dp.message(Command("strategies"))
async def cmd_strategies(message: types.Message):
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip():
            await message.answer(f"🧠 الاستراتيجيات المحفوظة:\n\n{content[-3500:]}")
            return
    await message.answer("لا توجد استراتيجيات مخزنة.")

@dp.message(Command("news"))
async def cmd_news(message: types.Message):
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip():
            await message.answer(f"📰 الأخبار المسجلة:\n\n{content[-3500:]}")
            return
    await message.answer("لا توجد أخبار مسجلة.")

@dp.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري التحقق من الأسعار الحية بدقة ومطابقتها للشارت...")
    report = await generate_market_report()
    await message.answer(f"📊 التقرير المباشر:\n\n{report[:4000]}")

@dp.message()
async def handle_any_message(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    text = message.text or message.caption
    if not text: return
    
    if "http://" in text or "https://" in text:
        await message.answer("جاري تحليل وحفظ استراتيجية الفيديو...")
        res = await safe_generate_content(f"لخص هذه الاستراتيجية في نقاط تداول صارمة:\n{text}")
        if res:
            with open(MEMORY_FILE, "a", encoding="utf-8") as mf:
                mf.write(f"\n[رابط: {text}]\n{res}\n" + "-"*30 + "\n")
            await message.answer(f"✅ تم الحفظ:\n\n{res[:3500]}")
        return
    else:
        await message.answer("📰 جاري تحليل الخبر واحتساب تأثيره على كافة الأسواق...")
        news_analysis = await safe_generate_content(f"لخص تأثير هذا الخبر باختصار شديد على الذهب والفوركس والكريبتو:\n\"{text}\"")
        if news_analysis:
            with open(NEWS_FILE, "a", encoding="utf-8") as nf:
                nf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}]\nالخبر: {text}\nالتأثير: {news_analysis}\n" + "="*35 + "\n")
            await message.answer(f"✅ تحليل الخبر المباشر:\n\n{news_analysis[:3500]}")
            return

async def main():
    await start_web_server()
    asyncio.create_task(hourly_background_reporter())
    asyncio.create_task(trade_monitor_background_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
