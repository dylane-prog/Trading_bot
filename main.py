import os
import subprocess
import sys

required_packages = ["aiogram", "aiohttp", "google-genai"]
for package in required_packages:
    try:
        __import__(package if package != "google-genai" else "google.genai")
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

import asyncio
import logging
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
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

# خادم ويب لإبقاء البوت شغالاً على Render
async def handle(request):
    return web.Response(text="Trading Bot is Active and Ready!")

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

async def generate_manual_report(asset_name, current_price, sl_diff, tp1_diff, tp2_diff):
    if not GEMINI_KEYS: return "⚠️ مفاتيح الذكاء الاصطناعي غير مضبوطة."
    
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    memory_content = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()[-1000:]

    prompt = f"بناءً على السعر الحالي الدقيق {current_price} للأصل {asset_name}، أعطني تحليلاً فنياً دقيقاً وموجزاً بناءً على الاستراتيجيات التالية:\n{memory_content}"
    ai_analysis = await safe_generate_content(prompt)
    if not ai_analysis:
        ai_analysis = "حركة الأسعار تحترم مستويات الدعم والمقاومة الحالية."

    report = (
        f"📊 **التحليل الفني اللحظي: {asset_name}**\n"
        f"⏱️ الوقت: {current_time_str}\n\n"
        f"• **السعر المعتمد:** `{current_price}`\n"
        f"• **منطقة الدخول:** `{round(current_price - 0.2, 2)} - {current_price}`\n"
        f"• **الاتجاه:** شراء (BUY)\n"
        f"• **وقف الخسارة (SL):** `{round(current_price - sl_diff, 2)}`\n"
        f"• **الأهداف (TP):**\n"
        f"  - الهدف الأول: `{round(current_price + tp1_diff, 2)}`\n"
        f"  - الهدف الثاني: `{round(current_price + tp2_diff, 2)}`\n\n"
        f"📝 **رؤية تحليلية:**\n{ai_analysis[:800]}"
    )
    return report

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "أهلاً بك يا زعيم! البوت جاهز للعمل بدقة تامة.\n\n"
        "لتحليل أي أصل بالسعر الحقيقي الذي تراه، اكتب الأمر متبوعاً بالسعر مباشرة، مثل:\n"
        "• `/gold 4406.23` (للذهب)\n"
        "• `/btc 79222.15` (للبيتكوين)\n"
        "• `/eurusd 1.0520` (لليورو دولار)\n"
        "• `/silver 31.50` (للفضة)"
    )

@dp.message(Command("gold"))
async def cmd_gold(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ الرجاء كتابة السعر مع الأمر، هكذا:\n`/gold 4406.23`")
        return
    try:
        price = float(args[1])
    except ValueError:
        await message.answer("⚠️ السعر غير صالح، تأكد من كتابة أرقام صحيحة.")
        return
    
    await message.answer("🔄 جاري إعداد التحليل الفني الدقيق للذهب...")
    report = await generate_manual_report("الذهب (XAU/USD)", price, 3.0, 4.0, 8.0)
    await message.answer(report)

@dp.message(Command("btc"))
async def cmd_btc(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ الرجاء كتابة السعر مع الأمر، هكذا:\n`/btc 79222.15`")
        return
    try:
        price = float(args[1])
    except ValueError:
        await message.answer("⚠️ السعر غير صالح.")
        return
    
    await message.answer("🔄 جاري إعداد التحليل الفني الدقيق للبيتكوين...")
    report = await generate_manual_report("البيتكوين (BTC/USD)", price, 300.0, 500.0, 1000.0)
    await message.answer(report)

@dp.message(Command("eurusd"))
async def cmd_eurusd(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ الرجاء كتابة السعر مع الأمر، هكذا:\n`/eurusd 1.0520`")
        return
    try:
        price = float(args[1])
    except ValueError:
        await message.answer("⚠️ السعر غير صالح.")
        return
    
    await message.answer("🔄 جاري إعداد التحليل الفني لـ EUR/USD...")
    report = await generate_manual_report("يورو/دولار (EUR/USD)", price, 0.0025, 0.0030, 0.0060)
    await message.answer(report)

@dp.message(Command("silver"))
async def cmd_silver(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ الرجاء كتابة السعر مع الأمر، هكذا:\n`/silver 31.50`")
        return
    try:
        price = float(args[1])
    except ValueError:
        await message.answer("⚠️ السعر غير صالح.")
        return
    
    await message.answer("🔄 جاري إعداد التحليل الفني للفضة...")
    report = await generate_manual_report("الفضة (XAG/USD)", price, 0.20, 0.30, 0.60)
    await message.answer(report)

@dp.message(F.text.func(lambda text: not text.startswith("/")))
async def handle_any_message(message: types.Message):
    text = message.text or message.caption
    if not text: return

    if "http://" in text or "https://" in text:
        await message.answer("جاري تحليل وحفظ استراتيجية الفيديو...")
        res = await safe_generate_content(f"لخص هذه الاستراتيجية في نقاط تداول صارمة:\n{text}")
        if res:
            with open(MEMORY_FILE, "a", encoding="utf-8") as mf:
                mf.write(f"\n[رابط: {text}]\n{res}\n" + "-"*30 + "\n")
            await message.answer(f"✅ تم حفظ الاستراتيجية بنجاح:\n\n{res[:3500]}")
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
