import os
import subprocess
import sys

# تثبيت المكتبات تلقائياً إذا لم تكن موجودة لمنع أي خطأ على Render
required_packages = ["aiogram", "aiohttp", "google-genai", "yfinance"]
for package in required_packages:
    try:
        __import__(package if package != "google-genai" else "google.genai")
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

import asyncio
import logging
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import google.genai as genai
import yfinance as yf

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

async def handle(request):
    return web.Response(text="Forex & Crypto Sync Bot is Running!")

app = web.Application()
app.add_routes([web.get('/', handle)])

async def start_web_server():
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def safe_generate_content(prompt, model='gemini-2.5-flash', retries=3):
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

def fetch_specific_asset_price(ticker_symbol, default_price):
    """جلب سعر أصل معين (معادن، فوركس، كريبتو) بدقة وحيّة"""
    try:
        ticker = yf.Ticker(ticker_symbol)
        todays_data = ticker.history(period='1d', interval='1m')
        if not todays_data.empty:
            val = float(todays_data['Close'].iloc[-1])
            if val < 10:
                return round(val, 4)
            else:
                return round(val, 2)
    except Exception:
        pass
    return default_price

async def generate_single_asset_report(asset_name, ticker_symbol, default_price, sl_val, tp1_val, tp2_val):
    if not GEMINI_KEYS: return "⚠️ مفاتيح الذكاء الاصطناعي غير مضبوطة."
    
    current_price = fetch_specific_asset_price(ticker_symbol, default_price)
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    memory_content = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()[-1000:]

    prompt = f"بناءً على السعر الحالي {current_price} للأصل {asset_name}، أعطني تحليلاً فنياً موجزاً يحدد الاتجاه (شراء/بيع) وأسباب الزخم:\n{memory_content}"
    ai_analysis = await safe_generate_content(prompt)
    if not ai_analysis:
        ai_analysis = "حركة الأسعار تحترم مستويات الدعم والمقاومة الحالية."

    report = (
        f"📊 **تحليل خاص: {asset_name}**\n"
        f"⏱️ الوقت: {current_time_str}\n\n"
        f"• **السعر الحي الآن:** `{current_price}`\n"
        f"• **منطقة الدخول:** `{round(current_price - (sl_val * 0.1), 4) if current_price < 10 else round(current_price - 0.5, 2)} - {current_price}`\n"
        f"• **الاتجاه:** شراء (BUY)\n"
        f"• **وقف الخسارة (SL):** `{round(current_price - sl_val, 4 if current_price < 10 else 2)}`\n"
        f"• **الأهداف (TP):**\n"
        f"  - الهدف الأول: `{round(current_price + tp1_val, 4 if current_price < 10 else 2)}`\n"
        f"  - الهدف الثاني: `{round(current_price + tp2_val, 4 if current_price < 10 else 2)}`\n\n"
        f"📝 **رؤية تحليلية:**\n{ai_analysis[:800]}"
    )
    return report

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer(
        "أهلاً بك يا زعيم! البوت جاهز بكل أقسام السوق (معادن، فوركس، كريبتو):\n\n"
        "🟡 **المعادن:**\n"
        "• `/analyzeGold` - الذهب\n"
        "• `/analyzeSilver` - الفضة\n\n"
        "💱 **الفوركس:**\n"
        "• `/analyzeEurUsd` - يورو / دولار\n"
        "• `/analyzeGbpUsd` - باوند / دولار\n"
        "• `/analyzeUsdJpy` - دولار / ين\n\n"
        "🪙 **الكريبتو:**\n"
        "• `/analyzeBtc` - بيتكوين\n"
        "• `/analyzeEth` - إيثريوم\n"
        "• `/analyzeSol` - سولانا"
    )

@dp.message(Command("analyzeGold"))
async def cmd_gold(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي للذهب...")
    await message.answer(await generate_single_asset_report("الذهب (XAU/USD)", "GC=F", 4412.0, 5.0, 4.0, 8.0))

@dp.message(Command("analyzeSilver"))
async def cmd_silver(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي للفضة...")
    await message.answer(await generate_single_asset_report("الفضة (XAG/USD)", "SI=F", 32.40, 0.25, 0.30, 0.60))

@dp.message(Command("analyzeEurUsd"))
async def cmd_eurusd(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي لـ EUR/USD...")
    await message.answer(await generate_single_asset_report("يورو/دولار (EUR/USD)", "EURUSD=X", 1.0500, 0.0030, 0.0025, 0.0050))

@dp.message(Command("analyzeGbpUsd"))
async def cmd_gbpusd(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي لـ GBP/USD...")
    await message.answer(await generate_single_asset_report("باوند/دولار (GBP/USD)", "GBPUSD=X", 1.2650, 0.0035, 0.0030, 0.0060))

@dp.message(Command("analyzeUsdJpy"))
async def cmd_usdjpy(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي لـ USD/JPY...")
    await message.answer(await generate_single_asset_report("دولار/ين (USD/JPY)", "USDJPY=X", 153.00, 0.40, 0.35, 0.70))

@dp.message(Command("analyzeBtc"))
async def cmd_btc(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي للبيتكوين...")
    await message.answer(await generate_single_asset_report("البيتكوين (BTC/USD)", "BTC-USD", 85000.0, 500.0, 800.0, 1500.0))

@dp.message(Command("analyzeEth"))
async def cmd_eth(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي للإيثريوم...")
    await message.answer(await generate_single_asset_report("الإيثريوم (ETH/USD)", "ETH-USD", 3100.0, 50.0, 80.0, 150.0))

@dp.message(Command("analyzeSol"))
async def cmd_sol(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer("🔄 جاري جلب السعر الحي لسولانا...")
    await message.answer(await generate_single_asset_report("سولانا (SOL/USD)", "SOL-USD", 180.0, 4.0, 6.0, 12.0))

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
        await message.answer("📰 جاري تحليل الخبر واحتساب تأثيره على الأسواق...")
        news_analysis = await safe_generate_content(f"لخص تأثير هذا الخبر باختصار شديد:\n\"{text}\"")
        if news_analysis:
            with open(NEWS_FILE, "a", encoding="utf-8") as nf:
                nf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}]\nالخبر: {text}\nالتأثير: {news_analysis}\n" + "="*35 + "\n")
            await message.answer(f"✅ تحليل الخبر:\n\n{news_analysis[:3500]}")
            return

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
