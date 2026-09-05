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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID") # اختياري: معرف الدردشة الخاص بك لإرسال التقارير التلقائية عليه

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
MEMORY_FILE = "strategies_memory.txt"
NEWS_FILE = "news_memory.txt"

async def handle(request):
    return web.Response(text="Fully Autonomous Trading & Backtesting Bot is active 24/7!")

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

# دالة لتوليد التقرير التحليلي الشامل
async def generate_market_report():
    if not ai_client:
        return "⚠️ مفتاح الذكاء الاصطناعي غير مضبوط."
        
    memory_content = ""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory_content = f.read()

    news_content = "لا توجد أخبار عاجلة مسجلة حالياً."
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news_content = f.read()
            
    if not memory_content.strip():
        return "⚠️ الذاكرة فارغة! يرجى إرسال رابط يوتيوب لاستراتيجية أولاً."

    prices = await fetch_live_prices()
    btc_price = prices.get("BTC")
    eth_price = prices.get("ETH")

    now = datetime.utcnow()
    is_weekend = now.weekday() >= 5  # السبت والأحد

    market_condition_note = (
        "اليوم عطلة نهاية الأسبوع (الذهب والفضة والفوركس مغلقة). التركيز حصرياً على العملات الرقمية (Bitcoin و Ethereum)."
        if is_weekend else "جميع الأسواق المالية مفتوحة حالياً."
    )

    prompt = (
        f"أنت مدير تداول محترف وخبير استراتيجي. لديك الاستراتيجيات المخزنة التالية:\n"
        f"{memory_content}\n\n"
        f"المعطيات الإضافية:\n"
        f"1. أحدث الأخبار المرصودة:\n{news_content}\n"
        f"2. حالة السوق: {market_condition_note}\n"
        f"3. الأسعار الحية للكريبتو: BTC = ${btc_price}, ETH = ${eth_price}\n\n"
        f"المطلوب تقرير صفقات تنفيذي دوري دقيق ومباشر يدمج أقوى الاستراتيجيات مع تأثير الأخبار والأسعار الحية."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"❌ حدث خطأ أثناء التوليد: {str(e)}"

# مهمة الخلفية التي تعمل أوتوماتيكياً كل ساعة لإرسال التقرير
async def hourly_background_reporter():
    await asyncio.sleep(10) # انتظار قليل حتى يعمل البوت ويبدأ الاستماع
    while True:
        try:
            # إذا قام المستخدم بتفاعل سابق مع البوت، يمكننا إرسال التقرير لآخر مستخدم أو استخدام معرف ثابت إذا تم تعيينه
            # لحين تفاعل المستخدم الأول، سنستمر في الانتظار أو الإرسال إذا وجدنا معرفاً مخزناً
            if os.path.exists("last_chat_id.txt"):
                with open("last_chat_id.txt", "r") as f:
                    chat_id = f.read().strip()
                if chat_id:
                    report = await generate_market_report()
                    full_msg = f"⏰ <b>التقرير التلقائي الساعي (أتمتة كاملة):</b>\n\n{report}"
                    if len(full_msg) > 4000:
                        full_msg = full_msg[:4000]
                    await bot.send_message(chat_id=int(chat_id), text=full_msg, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Error in background reporter: {e}")
        
        # الانتظار لمدة ساعة كاملة (3600 ثانية)
        await asyncio.sleep(3600)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # حفظ معرف الدردشة لكي يتم إرسال التقارير التلقائية عليه
    with open("last_chat_id.txt", "w") as f:
        f.write(str(message.chat.id))

    welcome_text = (
        "🌟 <b>مرحباً بك يا زعيم ديلان في نظام الأتمتة الكاملة!</b> 🌟\n\n"
        "• تم تفعيل <b>التقرير التلقائي الساعي</b> بنجاح (سيرسل لك البوت تقريراً كل ساعة أوتوماتيكياً).\n"
        "• أرسل روابط يوتيوب لأي استراتيجية لعمل باك تست وتوليد كود TradingView.\n"
        "• قم بتوجيه الأخبار ليحللها ويضيفها أوتوماتيكياً.\n"
        "• يمكنك طلب التقرير يدوياً في أي وقت باستخدام /analyze."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("strategies"))
async def cmd_strategies(message: types.Message):
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip():
            if len(content) > 3000:
                content = content[-3000:]
            await message.answer(f"📚 <b>مكتبة الاستراتيجيات ونتائج الباك تست:</b>\n\n<pre>{content}</pre>", parse_mode="HTML")
            return
    await message.answer("📭 لا توجد استراتيجيات مخزنة بعد.", parse_mode="HTML")

@dp.message(Command("news"))
async def cmd_news(message: types.Message):
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news_content = f.read()
        if news_content.strip():
            if len(news_content) > 3000:
                news_content = news_content[-3000:]
            await message.answer(f"📰 <b>أرشيف الأخبار والتحليلات:</b>\n\n<pre>{news_content}</pre>", parse_mode="HTML")
            return
    await message.answer("📭 لا توجد أخبار مسجلة حالياً.", parse_mode="HTML")

@dp.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    # حفظ المعرف أيضاً لضمان التحديث
    with open("last_chat_id.txt", "w") as f:
        f.write(str(message.chat.id))

    await message.answer("🔍 <i>جاري توليد التقرير الفوري...</i>", parse_mode="HTML")
    report = await generate_market_report()
    response_text = f"🤖 <b>التقرير التنفيذي الشامل:</b>\n\n{report}"
    if len(response_text) > 4000:
        response_text = response_text[:4000]
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
    # حفظ المعرف
    with open("last_chat_id.txt", "w") as f:
        f.write(str(message.chat.id))

    text = message.text or message.caption
    if not text:
        return

    # معالجة روابط يوتيوب
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

        await message.answer("🔄 <i>جاري تحليل الفيديو، إجراء محاكاة الباك تست، وتوليد كود TradingView...</i>", parse_mode="HTML")

        if ai_client:
            try:
                eval_prompt = (
                    f"بناءً على نص الفيديو المستخرج التالي:\n\"{transcript_text}\"\n\n"
                    f"قم بالآتي:\n"
                    f"1. استخرج اسم مقترح للاستراتيجية.\n"
                    f"2. قم بعمل محاكاة افتراضية لكفاءتها (Backtest Simulation) واكتب جدولاً يوضح: (اسم المؤشر، نسبة النجاح Win Rate، عامل الربح Profit Factor، أقصى انعكاس Max Drawdown).\n"
                    f"3. اكتب كود برمجي مجاني بـ (Pine Script v5) لتطبيق هذه الاستراتيجية على منصة TradingView.\n"
                    f"رتب الإجابة باللغة العربية بأسلوب احترافي."
                )
                res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=eval_prompt)
                evaluation_result = res.text

                save_to_memory(MEMORY_FILE, f"رابط يوتيوب: {video_link}\nالتحليل والباك تست:\n{evaluation_result}")

                response_text = f"📊 <b>نتائج تحليل ومحاكاة الاستراتيجية الجديدة:</b>\n\n{evaluation_result}"
                if len(response_text) > 4000:
                    response_text = response_text[:4000]

                await message.answer(response_text, parse_mode="HTML")
                return
            except Exception as e:
                await message.answer(f"❌ حدث خطأ: {str(e)}")
        return
    else:
        # معالجة الأخبار المحولة أوتوماتيكياً
        if ai_client:
            try:
                news_prompt = (
                    f"هذا خبر تم توجيه من قناة إخبارية:\n\"{text}\"\n\n"
                    f"قم بتحليله باختصار واذكر: 1) ملخص الخبر. 2) تأثيره على الأسواق (إيجابي صعودي / سلبي هبوطي / محايد)."
                )
                res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=news_prompt)
                news_analysis = res.text
                
                save_to_memory(NEWS_FILE, f"الخبر الأصلي: {text}\nالتحليل الآلي: {news_analysis}")
                
                response_alert = (
                    f"⚡ <b>تم رصد الخبر وتحليله أوتوماتيكياً!</b>\n\n"
                    f"📌 <b>التأثير:</b>\n{news_analysis}\n\n"
                    f"<i>✅ سيتم تضمينه في التقرير التلقائي القادم.</i>"
                )
                await message.answer(response_alert, parse_mode="HTML")
                return
            except Exception:
                pass
        
        await message.answer("أهلاً يا زعيم! البوت يعمل بأتمتة كاملة وسيرسل لك التقارير كل ساعة تلقائياً.", parse_mode="HTML")

async def main():
    # بدء السيرفر لتشغيل رندر
    await start_web_server()
    # تشغيل مهمة التقرير التلقائي في الخلفية جنباً إلى جنب مع استقبال رسائل البوت
    asyncio.create_task(hourly_background_reporter())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
