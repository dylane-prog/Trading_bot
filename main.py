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

# --- استقبال تنبيهات TradingView الحية من أي سوق ---
async def tradingview_webhook(request):
    try:
        data = await request.json()
        # المتوقع إرساله من تداول فيو: asset, price, action
        asset = data.get("asset", "المنصة")
        price = data.get("price", "0.0")
        action = data.get("action", "تحليل")
        
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # قراءة الذاكرة لتحليل الذكاء الاصطناعي
        memory_content = ""
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memory_content = f.read()[-1000:]

        prompt = f"السعر الحالي المباشر من TradingView للأصل {asset} هو {price}. أعطني تحليلاً موجزاً للوضع بناءً على استراتيجياتنا:\n{memory_content}"
        ai_analysis = await safe_generate_content(prompt)
        if not ai_analysis:
            ai_analysis = "حركة الأسعار عند مستويات حاسمة."

        report = (
            f"🚨 **تنبيه حقيقي من السوق (TradingView)**\n"
            f"📊 **الأصل:** {asset}\n"
            f"⏱️ الوقت: {current_time_str}\n\n"
            f"• **السعر الفوري:** `{price}`\n"
            f"• **الحركة/الإشارة:** `{action}`\n\n"
            f"📝 **رؤية تحليلية:**\n{ai_analysis[:800]}"
        )

        # إرسال التقرير لآخر مستخدم تفاعل مع البوت
        if os.path.exists("last_chat_id.txt"):
            with open("last_chat_id.txt", "r") as f:
                chat_id = f.read().strip()
            if chat_id:
                await bot.send_message(chat_id, report)

        return web.Response(text="Webhook Received Successfully", status=200)
    except Exception as e:
        return web.Response(text=f"Error: {str(e)}", status=400)

async def handle(request):
    return web.Response(text="TradingView & Telegram Sync Bot is Running!")

app = web.Application()
app.add_routes([
    web.get('/', handle),
    web.post('/webhook', tradingview_webhook) # مسار استقبال بيانات السوق اللحظية
])

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

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    await message.answer(
        "أهلاً بك يا زعيم! البوت جاهز الآن لاستقبال بيانات السوق الحية مباشرة عبر **TradingView Webhooks**.\n\n"
        "• قم بإرسال أي استراتيجية أو رابط وسيقوم البوت بحفظها.\n"
        "• استخدم أمر `/strategies` أو `/news` للمراجعة.\n"
        "• أي تنبيه تضبطه على TradingView برابط الـ Webhook الخاص بك سيصلك تحليله فوراً هنا!"
    )

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

@dp.message(F.text.func(lambda text: not text.startswith("/")))
async def handle_any_message(message: types.Message):
    text = message.text or message.caption
    if not text: return

    with open("last_chat_id.txt", "w") as f: f.write(str(message.chat.id))
    
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
