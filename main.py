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

# --- خادم ويب وهمي لإرضاء منصة Render ---
async def handle(request):
    return web.Response(text="Bot is running!")

app = web.Application()
app.add_routes([web.get('/', handle)])

async def start_web_server():
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
# ----------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Hello! Your trading bot is online and working!")

async def main():
    # تشغيل خادم الويب الوهمي أولاً لتلبية متطلبات Render
    await start_web_server()
    # تشغيل البوت بشكل طبيعي
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
