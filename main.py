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

async def handle(request):
    return web.Response(text="Trading & News Assistant Bot is active 24/7!")

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
    welcome_text = (
        "🌟 <b>مرحباً بك يا زعيم ديلان في مساعد التداول والأخبار الذكي!</b> 🌟\n\n"
        "• أرسل روابط يوتيوب لاستخلاص استراتيجيات SMC.\n"
        "• قم بتوجيه (Forward) الأخبار من القنوات الإخبارية إليّ هنا لأقوم بتحليلها فوراً.\n"
        "• استخدم الأمر /analyze للحصول على الصفقات المدمجة بالأسعار الحية والأخبار.\n"
        "• استخدم /news لعرض أرشيف الأخبار والتحليلات."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("news"))
async def cmd_news(message: types.Message):
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news_content = f.read()
        if news_content.strip():
            if len(news_content) > 3000:
                news_content = news_content[-3000:]
            await message.answer(f"📰 <b>أرشيف الأخبار والتحليلات المرصودة:</b>\n\n<pre>{news_content}</pre>", parse_mode="HTML")
            return
    await message.answer("📭 لا توجد أخبار مسجلة حالياً. قم بتوجيه رسائل الأخبار إليّ لتحليلها.", parse_mode="HTML")

@dp.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    if not ai_client:
        await message.answer("⚠️ مفتاح الذكاء الاصطناعي غير مضبوط.")
        return
        
    memory_content = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()

    news_content = "لا توجد أخبار عاجلة مسجلة حالياً."
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news_content = f.read()
            
    if not memory_content.strip():
        await message.answer("⚠️ الذاكرة فارغة! أرسل رابط استراتيجية يوتيوب أولاً للتعلم.")
        return

    await message.answer("🔍 <i>جاري تحليل الأسواق ودمج استراتيجيات SMC مع أحدث الأخبار والأسعار الحية...</i>", parse_mode="HTML")

    prices = await fetch_live_prices()
    btc_price = prices.get("BTC")
    eth_price = prices.get("ETH")

    now = datetime.utcnow()
    is_weekend = now.weekday() >= 5  # السبت والأحد

    market_condition_note = (
        "اليوم عطلة نهاية الأسبوع (الذهب والفضة والفوركس مغلقة). قم بالتركيز حصرياً على تحليل العملات الرقمية (Bitcoin و Ethereum)."
        if is_weekend else "جميع الأسواق المالية مفتوحة حالياً."
    )

    prompt = (
        f"أنت متداول محترف ومدير مخاطر خبير. المعطيات:\n\n"
        f"1. استراتيجيات التداول المخزنة:\n{memory_content}\n\n"
        f"2. أحدث الأخبار والتحليلات المستقطبة:\n{news_content}\n\n"
        f"3. حالة السوق: {market_condition_note}\n"
        f"4. الأسعار الحية للكريبتو: BTC = ${btc_price}, ETH = ${eth_price}\n\n"
        f"المطلوب تقرير صفقات تنفيذي دقيق يدمج تأثير الأخبار مع الاستراتيجية الفنية بوضوح تام."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        analysis_result = response.text
        response_text = f"🤖 <b>التقرير التنفيذي الشامل (استراتيجيات + أخبار + أسعار حية):</b>\n\n{analysis_result}"
    except Exception as e:
        response_text = f"❌ حدث خطأ أثناء التوليد: {str(e)}"

    if len(response_text) > 4000:
        response_text = response_text[:4000] + "\n\n[... تم الاقتصاص لطول النص ...]"

    await message.answer(response_text, parse_mode="HTML")

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
    text = message.text or message.caption
    if not text:
        return

    # معالجة روابط يوتيوب
    if "http://" in text or "https://" in text:
        video_link = text.strip()
        video_id = extract_youtube_id(video_link)
        extracted_summary = "استراتيجية تداول SMC مسجلة."
        if video_id:
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en', 'fr'])
                extracted_summary = " ".join([item['text'] for item in transcript_list])[:300]
            except Exception:
                pass
        save_to_memory(MEMORY_FILE, f"رابط يوتيوب: {video_link}\nالملخص: {extracted_summary}")
        await message.answer(f"✅ <b>تم استيعاب وحفظ الاستراتيجية بنجاح!</b>\n🔗 <code>{video_link}</code>", parse_mode="HTML")
    else:
        # معالجة الأخبار المحولة (Forwarded) أو المرسلة من المستخدم
        if ai_client:
            try:
                news_prompt = (
                    f"هذا خبر إخباري تم توجيهه من قنوات تداول:\n\"{text}\"\n\n"
                    f"قم بتحليله باختصار واذكر: 1) ملخص الخبر. 2) تأثيره على الأسواق (إيجابي صعودي / سلبي هبوطي / محايد)."
                )
                res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=news_prompt)
                news_analysis = res.text
                
                save_to_memory(NEWS_FILE, f"الخبر الأصلي: {text}\nالتحليل الآلي: {news_analysis}")
                
                response_alert = (
                    f"⚡ <b>تم استقبال وتحليل الخبر بنجاح!</b>\n\n"
                    f"📌 <b>التحليل والتأثير:</b>\n{news_analysis}\n\n"
                    f"<i>✅ تم تخزينه في الذاكرة وسيعتمد في أمر /analyze القادم.</i>"
                )
                await message.answer(response_alert, parse_mode="HTML")
                return
            except Exception:
                pass
        
        await message.answer("أهلاً يا زعيم! قم بتوجيه الأخبار إليّ لتحليلها، أو استخدم /analyze للحصول على الصفقات المحدثة.", parse_mode="HTML")

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
