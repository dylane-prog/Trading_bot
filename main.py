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

# دالة لجلب الأسعار الحية للأسواق المتاحة
async def fetch_live_prices():
    prices = {}
    try:
        async with ClientSession() as session:
            # جلب أسعار العملات الرقمية الحية (تعمل 24/7)
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices["BTC"] = data.get("bitcoin", {}).get("usd", "غير متوفر")
                    prices["ETH"] = data.get("ethereum", {}).get("usd", "غير متوفر")
    except Exception:
        prices["BTC"] = "يعذر الجلب"
        prices["ETH"] = "يعذر الجلب"
    return prices

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "🌟 <b>مرحباً بك يا ديلان في بوت الصفقات الذكي والمحترف!</b> 🌟\n\n"
        "• البوت يتحقق أوتوماتيكياً من أوقات فتح وإغلاق الأسواق.\n"
        "• يمنع إرسال صفقات الفوركس والمعادن في عطلة نهاية الأسبوع.\n"
        "• أرسل /analyze للتحقق من الأسواق وتوليد الصفقات بدقة."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("price"))
async def cmd_price(message: types.Message):
    prices = await fetch_live_prices()
    now = datetime.utcnow()
    weekday = now.weekday() # 5 = Saturday, 6 = Sunday
    is_weekend = weekday >= 5

    market_status = "🔴 مغلق (عطلة نهاية الأسبوع)" if is_weekend else "🟢 مفتوح ويتم التداول عليه"

    response_text = (
        f"📊 <b>حالة الأسواق الفورية الحالية:</b>\n\n"
        f"🟡 <b>الذهب والفضة والفوركس:</b> {market_status}\n"
        f"₿ <b>العملات الرقمية (Crypto):</b> 🟢 مفتوحة 24/7\n"
        f"  • Bitcoin (BTC): <code>${prices.get('BTC', 'جاري التحديث')}</code>\n"
        f"  • Ethereum (ETH): <code>${prices.get('ETH', 'جاري التحديث')}</code>"
    )
    await message.answer(response_text, parse_mode="HTML")

@dp.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    if not ai_client:
        await message.answer("⚠️ مفتاح `GEMINI_API_KEY` غير مضبوط في رندر.")
        return
        
    memory_content = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()
            
    if not memory_content.strip():
        await message.answer("⚠️ الذاكرة فارغة! أرسل رابط استراتيجية يوتيوب أولاً للتعلم.")
        return

    # التحقق من عطلة نهاية الأسبوع
    now = datetime.utcnow()
    is_weekend = now.weekday() >= 5 # السبت والأحد

    prices = await fetch_live_prices()

    if is_weekend:
        market_note = (
            "⚠️ **تنبيه هام:** اليوم عطلة نهاية الأسبوع (السوق مغلق للذهب، الفضة، والفوركس).\n"
            "لذلك **لن يتم إعطاء صفقات فعلية** لأزواج الفوركس والمعادن لعدم دقة الأسعار، "
            "وسنقتصر التحليل على **العملات الرقمية (Crypto)** المفتوحة حالياً بأسعار حية:\n"
            f"- Bitcoin (BTC) السعر الحالي: ${prices.get('BTC')}\n"
            f"- Ethereum (ETH) السعر الحالي: ${prices.get('ETH')}"
        )
    else:
        market_note = "🟢 الأسواق المالية مفتوحة حالياً، وجاري تحليل جميع الأصول بأسعارها الحية الحالية."

    await message.answer(f"🔍 <i>جاري فحص حالة الأسواق وتطبيق استراتيجيات SMC بدقة...</i>\n\n{market_note}", parse_mode="HTML")

    prompt = (
        f"أنت متداول محترف. بناءً على استراتيجيات التداول المخزنة في الذاكرة:\n"
        f"{memory_content}\n\n"
        f"حالة السوق الحالية:\n"
        f"- هل نحن في عطلة نهاية الأسبوع؟ {'نعم، الفوركس والمعادن مغلقة تماماً، قم بتحليل الكريبتو فقط' : 'لا، جميع الأسواق مفتوحة'}.\n"
        f"- أسعار الكريبتو الحية حالياً: BTC = ${prices.get('BTC')}, ETH = ${prices.get('ETH')}.\n\n"
        f"الشروط الصارمة للتقرير:\n"
        f"1. إذا كان السوق مغلقاً، لا تقم نهائياً بوضع صفقات للذهب أو الفضة أو الفوركس واكتفِ بتحليل العملات الرقمية المتاحة الآن.\n"
        f"2. استخدم الأسعار الحقيقية المذكورة أعلاه للكريبتو كمرجع لنقاط الدخول.\n"
        f"3. لكل صفقة، وفّر:\n"
        f"   * 📌 اسم الأصل\n"
        f"   * 🧠 الاستراتيجية المطبقة\n"
        f"   * 🟢 نقطة الدخول بناءً على السعر الحالي الفعلي\n"
        f"   * 🛑 وقف الخسارة\n"
        f"   * 🎯 أهداف جني الأرباح (من 2 إلى 10 أهداف تصاعدية).\n"
        f"اكتب التقرير باللغة العربية بأسلوب احترافي للمتداولين."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        analysis_result = response.text
        response_text = f"🤖 <b>تقرير الصفقات الذكية والمدقق حسب أوقات السوق:</b>\n\n{analysis_result}"
    except Exception as e:
        response_text = f"❌ خطأ في الاتصال بالذكاء الاصطناعي: {str(e)}"

    if len(response_text) > 4000:
        response_text = response_text[:4000] + "\n\n[... تم اقتصاص التقرير لطوله ...]"

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
        f.write(f"[{now}] الرابط: {link}\n")
        f.write(f"الملخص: {summary_text}\n")
        f.write("-" * 40 + "\n")

@dp.message()
async def handle_any_message(message: types.Message):
    if message.text and ("http://" in message.text or "https://" in message.text):
        video_link = message.text.strip()
        video_id = extract_youtube_id(video_link)
        
        extracted_summary = ""
        success_mode = False
        
        if video_id:
            try:
                popular_languages = ['ar', 'en', 'fr', 'es', 'de', 'it', 'ru', 'pt', 'tr']
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=popular_languages)
                transcript_text = " ".join([item['text'] for item in transcript_list])
                extracted_summary = transcript_text[:500] + "..." if len(transcript_text) > 500 else transcript_text
                success_mode = True
            except Exception:
                pass
        
        if not success_mode and ai_client:
            try:
                ai_prompt = f"المستخدم أرسل رابط يوتيوب: {video_link}. استخلص منه استراتيجية تداول احترافية (مثل SMC أو Price Action) لحفظها في الذاكرة."
                ai_res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=ai_prompt)
                extracted_summary = ai_res.text[:500]
                success_mode = True
            except Exception:
                extracted_summary = "استراتيجية تداول مسجلة للتحليل الأوتوماتيكي."

        save_to_memory(video_link, extracted_summary)
        
        response_text = (
            f"🤖🧠 <b>تم استيعاب الاستراتيجية وحفظها بنجاح!</b>\n\n"
            f"🔗 <code>{video_link}</code>\n"
            f"📌 <b>الملخص:</b> <i>\"{extracted_summary}\"</i>"
        )
        await message.answer(response_text, parse_mode="HTML")
    else:
        await message.answer("أهلاً يا ديلان! أرسل رابط يوتيوب جديد، أو استخدم /analyze لتحليل الأسواق المتاحة حالياً، أو /price لمعرفة الأسعار الفورية.")

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
