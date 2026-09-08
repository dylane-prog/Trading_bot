import os
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from google import genai


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

GEMINI_KEYS_RAW = os.getenv("GEMINI_API_KEYS", "")
GEMINI_KEYS = [
    key.strip()
    for key in GEMINI_KEYS_RAW.split(",")
    if key.strip()
]

# Google currently documents Gemini 3.8 Flash for generate_content.
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.8-flash"
).strip()

PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = Path(__file__).resolve().parent

NEWS_FILE = BASE_DIR / "news_memory.txt"
STRATEGY_FILE = BASE_DIR / "strategies_memory.txt"

MAX_TELEGRAM_MESSAGE_LENGTH = 3900


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | TradingBot | %(message)s"
)

logger = logging.getLogger("TradingBot")


# ============================================================
# BASIC VALIDATION
# ============================================================

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is missing.")

if not GEMINI_KEYS:
    logger.warning("No GEMINI_API_KEYS were found.")


# ============================================================
# TELEGRAM BOT
# IMPORTANT:
# parse_mode=None means Telegram will NOT parse Markdown/HTML.
# This prevents:
#   can't parse entities
#   Can't find end of the entity
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=None
    )
)

dp = Dispatcher()


# ============================================================
# GEMINI KEY MANAGER
# ============================================================

class KeyManager:

    def __init__(self, keys):
        self.keys = keys
        self.current_index = 0
        self.cooldowns = {}
        self.lock = asyncio.Lock()

    async def get_available_key(self):
        if not self.keys:
            return None, None

        async with self.lock:

            now = asyncio.get_running_loop().time()

            for _ in range(len(self.keys)):

                index = self.current_index % len(self.keys)

                cooldown_until = self.cooldowns.get(index, 0)

                if cooldown_until <= now:
                    key = self.keys[index]
                    return index, key

                self.current_index = (
                    self.current_index + 1
                ) % len(self.keys)

            return None, None

    async def rotate(self):
        if not self.keys:
            return

        async with self.lock:
            self.current_index = (
                self.current_index + 1
            ) % len(self.keys)

    async def cooldown(self, index, seconds=30):

        if index is None:
            return

        async with self.lock:

            now = asyncio.get_running_loop().time()

            self.cooldowns[index] = now + seconds

            self.current_index = (
                index + 1
            ) % len(self.keys)

            logger.warning(
                "Gemini key #%s placed in cooldown for %s seconds.",
                index + 1,
                seconds
            )


key_manager = KeyManager(GEMINI_KEYS)


# ============================================================
# GEMINI
# ============================================================

async def gemini_generate(prompt: str) -> str:

    if not GEMINI_KEYS:
        return (
            "⚠️ Gemini غير متاح حاليًا.\n"
            "لم يتم العثور على GEMINI_API_KEYS في Render."
        )

    if not prompt:
        return "⚠️ لم يتم إرسال محتوى للتحليل."

    last_error = None

    # Try every available key.
    for attempt in range(max(3, len(GEMINI_KEYS) * 2)):

        key_index, api_key = await key_manager.get_available_key()

        if api_key is None:

            # All keys temporarily unavailable.
            await asyncio.sleep(2)
            continue

        try:

            client = genai.Client(
                api_key=api_key
            )

            response = await asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt
            )

            text = getattr(response, "text", None)

            if text:

                text = str(text).strip()

                if text:
                    return text

            last_error = "Gemini returned an empty response."

            await key_manager.rotate()

        except Exception as exc:

            last_error = exc

            error_text = str(exc).lower()

            logger.error(
                "Gemini attempt failed using key #%s: %s",
                (key_index + 1) if key_index is not None else "?",
                exc
            )

            # Quota / rate-limit errors.
            if any(
                phrase in error_text
                for phrase in [
                    "429",
                    "quota",
                    "rate limit",
                    "resource exhausted",
                    "too many requests"
                ]
            ):
                await key_manager.cooldown(
                    key_index,
                    30
                )

            # Authentication errors.
            elif any(
                phrase in error_text
                for phrase in [
                    "401",
                    "403",
                    "api key",
                    "permission",
                    "unauthorized"
                ]
            ):
                await key_manager.cooldown(
                    key_index,
                    300
                )

            # Model not found.
            elif (
                "404" in error_text
                or "not found" in error_text
                or "not available" in error_text
            ):
                logger.error(
                    "Gemini model '%s' is unavailable. "
                    "Check GEMINI_MODEL.",
                    GEMINI_MODEL
                )

                # Do NOT mark the key as bad.
                await key_manager.rotate()

            else:
                await key_manager.rotate()

            await asyncio.sleep(1)

    logger.error(
        "All Gemini attempts failed. Last error: %s",
        last_error
    )

    return (
        "⚠️ تعذر الحصول على رد من Gemini حاليًا.\n"
        "تمت محاولة مفاتيح Gemini المتاحة."
    )


# ============================================================
# TELEGRAM SAFE SENDING
# ============================================================

def split_text(text: str, limit=MAX_TELEGRAM_MESSAGE_LENGTH):

    if text is None:
        return [""]

    text = str(text)

    if len(text) <= limit:
        return [text]

    chunks = []

    remaining = text

    while len(remaining) > limit:

        # Prefer splitting at newline.
        cut = remaining.rfind(
            "\n",
            0,
            limit
        )

        if cut < 500:
            cut = remaining.rfind(
                " ",
                0,
                limit
            )

        if cut < 1:
            cut = limit

        chunks.append(
            remaining[:cut]
        )

        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks


async def safe_answer(
    message: types.Message,
    text: str
):

    """
    Telegram-safe sender.

    No Markdown.
    No MarkdownV2.
    No HTML.

    This completely avoids Telegram entity parsing errors.
    """

    if text is None:
        text = ""

    text = str(text)

    chunks = split_text(text)

    sent_messages = []

    for chunk in chunks:

        try:

            sent = await message.answer(
                chunk,
                parse_mode=None
            )

            sent_messages.append(sent)

        except TelegramBadRequest as exc:

            logger.error(
                "Telegram BadRequest while sending message: %s",
                exc
            )

            # Last-resort cleanup.
            # Remove NUL/control characters.
            cleaned = "".join(
                char
                for char in chunk
                if char == "\n"
                or char == "\t"
                or ord(char) >= 32
            )

            try:

                sent = await message.answer(
                    cleaned,
                    parse_mode=None
                )

                sent_messages.append(sent)

            except Exception as second_error:

                logger.exception(
                    "Second Telegram send attempt failed: %s",
                    second_error
                )

        except TelegramNetworkError as exc:

            logger.error(
                "Telegram network error: %s",
                exc
            )

            await asyncio.sleep(2)

            try:

                sent = await message.answer(
                    chunk,
                    parse_mode=None
                )

                sent_messages.append(sent)

            except Exception as retry_error:

                logger.exception(
                    "Telegram retry failed: %s",
                    retry_error
                )

        except Exception as exc:

            logger.exception(
                "Unexpected Telegram send error: %s",
                exc
            )

    return sent_messages


# ============================================================
# FILE MEMORY
# ============================================================

def save_memory(
    file_path: Path,
    title: str,
    content: str
):

    try:

        with open(
            file_path,
            "a",
            encoding="utf-8"
        ) as file:

            file.write(
                "\n"
                + "=" * 60
                + "\n"
            )

            file.write(
                f"[{datetime.now(timezone.utc).isoformat()}]\n"
            )

            file.write(
                f"{title}\n\n"
            )

            file.write(
                str(content)
            )

            file.write("\n")

    except Exception as exc:

        logger.exception(
            "Could not save memory file: %s",
            exc
        )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(request):

    return web.Response(
        text="Trading Bot is running."
    )


async def status(request):

    gemini_status = (
        "configured"
        if GEMINI_KEYS
        else "missing"
    )

    return web.json_response(
        {
            "status": "online",
            "telegram": "polling",
            "gemini": gemini_status,
            "gemini_model": GEMINI_MODEL,
            "gemini_keys": len(GEMINI_KEYS),
            "time_utc": datetime.now(
                timezone.utc
            ).isoformat()
        }
    )


app = web.Application()

app.add_routes(
    [
        web.get("/", health),
        web.get("/health", health),
        web.get("/status", status),
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


# ============================================================
# START COMMAND
# ============================================================

@dp.message(Command("start"))
async def cmd_start(
    message: types.Message
):

    text = (
        "👑 مرحبًا بك في Trading Bot\n\n"

        "📈 أوامر التحليل:\n"
        "/gold - الذهب XAU/USD\n"
        "/btc - البيتكوين BTC/USD\n"
        "/eurusd - EUR/USD\n"
        "/silver - الفضة XAG/USD\n"
        "/oil - النفط WTI\n"
        "/eth - Ethereum ETH/USD\n\n"

        "🧪 أوامر الاختبار:\n"
        "/auto_backtest\n"
        "/weekly_table\n\n"

        "📰 الأخبار والاستراتيجيات:\n"
        "يمكنك إرسال خبر أو إعادة توجيه رسالة من Telegram.\n"
        "ويمكنك إرسال رابط استراتيجية ليتم تحليله.\n\n"

        "ℹ️ ملاحظة:\n"
        "التحليل لا يعتبر ضمانًا للربح، ويجب التحقق من بيانات السوق الحية قبل التداول."
    )

    await safe_answer(
        message,
        text
    )


# ============================================================
# STATUS COMMAND
# ============================================================

@dp.message(Command("status"))
async def cmd_status(
    message: types.Message
):

    text = (
        "🟢 حالة Trading Bot\n\n"
        f"Telegram: Online\n"
        f"Gemini keys: {len(GEMINI_KEYS)}\n"
        f"Gemini model: {GEMINI_MODEL}\n"
        f"Server port: {PORT}\n"
        f"Time UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )

    await safe_answer(
        message,
        text
    )


# ============================================================
# MARKET COMMAND
# ============================================================

MARKETS = {
    "gold": "الذهب XAU/USD",
    "btc": "Bitcoin BTC/USD",
    "eurusd": "EUR/USD",
    "silver": "Silver XAG/USD",
    "oil": "WTI Crude Oil",
    "eth": "Ethereum ETH/USD"
}


async def process_market_command(
    message: types.Message,
    asset_name: str
):

    command_text = message.text or ""

    parts = command_text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        await safe_answer(
            message,
            (
                f"⚠️ لم يتم إدخال سعر.\n\n"
                f"الأمر الحالي: {parts[0]}\n"
                f"مثال: {parts[0]} 3500"
            )
        )

        return

    raw_price = parts[1].strip()

    try:

        price = float(
            raw_price.replace(",", "")
        )

    except ValueError:

        await safe_answer(
            message,
            (
                "⚠️ السعر غير صالح.\n"
                "أدخل رقمًا فقط، مثال:\n"
                f"{parts[0]} 3500"
            )
        )

        return

    await safe_answer(
        message,
        (
            f"🔄 جاري تحليل {asset_name}...\n"
            f"السعر المدخل: {price}"
        )
    )

    prompt = f"""
أنت محلل تداول محترف.

حلل الأصل التالي:

الأصل:
{asset_name}

السعر الحالي الذي أدخله المستخدم:
{price}

أعطني تحليلًا منظمًا باللغة العربية.

يجب أن يتضمن:

1. الاتجاه:
BUY أو SELL أو WAIT

2. سبب القرار.

3. منطقة الدخول.

4. Stop Loss.

5. من 3 إلى 10 مستويات Take Profit منطقية.

6. مستوى إلغاء السيناريو.

7. مدة متوقعة للصفقة.

8. درجة قوة السيناريو من 0 إلى 100.

9. أهم المخاطر.

مهم:
لا تخترع بيانات سوق حية غير موجودة في الطلب.
السعر المذكور هو السعر الذي أدخله المستخدم وليس مصدرًا مباشرًا للسوق.
إذا كانت البيانات غير كافية، قل WAIT بدل اختراع معلومات.

لا تستخدم تنسيق HTML.
يمكنك استخدام نص عادي.
"""

    analysis = await gemini_generate(
        prompt
    )

    report = (
        f"📊 تحليل {asset_name}\n\n"
        f"السعر المدخل: {price}\n"
        f"وقت التحليل UTC: "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"{analysis}"
    )

    await safe_answer(
        message,
        report
    )


# ============================================================
# MARKET COMMANDS
# ============================================================

@dp.message(Command("gold"))
async def cmd_gold(message: types.Message):
    await process_market_command(
        message,
        MARKETS["gold"]
    )


@dp.message(Command("btc"))
async def cmd_btc(message: types.Message):
    await process_market_command(
        message,
        MARKETS["btc"]
    )


@dp.message(Command("eurusd"))
async def cmd_eurusd(message: types.Message):
    await process_market_command(
        message,
        MARKETS["eurusd"]
    )


@dp.message(Command("silver"))
async def cmd_silver(message: types.Message):
    await process_market_command(
        message,
        MARKETS["silver"]
    )


@dp.message(Command("oil"))
async def cmd_oil(message: types.Message):
    await process_market_command(
        message,
        MARKETS["oil"]
    )


@dp.message(Command("eth"))
async def cmd_eth(message: types.Message):
    await process_market_command(
        message,
        MARKETS["eth"]
    )


# ============================================================
# BACKTEST
# ============================================================

@dp.message(Command("auto_backtest"))
async def cmd_auto_backtest(
    message: types.Message
):

    await safe_answer(
        message,
        (
            "⚠️ نظام الـ Backtest الحقيقي لم يتم ربطه "
            "بمصدر بيانات تاريخية في هذه النسخة.\n\n"
            "لن أعرض أرقامًا عشوائية وأقول إنها نتائج 50 صفقة حقيقية.\n\n"
            "عندما يتم ربط مصدر OHLCV تاريخي، يمكن تنفيذ "
            "Backtest حقيقي وتسجيل كل صفقة ونتيجتها."
        )
    )


@dp.message(Command("weekly_table"))
async def cmd_weekly_table(
    message: types.Message
):

    await safe_answer(
        message,
        (
            "📊 الجدول الأسبوعي غير مفعل بعد.\n\n"
            "لن يتم اختيار استراتيجية فائزة اعتمادًا على "
            "بيانات عشوائية أو نتائج وهمية.\n\n"
            "يجب أولًا توفير بيانات تاريخية حقيقية ثم "
            "اختبار الاستراتيجيات عليها."
        )
    )


# ============================================================
# FORWARDED NEWS / STRATEGY
# ============================================================

def get_forward_info(
    message: types.Message
):

    origin = getattr(
        message,
        "forward_origin",
        None
    )

    if origin is None:
        return None

    info = {
        "type": type(origin).__name__,
        "date": getattr(
            origin,
            "date",
            None
        ),
        "chat_title": None,
        "message_id": None
    }

    chat = getattr(
        origin,
        "chat",
        None
    )

    if chat:
        info["chat_title"] = getattr(
            chat,
            "title",
            None
        )

    info["message_id"] = getattr(
        origin,
        "message_id",
        None
    )

    return info


def extract_message_text(
    message: types.Message
):

    text = message.text

    if not text:
        text = message.caption

    if not text:
        return ""

    return str(text).strip()


def looks_like_strategy(
    text: str
):

    lowered = text.lower()

    keywords = [
        "strategy",
        "trading strategy",
        "استراتيجية",
        "استراتيجيه",
        "tradingview",
        "forex strategy",
        "scalping",
        "swing trading"
    ]

    if "http://" in lowered:
        return True

    if "https://" in lowered:
        return True

    return any(
        keyword in lowered
        for keyword in keywords
    )


@dp.message(
    F.text
)
async def handle_text(
    message: types.Message
):

    text = extract_message_text(
        message
    )

    if not text:
        return

    # Ignore commands.
    if text.startswith("/"):
        return

    forward_info = get_forward_info(
        message
    )

    is_forwarded = (
        forward_info is not None
    )

    strategy = looks_like_strategy(
        text
    )

    if strategy:
        target = "استراتيجية تداول"
    elif is_forwarded:
        target = "خبر أو رسالة معاد توجيهها"
    else:
        target = "خبر اقتصادي أو رسالة تداول"

    await safe_answer(
        message,
        (
            f"🧠 جاري تحليل {target}...\n"
            "يرجى الانتظار."
        )
    )

    forward_context = ""

    if forward_info:

        forward_context = (
            "\n\nمعلومات إعادة التوجيه:\n"
            f"النوع: {forward_info.get('type')}\n"
            f"القناة: {forward_info.get('chat_title')}\n"
            f"Message ID: {forward_info.get('message_id')}\n"
        )

    prompt = f"""
أنت محلل أسواق مالية.

حلل المحتوى التالي:

{target}

المحتوى:
{text}

{forward_context}

أجب بالعربية.

استخرج:

1. ملخص المحتوى.
2. الأصل أو الأصول المتأثرة.
3. هل التأثير إيجابي أم سلبي أم محايد؟
4. قوة التأثير من 0 إلى 100.
5. المدة المتوقعة للتأثير.
6. هل يمكن أن يؤثر على الذهب؟
7. هل يمكن أن يؤثر على الدولار؟
8. هل يمكن أن يؤثر على EUR/USD؟
9. هل يمكن أن يؤثر على النفط؟
10. هل يمكن أن يؤثر على Bitcoin؟
11. ما الإجراء التداولي المنطقي؟
12. إذا كانت المعلومات غير كافية، قل بوضوح: غير كافٍ لاتخاذ قرار.

ممنوع اختراع أسعار أو أخبار غير موجودة في النص.

استخدم نصًا عاديًا.
لا تعتمد على Markdown أو HTML.
"""

    analysis = await gemini_generate(
        prompt
    )

    if strategy:

        save_memory(
            STRATEGY_FILE,
            "STRATEGY",
            (
                f"INPUT:\n{text}\n\n"
                f"ANALYSIS:\n{analysis}"
            )
        )

    else:

        save_memory(
            NEWS_FILE,
            "NEWS",
            (
                f"INPUT:\n{text}\n\n"
                f"ANALYSIS:\n{analysis}"
            )
        )

    result = (
        f"✅ تحليل {target}\n\n"
        f"{analysis}"
    )

    await safe_answer(
        message,
        result
    )


# ============================================================
# PHOTO / CAPTION
# ============================================================

@dp.message(
    F.photo
)
async def handle_photo(
    message: types.Message
):

    caption = (
        message.caption
        or ""
    ).strip()

    if not caption:

        await safe_answer(
            message,
            (
                "🖼️ تم استلام صورة.\n\n"
                "في هذه النسخة لم يتم تفعيل تحليل "
                "صور Telegram بواسطة Gemini."
            )
        )

        return

    await safe_answer(
        message,
        "🧠 جاري تحليل النص المرفق بالصورة..."
    )

    prompt = f"""
حلل هذا النص المرتبط بصورة تداولية:

{caption}

أعطني:
- الأصل المحتمل
- الاتجاه المحتمل
- أهم المستويات المذكورة
- المخاطر
- هل توجد معلومات كافية لاتخاذ قرار؟

لا تخترع بيانات غير موجودة.
"""

    analysis = await gemini_generate(
        prompt
    )

    await safe_answer(
        message,
        "📊 تحليل الصورة والنص:\n\n" + analysis
    )


# ============================================================
# UNKNOWN COMMAND HANDLER
# ============================================================

@dp.message(
    F.text.startswith("/")
)
async def unknown_command(
    message: types.Message
):

    text = (
        "⚠️ الأمر غير معروف.\n\n"
        "استخدم /start لرؤية الأوامر المتاحة."
    )

    await safe_answer(
        message,
        text
    )


# ============================================================
# STARTUP
# ============================================================

async def startup():

    logger.info(
        "Starting Trading Bot..."
    )

    logger.info(
        "Gemini model: %s",
        GEMINI_MODEL
    )

    logger.info(
        "Gemini keys configured: %s",
        len(GEMINI_KEYS)
    )

    # Remove an old webhook if one exists.
    # This is necessary because polling and webhook mode
    # must not be active simultaneously.
    try:

        await bot.delete_webhook(
            drop_pending_updates=False
        )

        logger.info(
            "Telegram webhook cleared."
        )

    except Exception as exc:

        logger.warning(
            "Could not clear Telegram webhook: %s",
            exc
        )

    await start_web_server()

    try:

        bot_info = await bot.get_me()

        logger.info(
            "Bot connected: @%s id=%s",
            bot_info.username,
            bot_info.id
        )

    except Exception as exc:

        logger.error(
            "Could not connect to Telegram: %s",
            exc
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    await startup()

    logger.info(
        "Starting Telegram polling..."
    )

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    except asyncio.CancelledError:

        logger.warning(
            "Polling cancelled."
        )

    except Exception as exc:

        logger.exception(
            "Polling stopped because of an error: %s",
            exc
        )

        raise

    finally:

        await bot.session.close()

        logger.info(
            "Trading Bot stopped."
        )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped manually."
        )

    except Exception as exc:

        logger.exception(
            "Fatal error: %s",
            exc
    )
