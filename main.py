import os
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from google import genai


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

GEMINI_KEYS_RAW = os.getenv("GEMINI_API_KEYS", "")
GEMINI_KEYS = [
    key.strip()
    for key in GEMINI_KEYS_RAW.split(",")
    if key.strip()
]

# يمكنك تغيير النموذج من Render بدون تعديل الكود
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = Path(__file__).resolve().parent

NEWS_FILE = BASE_DIR / "news_memory.txt"
STRATEGY_FILE = BASE_DIR / "strategies_memory.txt"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("TradingBot")


# ============================================================
# VALIDATION
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN غير موجود في Environment Variables."
    )

if not GEMINI_KEYS:
    raise RuntimeError(
        "GEMINI_API_KEYS غير موجود أو فارغ."
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.MARKDOWN
    )
)

dp = Dispatcher()


# ============================================================
# GEMINI KEY MANAGER
# ============================================================

class KeyManager:

    def __init__(self, keys):
        self.keys = list(dict.fromkeys(keys))
        self.index = 0
        self.lock = asyncio.Lock()

        # key -> cooldown timestamp
        self.cooldowns = {}

        logger.info(
            "Gemini KeyManager loaded with %d unique keys.",
            len(self.keys)
        )

    def _available_indices(self):
        now = asyncio.get_running_loop().time()

        available = []

        for i in range(len(self.keys)):
            cooldown_until = self.cooldowns.get(i, 0)

            if cooldown_until <= now:
                available.append(i)

        return available

    async def get_next_client(self):
        """
        اختيار المفتاح التالي بطريقة Round-Robin.
        لا نستخدم مفتاحاً موجوداً في cooldown.
        """

        async with self.lock:

            available = self._available_indices()

            if not available:
                return None, None

            for _ in range(len(self.keys)):

                index = self.index
                self.index = (self.index + 1) % len(self.keys)

                if index in available:
                    client = genai.Client(
                        api_key=self.keys[index]
                    )

                    return client, index

            return None, None

    async def cooldown_key(
        self,
        index,
        seconds=60
    ):
        """
        عند حدوث Rate Limit أو فشل مؤقت،
        نعزل المفتاح مؤقتاً بدلاً من إسقاط البوت.
        """

        async with self.lock:

            self.cooldowns[index] = (
                asyncio.get_running_loop().time()
                + seconds
            )

            logger.warning(
                "Gemini key #%d placed in cooldown for %d seconds.",
                index + 1,
                seconds
            )

    async def available_count(self):

        async with self.lock:

            return len(
                self._available_indices()
            )


key_manager = KeyManager(GEMINI_KEYS)


# ============================================================
# GEMINI AI
# ============================================================

async def safe_ai_generate(
    prompt,
    fallback_text=None,
    attempts=None
):
    """
    نظام آمن للذكاء الاصطناعي.

    - يستخدم المفاتيح السبعة بالتناوب.
    - لا يجعل استثناء Gemini يوقف البوت.
    - الطلب المتزامن يتم تشغيله خارج Event Loop.
    """

    if fallback_text is None:
        fallback_text = (
            "⚠️ تعذر الحصول على تحليل الذكاء الاصطناعي حالياً."
        )

    if attempts is None:
        attempts = max(7, len(GEMINI_KEYS) * 2)

    last_error = None

    for attempt in range(attempts):

        client, key_index = await key_manager.get_next_client()

        if client is None:

            # كل المفاتيح في cooldown
            logger.warning(
                "All Gemini keys are temporarily unavailable."
            )

            await asyncio.sleep(5)
            continue

        try:

            response = await asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt
            )

            text = getattr(response, "text", None)

            if text:
                logger.info(
                    "Gemini request succeeded using key #%d.",
                    key_index + 1
                )

                return text.strip()

            raise RuntimeError(
                "Gemini returned an empty response."
            )

        except Exception as exc:

            last_error = exc

            error_text = str(exc).lower()

            logger.exception(
                "Gemini error with key #%d.",
                key_index + 1
            )

            # إذا كان الخطأ يشير إلى quota/rate limit
            # نعزل المفتاح مؤقتاً.
            if any(word in error_text for word in [
                "quota",
                "rate",
                "429",
                "resource exhausted",
                "too many requests"
            ]):
                cooldown = 120
            else:
                cooldown = 30

            await key_manager.cooldown_key(
                key_index,
                cooldown
            )

            await asyncio.sleep(1)

    logger.error(
        "All Gemini attempts failed. Last error: %s",
        last_error
    )

    return fallback_text


# ============================================================
# FILE MEMORY
# ============================================================

def append_memory(
    file_path,
    source,
    content,
    analysis
):

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    try:

        with open(
            file_path,
            "a",
            encoding="utf-8"
        ) as f:

            f.write(
                "\n"
                + "=" * 60
                + "\n"
            )

            f.write(
                f"TIME: {timestamp}\n"
            )

            f.write(
                f"SOURCE: {source}\n"
            )

            f.write(
                f"INPUT:\n{content}\n\n"
            )

            f.write(
                f"ANALYSIS:\n{analysis}\n"
            )

            f.write(
                "=" * 60
                + "\n"
            )

    except Exception:
        logger.exception(
            "Could not write memory file."
        )


# ============================================================
# FORWARD INFORMATION
# ============================================================

def get_forward_source(message: types.Message):

    origin = message.forward_origin

    if origin is None:
        return None

    try:

        # MessageOriginChannel
        if hasattr(origin, "chat"):

            chat = origin.chat

            title = getattr(
                chat,
                "title",
                None
            )

            username = getattr(
                chat,
                "username",
                None
            )

            if username:
                source = f"@{username}"
            elif title:
                source = title
            else:
                source = "Telegram Channel"

            return {
                "type": "channel",
                "name": source,
                "date": getattr(
                    origin,
                    "date",
                    None
                ),
                "message_id": getattr(
                    origin,
                    "message_id",
                    None
                )
            }

        # Other forward types
        sender_name = getattr(
            origin,
            "sender_user",
            None
        )

        return {
            "type": "forward",
            "name": str(sender_name or "Telegram"),
            "date": getattr(
                origin,
                "date",
                None
            ),
            "message_id": None
        }

    except Exception:

        logger.exception(
            "Failed to parse forward origin."
        )

        return {
            "type": "forward",
            "name": "Telegram",
            "date": None,
            "message_id": None
        }


# ============================================================
# NEWS ANALYZER
# ============================================================

async def analyze_forwarded_news(
    message: types.Message
):

    source_info = get_forward_source(message)

    text = message.text or message.caption or ""

    if not text.strip():

        await message.answer(
            "⚠️ وصلت رسالة Forward ولكن لا تحتوي على نص.\n"
            "حالياً سنضيف تحليل الصور والوسائط في المرحلة التالية."
        )

        return

    source_name = (
        source_info["name"]
        if source_info
        else "Telegram"
    )

    received_time = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    await message.answer(
        "📨 *تم استلام الخبر من Forward.*\n\n"
        f"📰 المصدر: `{source_name}`\n"
        f"🕐 وقت الاستلام: `{received_time}`\n\n"
        "🧠 جاري تحليل الخبر..."
    )

    prompt = f"""
أنت محلل أخبار وأسواق محترف.

وصلني خبر من Telegram.

المصدر:
{source_name}

وقت الاستلام:
{received_time}

نص الخبر:
{text}

حلل الخبر بطريقة منظمة.

أريد منك تحديد:

1. ملخص الخبر.
2. هل الخبر حقيقي/واضح من النص أم توجد درجة عدم يقين؟
3. الأصول المحتمل تأثرها:
   - XAU/USD
   - XAG/USD
   - EUR/USD
   - GBP/USD
   - USD/JPY
   - AUD/USD
   - USD/CAD
   - USD/CHF
   - NZD/USD
   - BTC/USD
   - ETH/USD
   - WTI
   - Brent
4. قوة التأثير:
   NONE / LOW / MEDIUM / HIGH / EXTREME
5. الاتجاه المحتمل لكل أصل:
   BULLISH / BEARISH / MIXED / UNKNOWN
6. المدة التقريبية التي قد يبقى فيها تأثير الخبر:
   بالدقائق، مع ذكر أنها تقديرية.
7. هل يستحق الخبر إعادة تحليل صفقة مفتوحة فوراً؟
   YES / NO
8. ما المعلومات التي يجب فحصها من السوق قبل اتخاذ قرار؟
9. لا تعطِ ضماناً للربح.
10. لا تخترع أرقام أسعار غير موجودة في البيانات.

مهم:
هذا تحليل للخبر فقط.
لا تفترض وجود صفقة مفتوحة إذا لم يتم تزويدك بها.
"""

    analysis = await safe_ai_generate(
        prompt,
        fallback_text=(
            "⚠️ تعذر تحليل الخبر حالياً. "
            "سيتم الاحتفاظ بالخبر للمراجعة."
        )
    )

    append_memory(
        NEWS_FILE,
        source_name,
        text,
        analysis
    )

    response = (
        "📰 *تحليل الخبر المُعاد توجيهه*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📡 المصدر: `{source_name}`\n"
        f"🕐 الاستلام: `{received_time}`\n\n"
        f"🧠 *التحليل:*\n{analysis}\n\n"
        "💾 تم حفظ الخبر في ذاكرة البوت."
    )

    # Telegram message limit protection
    if len(response) <= 4000:

        await message.answer(response)

    else:

        await message.answer(
            response[:4000]
        )

        remaining = response[4000:]

        while remaining:

            chunk = remaining[:4000]
            remaining = remaining[4000:]

            await message.answer(chunk)


# ============================================================
# START
# ============================================================

@dp.message(Command("start"))
async def cmd_start(
    message: types.Message
):

    keys_count = len(GEMINI_KEYS)

    await message.answer(
        "👑 *TRADING BOT V2*\n\n"

        "🟢 النظام يعمل.\n"
        f"🔑 مفاتيح Gemini المتاحة: `{keys_count}`\n\n"

        "📰 *الأخبار*\n"
        "أرسل Forward من أي قناة Telegram "
        "إلى البوت وسيقوم بتحليل الخبر.\n\n"

        "📊 *الأوامر الحالية*\n"
        "`/gold` — الذهب\n"
        "`/btc` — Bitcoin\n"
        "`/eurusd` — EUR/USD\n"
        "`/silver` — الفضة\n"
        "`/oil` — النفط\n"
        "`/eth` — Ethereum\n\n"

        "⚠️ محرك الأسعار الحية والصفقات "
        "والـBacktest الحقيقي سيتم إضافته "
        "في المرحلة التالية."
    )


# ============================================================
# STATUS
# ============================================================

@dp.message(Command("status"))
async def cmd_status(
    message: types.Message
):

    available = await key_manager.available_count()

    await message.answer(
        "🤖 *BOT STATUS*\n\n"
        f"🟢 Telegram: OK\n"
        f"🧠 Gemini model: `{GEMINI_MODEL}`\n"
        f"🔑 Total keys: `{len(GEMINI_KEYS)}`\n"
        f"🔑 Available keys: `{available}`\n"
        f"🕐 Server UTC: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}`"
    )


# ============================================================
# MARKET COMMANDS - TEMPORARY
# ============================================================

async def process_market_command(
    message: types.Message,
    asset_name: str
):

    await message.answer(
        f"📊 *{asset_name}*\n\n"
        "⚠️ محرك البيانات الحية لم يتم تركيبه بعد.\n\n"
        "لن أعطيك سعراً وهمياً أو إشارة BUY/SELL "
        "من دون بيانات سوق حقيقية.\n\n"
        "سيتم تركيب Market Data Engine في المرحلة التالية."
    )


@dp.message(Command("gold"))
async def c_gold(
    message: types.Message
):
    await process_market_command(
        message,
        "XAU/USD — GOLD"
    )


@dp.message(Command("btc"))
async def c_btc(
    message: types.Message
):
    await process_market_command(
        message,
        "BTC/USD — BITCOIN"
    )


@dp.message(Command("eurusd"))
async def c_eur(
    message: types.Message
):
    await process_market_command(
        message,
        "EUR/USD"
    )


@dp.message(Command("silver"))
async def c_silver(
    message: types.Message
):
    await process_market_command(
        message,
        "XAG/USD — SILVER"
    )


@dp.message(Command("oil"))
async def c_oil(
    message: types.Message
):
    await process_market_command(
        message,
        "WTI — OIL"
    )


@dp.message(Command("eth"))
async def c_eth(
    message: types.Message
):
    await process_market_command(
        message,
        "ETH/USD"
    )


# ============================================================
# FORWARDED MESSAGES
# ============================================================

@dp.message()
async def all_messages(
    message: types.Message
):

    # الأوامر تم التعامل معها سابقاً
    if message.text and message.text.startswith("/"):
        return

    # أهم جزء:
    # إذا كانت الرسالة Forward
    if message.forward_origin is not None:

        await analyze_forwarded_news(
            message
        )

        return

    # الرسائل العادية
    text = message.text or message.caption

    if not text:
        return

    # حالياً لا نعتبر كل رسالة عادية خبراً تلقائياً.
    await message.answer(
        "📩 استلمت رسالتك.\n\n"
        "إذا كانت أخباراً من قناة Telegram، "
        "استخدم *Forward* للرسالة إلى البوت "
        "حتى أسجل مصدر الخبر ووقت الـForward."
    )


# ============================================================
# WEB SERVER
# ============================================================

async def health_handler(
    request
):

    return web.Response(
        text="Trading Bot V2 is online."
    )


app = web.Application()

app.add_routes(
    [
        web.get(
            "/",
            health_handler
        ),
        web.get(
            "/health",
            health_handler
        )
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
        "Web server started on port %d.",
        PORT
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "Starting Trading Bot V2..."
    )

    logger.info(
        "Gemini keys configured: %d",
        len(GEMINI_KEYS)
    )

    await start_web_server()

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    finally:

        await bot.session.close()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):

        logger.info(
            "Trading Bot stopped."
        )
