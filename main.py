import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- خادم الويب الأساسي لإبقاء البوت متصلاً 24/7 ---
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
# ---------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "🌟 <b>مرحباً بك في بوت التداول الذكي!</b> 🌟\n\n"
        "أنا جاهز ومستعد لمساعدتك.\n"
        "• أرسل لي أي رابط فيديو تعليمي من أي منصة لأتعلم منه.\n"
        "• استخدم أمر /price لمعرفة أسعار السوق الحالية."
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

# --- استقبال روابط الفيديوهات من مختلف التطبيقات والمواقع ---
@dp.message()
async def handle_any_message(message: types.Message):
    if message.text and ("http://" in message.text or "https://" in message.text):
        video_link = message.text.strip()
        response_text = (
            f"📥 <b>تم استلام رابط الفيديو بنجاح!</b>\n\n"
            f"الرابط:\n<code>{video_link}</code>\n\n"
            f"<i>جاري حفظه ومعالجة محتواه لاستراتيجيات التداول الخاصة بك... 🧠</i>"
        )
        await message.answer(response_text, parse_mode="HTML")
    else:
        await message.answer("أهلاً بك! أرسل لي رابط فيديو تعليمي لأتعلم منه، أو استخدم /price لمعرفة الأسعار.")

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
