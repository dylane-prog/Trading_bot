import os
import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from google import genai


# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Example:
# GEMINI_API_KEYS=KEY1,KEY2,KEY3,KEY4,KEY5,KEY6,KEY7
GEMINI_KEYS_RAW = os.getenv("GEMINI_API_KEYS", "")

GEMINI_KEYS = [
    key.strip()
    for key in GEMINI_KEYS_RAW.split(",")
    if key.strip()
]

# Main model.
# You can change it from Render Environment Variables.
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
).strip()

# Automatic model fallbacks.
MODEL_FALLBACKS = [
    GEMINI_MODEL,
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]

# Remove duplicates while preserving order.
MODEL_FALLBACKS = list(dict.fromkeys(MODEL_FALLBACKS))

PORT = int(os.getenv("PORT", "10000"))

NEWS_FILE = Path("news_memory.txt")
STRATEGY_FILE = Path("strategies_memory.txt")
ERROR_FILE = Path("bot_errors.log")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | TradingBot | %(message)s"
)

logger = logging.getLogger("TradingBot")


# =========================================================
# BASIC VALIDATION
# =========================================================

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is not configured.")

if not GEMINI_KEYS:
    logger.warning("GEMINI_API_KEYS is not configured.")

logger.info(
    "Loaded %s Gemini API key(s).",
    len(GEMINI_KEYS)
)

logger.info(
    "Gemini primary model: %s",
    GEMINI_MODEL
)

logger.info(
    "Gemini model fallbacks: %s",
    ", ".join(MODEL_FALLBACKS)
)


# =========================================================
# TELEGRAM
# =========================================================

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


# =========================================================
# GEMINI KEY MANAGER
# =========================================================

class KeyManager:

    def __init__(self, keys):
        self.keys = keys
        self.current_index = 0
        self.cooldowns = {}
        self.lock = asyncio.Lock()

    def count(self):
        return len(self.keys)

    async def get_available_key(self):
        """
        Returns:
            (index, key)
        or
            (None, None)
        """

        if not self.keys:
            return None, None

        async with self.lock:

            now = time.time()

            # Try every key starting from current index.
            for offset in range(len(self.keys)):

                index = (
                    self.current_index + offset
                ) % len(self.keys)

                cooldown_until = self.cooldowns.get(
                    index,
                    0
                )

                if cooldown_until <= now:

                    self.current_index = (
                        index + 1
                    ) % len(self.keys)

                    return index, self.keys[index]

            return None, None

    async def cooldown(
        self,
        index,
        seconds=60
    ):
        if index is None:
            return

        async with self.lock:

            self.cooldowns[index] = (
                time.time() + seconds
            )

            logger.warning(
                "Gemini key #%s placed in cooldown for %s seconds.",
                index + 1,
                seconds
            )

    async def reset_cooldown(self, index):

        if index is None:
            return

        async with self.lock:
            self.cooldowns.pop(index, None)


key_manager = KeyManager(GEMINI_KEYS)


# =========================================================
# GEMINI CLIENT
# =========================================================

def create_client(api_key):
    return genai.Client(
        api_key=api_key
    )


# =========================================================
# ERROR CLASSIFICATION
# =========================================================

def error_text(error):
    try:
        return str(error).lower()
    except Exception:
        return ""


def is_model_error(error):
    text = error_text(error)

    return (
        "404" in text
        and (
            "model" in text
            or "not_found" in text
            or "not found" in text
        )
    )


def is_rate_limit_error(error):
    text = error_text(error)

    return (
        "429" in text
        or "resource_exhausted" in text
        or "rate limit" in text
        or "quota" in text
    )


def is_auth_error(error):
    text = error_text(error)

    return (
        "401" in text
        or "403" in text
        or "permission denied" in text
        or "api key" in text
        and "invalid" in text
    )


def is_server_error(error):
    text = error_text(error)

    return (
        "500" in text
        or "502" in text
        or "503" in text
        or "504" in text
        or "internal server error" in text
        or "service unavailable" in text
    )


# =========================================================
# SAVE ERRORS
# =========================================================

def save_error(error):

    try:

        with ERROR_FILE.open(
            "a",
            encoding="utf-8"
        ) as f:

            f.write(
                f"\n[{datetime.now().isoformat()}]\n"
            )

            f.write(
                f"{repr(error)}\n"
            )

            f.write(
                "=" * 60 + "\n"
            )

    except Exception:
        pass


# =========================================================
# GEMINI GENERATION
# =========================================================

async def safe_ai_generate(
    prompt,
    fallback_text=None
):

    if not GEMINI_KEYS:

        return (
            fallback_text
            or
            "❌ لا توجد مفاتيح Gemini في إعدادات Render."
        )

    last_error = None

    # We try several model/key combinations.
    #
    # Important:
    # A 404 model error does NOT put the key in cooldown.
    # This avoids wasting all 7 keys when the model itself is wrong.

    for model in MODEL_FALLBACKS:

        for attempt in range(
            max(1, len(GEMINI_KEYS) * 2)
        ):

            key_index, api_key = (
                await key_manager.get_available_key()
            )

            if api_key is None:

                # All keys are temporarily cooling down.
                await asyncio.sleep(2)

                key_index, api_key = (
                    await key_manager.get_available_key()
                )

                if api_key is None:
                    continue

            try:

                logger.info(
                    "Gemini request | model=%s | key=%s",
                    model,
                    key_index + 1
                )

                client = create_client(api_key)

                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model,
                    contents=prompt
                )

                if response:

                    text = getattr(
                        response,
                        "text",
                        None
                    )

                    if text and text.strip():

                        await key_manager.reset_cooldown(
                            key_index
                        )

                        return text.strip()

                last_error = Exception(
                    "Gemini returned an empty response."
                )

            except Exception as error:

                last_error = error

                save_error(error)

                logger.error(
                    "Gemini error | model=%s | key=%s | %s",
                    model,
                    key_index + 1,
                    error
                )

                # -------------------------------------------------
                # MODEL ERROR
                # -------------------------------------------------

                if is_model_error(error):

                    logger.warning(
                        "Model %s is unavailable. Trying another model.",
                        model
                    )

                    # DO NOT cooldown the key.
                    break

                # -------------------------------------------------
                # RATE LIMIT / QUOTA
                # -------------------------------------------------

                if is_rate_limit_error(error):

                    await key_manager.cooldown(
                        key_index,
                        seconds=60
                    )

                    continue

                # -------------------------------------------------
                # AUTHENTICATION
                # -------------------------------------------------

                if is_auth_error(error):

                    # Keep the key out for longer.
                    await key_manager.cooldown(
                        key_index,
                        seconds=300
                    )

                    continue

                # -------------------------------------------------
                # SERVER ERROR
                # -------------------------------------------------

                if is_server_error(error):

                    await asyncio.sleep(2)

                    continue

                # -------------------------------------------------
                # OTHER ERROR
                # -------------------------------------------------

                await asyncio.sleep(1)

    logger.error(
        "All Gemini attempts failed. Last error: %s",
        last_error
    )

    return (
        fallback_text
        or
        "❌ تعذر تحليل الطلب بواسطة Gemini حاليًا. "
        "تم تسجيل الخطأ للمراجعة."
    )


# =========================================================
# WEB SERVER FOR RENDER
# =========================================================

async def health(request):

    return web.json_response(
        {
            "status": "online",
            "service": "Trading Bot",
            "gemini_keys": len(GEMINI_KEYS),
            "gemini_model": GEMINI_MODEL,
            "models": MODEL_FALLBACKS,
            "time": datetime.now(
                timezone.utc
            ).isoformat()
        }
    )


async def home(request):

    return web.Response(
        text=(
            "Trading Bot is online.\n"
            f"Gemini keys: {len(GEMINI_KEYS)}\n"
            f"Primary model: {GEMINI_MODEL}\n"
        )
    )


app = web.Application()

app.add_routes(
    [
        web.get("/", home),
        web.get("/health", health),
    ]
)


async def start_web_server():

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info(
        "Web server started on port %s",
        PORT
    )


# =========================================================
# FILE MEMORY
# =========================================================

def save_memory(
    file_path,
    title,
    content,
    analysis
):

    try:

        with file_path.open(
            "a",
            encoding="utf-8"
        ) as f:

            f.write(
                f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n"
            )

            f.write(
                f"{title}\n"
            )

            f.write(
                f"INPUT:\n{content}\n\n"
            )

            f.write(
                f"ANALYSIS:\n{analysis}\n"
            )

            f.write(
                "\n" + "=" * 70 + "\n"
            )

    except Exception as error:

        logger.error(
            "Could not save memory: %s",
            error
        )


# =========================================================
# START COMMAND
# =========================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):

    text = (
        "👑 **مرحبًا بك في Trading Bot**\n\n"

        "📊 **التحليل الحالي:**\n"
        "النظام جاهز لاستقبال الأخبار والرسائل وتحليلها بواسطة Gemini.\n\n"

        "📰 **الأخبار:**\n"
        "قم بعمل Forward لأي خبر من قناة Telegram إلى البوت.\n\n"

        "🧠 **الاستراتيجيات:**\n"
        "أرسل نص استراتيجية أو رابطًا لها وسيقوم البوت بتحليلها.\n\n"

        "🤖 **Gemini:**\n"
        f"الموديل الأساسي: `{GEMINI_MODEL}`\n"
        f"عدد المفاتيح المتاحة: `{len(GEMINI_KEYS)}`\n\n"

        "🔧 **الأوامر:**\n"
        "/status - حالة البوت\n"
        "/test_ai - اختبار Gemini\n"
        "/help - المساعدة\n\n"

        "⚠️ نظام التداول الحقيقي وBacktest الحقيقي "
        "سيتم ربطهما ببيانات السوق الفعلية، وليس بأرقام عشوائية."
    )

    await message.answer(
        text,
        parse_mode="Markdown"
    )


# =========================================================
# HELP
# =========================================================

@dp.message(Command("help"))
async def cmd_help(message: types.Message):

    await message.answer(
        "📚 **طريقة الاستخدام**\n\n"

        "1️⃣ **خبر من Telegram**\n"
        "اعمل Forward للخبر إلى البوت.\n\n"

        "2️⃣ **استراتيجية**\n"
        "أرسل نص الاستراتيجية أو رابطها.\n\n"

        "3️⃣ **اختبار Gemini**\n"
        "استخدم `/test_ai`.\n\n"

        "4️⃣ **حالة النظام**\n"
        "استخدم `/status`.",
        parse_mode="Markdown"
    )


# =========================================================
# STATUS COMMAND
# =========================================================

@dp.message(Command("status"))
async def cmd_status(message: types.Message):

    available = 0

    now = time.time()

    for index in range(
        len(GEMINI_KEYS)
    ):

        cooldown_until = (
            key_manager.cooldowns.get(
                index,
                0
            )
        )

        if cooldown_until <= now:
            available += 1

    status = (
        "🟢 ONLINE"
        if bot
        else
        "🔴 BOT_TOKEN MISSING"
    )

    text = (
        "📡 **Trading Bot Status**\n\n"

        f"Bot: `{status}`\n"
        f"Gemini keys configured: `{len(GEMINI_KEYS)}`\n"
        f"Gemini keys available now: `{available}`\n"
        f"Primary model: `{GEMINI_MODEL}`\n"
        f"Fallback models: `{len(MODEL_FALLBACKS)}`\n"
        f"UTC time: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}`"
    )

    await message.answer(
        text,
        parse_mode="Markdown"
    )


# =========================================================
# TEST GEMINI
# =========================================================

@dp.message(Command("test_ai"))
async def cmd_test_ai(message: types.Message):

    await message.answer(
        "🧠 جاري اختبار اتصال Gemini..."
    )

    result = await safe_ai_generate(
        (
            "أجب بالعربية باختصار شديد. "
            "قل إن اتصال Gemini يعمل بنجاح، "
            "ولا تضف أي معلومات أخرى."
        ),
        fallback_text=(
            "❌ فشل اختبار Gemini."
        )
    )

    await message.answer(
        "🧪 **نتيجة الاختبار:**\n\n"
        + result,
        parse_mode="Markdown"
    )


# =========================================================
# MARKET COMMANDS
# =========================================================

async def market_not_ready(
    message: types.Message,
    asset_name: str
):

    await message.answer(
        f"📊 **{asset_name}**\n\n"
        "⚠️ محرك بيانات السوق المباشرة لم يتم ربطه "
        "بهذا الإصدار بعد.\n\n"
        "لن أعطيك سعرًا وهميًا أو صفقة مبنية على "
        "بيانات عشوائية.\n\n"
        "الخطوة التالية هي ربط مصدر OHLCV حقيقي "
        "ثم بناء التحليل الفني وTrade Manager."
    )


@dp.message(Command("gold"))
async def c_gold(message: types.Message):

    await market_not_ready(
        message,
        "الذهب XAU/USD"
    )


@dp.message(Command("btc"))
async def c_btc(message: types.Message):

    await market_not_ready(
        message,
        "Bitcoin BTC/USD"
    )


@dp.message(Command("eurusd"))
async def c_eurusd(message: types.Message):

    await market_not_ready(
        message,
        "EUR/USD"
    )


@dp.message(Command("silver"))
async def c_silver(message: types.Message):

    await market_not_ready(
        message,
        "Silver XAG/USD"
    )


@dp.message(Command("oil"))
async def c_oil(message: types.Message):

    await market_not_ready(
        message,
        "WTI Oil"
    )


@dp.message(Command("eth"))
async def c_eth(message: types.Message):

    await market_not_ready(
        message,
        "Ethereum ETH/USD"
    )


# =========================================================
# FORWARD DETECTION
# =========================================================

def get_forward_info(message: types.Message):

    origin = getattr(
        message,
        "forward_origin",
        None
    )

    if not origin:
        return None

    info = {
        "source": "Unknown",
        "date": None,
        "message_id": None
    }

    try:

        origin_type = getattr(
            origin,
            "type",
            ""
        )

        if origin_type == "channel":

            chat = getattr(
                origin,
                "chat",
                None
            )

            if chat:

                info["source"] = (
                    getattr(
                        chat,
                        "title",
                        None
                    )
                    or
                    getattr(
                        chat,
                        "username",
                        None
                    )
                    or
                    "Telegram Channel"
                )

            info["message_id"] = getattr(
                origin,
                "message_id",
                None
            )

        elif origin_type == "user":

            sender_user = getattr(
                origin,
                "sender_user",
                None
            )

            if sender_user:

                first_name = getattr(
                    sender_user,
                    "first_name",
                    ""
                )

                last_name = getattr(
                    sender_user,
                    "last_name",
                    ""
                )

                info["source"] = (
                    f"{first_name} {last_name}"
                ).strip() or "Telegram User"

        elif origin_type == "hidden_user":

            info["source"] = getattr(
                origin,
                "sender_user_name",
                "Hidden Telegram User"
            )

        info["date"] = getattr(
            origin,
            "date",
            None
        )

    except Exception as error:

        logger.warning(
            "Could not parse forward origin: %s",
            error
        )

    return info


# =========================================================
# NEWS ANALYSIS
# =========================================================

async def analyze_news(
    message: types.Message,
    text: str,
    forward_info
):

    source = (
        forward_info["source"]
        if forward_info
        else
        "Telegram"
    )

    date = (
        forward_info["date"]
        if forward_info
        else
        datetime.now(timezone.utc)
    )

    prompt = f"""
أنت محلل أخبار اقتصادي محترف متخصص في أسواق الفوركس
والذهب والعملات الرقمية والنفط.

وصل خبر من Telegram.

المصدر:
{source}

وقت الخبر:
{date}

نص الخبر:
{text}

حلل الخبر بدون اختلاق أي معلومات غير موجودة.

أعطني التقرير بهذا الترتيب:

1. 📰 ملخص الخبر
2. 🎯 الأصول التي قد تتأثر
3. 📈 الاتجاه المحتمل لكل أصل:
   BUY / SELL / NEUTRAL
4. ⚡ قوة التأثير:
   LOW / MEDIUM / HIGH / EXTREME
5. ⏱️ مدة التأثير المحتملة:
   دقائق / ساعات / يوم / عدة أيام
6. 📊 لماذا قد يتأثر السوق؟
7. ⚠️ المخاطر وما الذي قد يبطل التأثير
8. 🔎 ما البيانات التي يجب مراقبتها بعد الخبر؟
9. 🚫 لا تعطِ صفقة مباشرة إذا لم توجد معلومات كافية.

مهم:
لا تدّعِ أنك تملك سعرًا لحظيًا إذا لم يتم تزويدك به.
لا تخترع أرقامًا أو أسعارًا.
"""

    analysis = await safe_ai_generate(
        prompt,
        fallback_text=(
            "❌ تعذر تحليل الخبر حاليًا، "
            "لكن تم حفظه في ذاكرة البوت."
        )
    )

    save_memory(
        NEWS_FILE,
        f"NEWS FROM: {source}",
        text,
        analysis
    )

    return analysis


# =========================================================
# STRATEGY ANALYSIS
# =========================================================

async def analyze_strategy(
    text: str
):

    prompt = f"""
أنت مهندس استراتيجيات تداول وباحث Quant.

حلل الاستراتيجية التالية:

{text}

أريد استخراجها بطريقة قابلة للتحويل لاحقًا إلى Backtest حقيقي.

اكتب:

1. اسم الاستراتيجية
2. السوق المناسب
3. Timeframe
4. شروط BUY بالتحديد
5. شروط SELL بالتحديد
6. شروط الدخول
7. Stop Loss
8. Take Profit
9. إدارة الصفقة
10. شروط الخروج
11. المؤشرات المطلوبة
12. الحالات التي تمنع الدخول
13. هل يمكن تحويلها إلى قواعد برمجية واضحة؟
14. ما المعلومات الناقصة؟
15. كيف يجب اختبارها تاريخيًا؟

مهم جدًا:
لا تدّعي أن الاستراتيجية رابحة.
لا تخترع نتائج Backtest.
لا تقل إنها حققت نسبة نجاح معينة بدون بيانات تاريخية فعلية.
"""

    analysis = await safe_ai_generate(
        prompt,
        fallback_text=(
            "❌ تعذر تحليل الاستراتيجية حاليًا."
        )
    )

    save_memory(
        STRATEGY_FILE,
        "STRATEGY",
        text,
        analysis
    )

    return analysis


# =========================================================
# GENERAL TEXT / FORWARDED NEWS / STRATEGIES
# =========================================================

@dp.message()
async def handle_all_messages(
    message: types.Message
):

    # Ignore commands not caught above.
    if message.text and message.text.startswith("/"):
        return

    text = (
        message.text
        or
        message.caption
        or
        ""
    ).strip()

    # -----------------------------------------------------
    # FORWARDED MESSAGE
    # -----------------------------------------------------

    forward_info = get_forward_info(
        message
    )

    if forward_info:

        if not text:

            await message.answer(
                "⚠️ استلمت الرسالة المُعاد توجيهها، "
                "لكن لا يوجد نص أو Caption يمكن تحليله في هذا الإصدار."
            )

            return

        source = forward_info["source"]

        await message.answer(
            f"📰 تم استلام خبر مُعاد توجيهه من:\n"
            f"**{source}**\n\n"
            f"🧠 جاري التحليل...",
            parse_mode="Markdown"
        )

        analysis = await analyze_news(
            message,
            text,
            forward_info
        )

        await message.answer(
            "📊 **تحليل الخبر:**\n\n"
            + analysis,
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------
    # EMPTY MESSAGE
    # -----------------------------------------------------

    if not text:

        await message.answer(
            "⚠️ أرسل نصًا أو Caption أو قم بعمل Forward لخبر."
        )

        return

    # -----------------------------------------------------
    # DETECT STRATEGY / URL
    # -----------------------------------------------------

    lower_text = text.lower()

    is_url = (
        "http://" in lower_text
        or
        "https://" in lower_text
        or
        "www." in lower_text
    )

    strategy_keywords = [
        "استراتيجية",
        "استراتيجيه",
        "strategy",
        "trading strategy",
        "tradingview",
        "pine script",
        "backtest",
        "مؤشر",
        "indicator",
        "buy signal",
        "sell signal"
    ]

    is_strategy = (
        is_url
        or
        any(
            keyword in lower_text
            for keyword in strategy_keywords
        )
    )

    # -----------------------------------------------------
    # STRATEGY
    # -----------------------------------------------------

    if is_strategy:

        await message.answer(
            "🧠 تم التعرف على محتوى متعلق باستراتيجية.\n"
            "جاري استخراج قواعدها..."
        )

        analysis = await analyze_strategy(
            text
        )

        await message.answer(
            "🧪 **تحليل الاستراتيجية:**\n\n"
            + analysis,
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------
    # GENERAL TEXT = NEWS
    # -----------------------------------------------------

    await message.answer(
        "📰 تم استلام النص.\n"
        "جاري تحليله كخبر/معلومة سوقية..."
    )

    analysis = await analyze_news(
        message,
        text,
        None
    )

    await message.answer(
        "📊 **تحليل المحتوى:**\n\n"
        + analysis,
        parse_mode="Markdown"
    )


# =========================================================
# GLOBAL ERROR HANDLER
# =========================================================

async def global_error_handler(
    event,
    exception
):

    logger.exception(
        "Unhandled Telegram error: %s",
        exception
    )

    save_error(exception)


# =========================================================
# STARTUP
# =========================================================

async def main():

    if not BOT_TOKEN:

        logger.error(
            "BOT_TOKEN is missing. "
            "Set BOT_TOKEN in Render Environment Variables."
        )

        return

    if not GEMINI_KEYS:

        logger.warning(
            "No GEMINI_API_KEYS found. "
            "Bot will start but AI analysis will fail."
        )

    await start_web_server()

    logger.info(
        "=============================================="
    )

    logger.info(
        "Trading Bot starting..."
    )

    logger.info(
        "Gemini keys: %s",
        len(GEMINI_KEYS)
    )

    logger.info(
        "Primary model: %s",
        GEMINI_MODEL
    )

    logger.info(
        "=============================================="
    )

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    finally:

        if bot:

            await bot.session.close()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Trading Bot stopped."
        )

    except Exception as error:

        logger.exception(
            "Fatal application error: %s",
            error
        )

        save_error(error)
