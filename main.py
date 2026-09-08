import os
import json
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

from google import genai


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

# Seven Gemini keys can be placed:
# GEMINI_API_KEYS=key1,key2,key3,key4,key5,key6,key7
gemini_raw = os.getenv("GEMINI_API_KEYS", "").strip()

if not gemini_raw:
    single_key = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_raw = single_key

GEMINI_KEYS = [
    key.strip()
    for key in gemini_raw.split(",")
    if key.strip()
]

# Current model first, older fallbacks afterward.
GEMINI_MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]

PORT = int(os.getenv("PORT", "10000"))

DATA_FILE = Path("trading_state.json")
NEWS_FILE = Path("news_memory.txt")
STRATEGY_FILE = Path("strategies_memory.txt")

# How often the manager wakes up.
MANAGER_LOOP_SECONDS = 30

# Maximum Telegram text chunk.
TELEGRAM_CHUNK_SIZE = 3900


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
    raise RuntimeError("BOT_TOKEN is missing.")

if not TWELVE_DATA_API_KEY:
    logger.warning(
        "TWELVE_DATA_API_KEY is missing. Market commands will not work."
    )

if not GEMINI_KEYS:
    logger.warning(
        "No Gemini API key configured. AI analysis will be disabled."
    )


# ============================================================
# TELEGRAM
# ============================================================

# IMPORTANT:
# parse_mode=None prevents the Markdown/HTML entity problem
# that caused:
# "can't parse entities: Can't find end of the entity..."
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=None
    )
)

dp = Dispatcher()


# ============================================================
# HTTP SESSION
# ============================================================

http_session: Optional[aiohttp.ClientSession] = None


async def get_http_session() -> aiohttp.ClientSession:
    global http_session

    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=20)

        http_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": "TradingBot/2.0"
            }
        )

    return http_session


# ============================================================
# TELEGRAM SAFE SEND
# ============================================================

def split_text(text: str, limit: int = TELEGRAM_CHUNK_SIZE) -> List[str]:
    if not text:
        return [""]

    chunks = []

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)

        if cut < int(limit * 0.5):
            cut = text.rfind(" ", 0, limit)

        if cut < int(limit * 0.5):
            cut = limit

        chunks.append(text[:cut])
        text = text[cut:].lstrip()

    if text:
        chunks.append(text)

    return chunks


async def safe_send(
    chat_id: int,
    text: str,
    reply_to: Optional[int] = None
):
    """
    Sends plain text only.
    No Markdown.
    No HTML.
    This prevents Telegram entity parsing errors.
    """

    chunks = split_text(str(text))

    for i, chunk in enumerate(chunks):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=None,
                reply_to_message_id=(
                    reply_to if i == 0 else None
                )
            )
        except Exception as exc:
            logger.exception("Telegram send error: %s", exc)

            # Last fallback: remove unusual formatting characters.
            fallback = (
                chunk
                .replace("\x00", "")
                .replace("\u200b", "")
            )

            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=fallback,
                    parse_mode=None
                )
            except Exception:
                logger.exception("Telegram fallback send failed.")


# ============================================================
# GEMINI KEY MANAGER
# ============================================================

class GeminiKeyManager:

    def __init__(self, keys: List[str]):
        self.keys = keys
        self.index = 0
        self.cooldowns: Dict[str, datetime] = {}
        self.lock = asyncio.Lock()

    async def get_key(self) -> Optional[str]:

        if not self.keys:
            return None

        now = datetime.now(timezone.utc)

        async with self.lock:

            for _ in range(len(self.keys)):

                key = self.keys[self.index]

                self.index = (
                    self.index + 1
                ) % len(self.keys)

                cooldown_until = self.cooldowns.get(key)

                if (
                    cooldown_until is None
                    or cooldown_until <= now
                ):
                    return key

        return None

    async def cooldown(
        self,
        key: str,
        seconds: int = 60
    ):
        self.cooldowns[key] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=seconds)
        )


gemini_manager = GeminiKeyManager(GEMINI_KEYS)


def is_quota_error(error_text: str) -> bool:

    text = error_text.lower()

    keywords = [
        "quota",
        "rate limit",
        "resource exhausted",
        "too many requests",
        "429",
        "limit",
    ]

    return any(x in text for x in keywords)


async def safe_ai_generate(
    prompt: str,
    fallback: str = ""
) -> str:

    if not GEMINI_KEYS:
        return fallback

    # Try multiple keys/models.
    max_attempts = max(
        len(GEMINI_KEYS) * len(GEMINI_MODELS),
        1
    )

    for _ in range(max_attempts):

        key = await gemini_manager.get_key()

        if not key:
            await asyncio.sleep(2)
            continue

        client = genai.Client(api_key=key)

        for model in GEMINI_MODELS:

            try:

                def generate():
                    response = client.models.generate_content(
                        model=model,
                        contents=prompt
                    )

                    return (
                        response.text.strip()
                        if response and response.text
                        else ""
                    )

                result = await asyncio.to_thread(generate)

                if result:
                    return result

            except Exception as exc:

                error_text = str(exc)

                logger.warning(
                    "Gemini error model=%s: %s",
                    model,
                    error_text
                )

                if is_quota_error(error_text):
                    await gemini_manager.cooldown(
                        key,
                        90
                    )
                    break

                # Try next model.
                continue

    return fallback


# ============================================================
# ASSET CONFIGURATION
# ============================================================

ASSETS: Dict[str, Dict[str, Any]] = {

    "gold": {
        "name": "Gold",
        "symbol": "XAU/USD",
        "interval": "5min",
        "higher_interval": "15min",
        "category": "commodity",
        "digits": 2,
    },

    "btc": {
        "name": "Bitcoin",
        "symbol": "BTC/USD",
        "interval": "5min",
        "higher_interval": "15min",
        "category": "crypto",
        "digits": 2,
    },

    "eurusd": {
        "name": "EUR/USD",
        "symbol": "EUR/USD",
        "interval": "5min",
        "higher_interval": "15min",
        "category": "forex",
        "digits": 5,
    },

    "silver": {
        "name": "Silver",
        "symbol": "XAG/USD",
        "interval": "5min",
        "higher_interval": "15min",
        "category": "commodity",
        "digits": 3,
    },

    "oil": {
        "name": "WTI Oil",
        "symbol": "WTI/USD",
        "interval": "5min",
        "higher_interval": "15min",
        "category": "commodity",
        "digits": 2,
    },

    "eth": {
        "name": "Ethereum",
        "symbol": "ETH/USD",
        "interval": "5min",
        "higher_interval": "15min",
        "category": "crypto",
        "digits": 2,
    },
}


# ============================================================
# MARKET DATA
# ============================================================

async def twelve_request(
    endpoint: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:

    if not TWELVE_DATA_API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY is not configured."
        )

    session = await get_http_session()

    params = dict(params)

    params["apikey"] = TWELVE_DATA_API_KEY

    url = (
        "https://api.twelvedata.com/"
        + endpoint
    )

    async with session.get(
        url,
        params=params
    ) as response:

        data = await response.json(
            content_type=None
        )

        if response.status >= 400:
            raise RuntimeError(
                f"Twelve Data HTTP {response.status}: {data}"
            )

        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(
                data.get("message", "Twelve Data error")
            )

        return data


async def get_price(symbol: str) -> float:

    data = await twelve_request(
        "price",
        {
            "symbol": symbol
        }
    )

    price = data.get("price")

    if price is None:
        raise RuntimeError(
            f"No price returned for {symbol}: {data}"
        )

    return float(price)


async def get_candles(
    symbol: str,
    interval: str,
    outputsize: int = 150
) -> List[Dict[str, float]]:

    data = await twelve_request(
        "time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "format": "JSON",
        }
    )

    values = data.get("values")

    if not values:
        raise RuntimeError(
            f"No candle data for {symbol} {interval}"
        )

    candles = []

    # Twelve Data normally returns newest first.
    # We reverse to oldest -> newest.
    for item in reversed(values):

        try:

            candle = {
                "datetime": item.get("datetime"),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }

            if "volume" in item:
                try:
                    candle["volume"] = float(
                        item["volume"]
                    )
                except Exception:
                    candle["volume"] = 0.0

            candles.append(candle)

        except Exception:
            continue

    if len(candles) < 30:
        raise RuntimeError(
            f"Not enough candles for {symbol}"
        )

    return candles


# ============================================================
# INDICATORS
# ============================================================

def closes(candles):
    return [float(x["close"]) for x in candles]


def highs(candles):
    return [float(x["high"]) for x in candles]


def lows(candles):
    return [float(x["low"]) for x in candles]


def sma(values: List[float], period: int) -> Optional[float]:

    if len(values) < period:
        return None

    return sum(values[-period:]) / period


def ema(
    values: List[float],
    period: int
) -> Optional[float]:

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for price in values[period:]:
        result = (
            (price - result) * multiplier
            + result
        )

    return result


def rsi(
    values: List[float],
    period: int = 14
) -> Optional[float]:

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(
        len(values) - period,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def atr(
    candles: List[Dict[str, float]],
    period: int = 14
) -> Optional[float]:

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def macd(
    values: List[float]
):

    if len(values) < 35:
        return None, None

    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None, None

    line = fast - slow

    # Simplified current MACD line.
    return line, None


def bollinger(
    values: List[float],
    period: int = 20,
    deviation: float = 2.0
):

    if len(values) < period:
        return None, None, None

    recent = values[-period:]

    mean = sum(recent) / period

    variance = sum(
        (x - mean) ** 2
        for x in recent
    ) / period

    std = math.sqrt(variance)

    upper = mean + deviation * std
    lower = mean - deviation * std

    return upper, mean, lower


def support_resistance(
    candles,
    lookback=50
):

    recent = candles[-lookback:]

    support = min(
        x["low"]
        for x in recent
    )

    resistance = max(
        x["high"]
        for x in recent
    )

    return support, resistance


# ============================================================
# TECHNICAL ANALYSIS
# ============================================================

def technical_snapshot(
    candles: List[Dict[str, float]]
) -> Dict[str, Any]:

    c = closes(candles)

    current = c[-1]

    ema9 = ema(c, 9)
    ema21 = ema(c, 21)
    ema50 = ema(c, 50)

    rsi14 = rsi(c, 14)

    atr14 = atr(candles, 14)

    macd_line, _ = macd(c)

    bb_upper, bb_middle, bb_lower = (
        bollinger(c)
    )

    support, resistance = (
        support_resistance(candles)
    )

    score = 0

    if ema9 and ema21:

        if ema9 > ema21:
            score += 1
        else:
            score -= 1

    if ema21 and ema50:

        if ema21 > ema50:
            score += 1
        else:
            score -= 1

    if rsi14 is not None:

        if 50 < rsi14 < 70:
            score += 1

        elif 30 < rsi14 < 50:
            score -= 1

        elif rsi14 >= 75:
            score -= 1

        elif rsi14 <= 25:
            score += 1

    if macd_line is not None:

        if macd_line > 0:
            score += 1
        else:
            score -= 1

    if current > resistance:
        score += 1

    if current < support:
        score -= 1

    if score >= 3:
        direction = "BUY"

    elif score <= -3:
        direction = "SELL"

    else:
        direction = "NO TRADE"

    return {
        "price": current,
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "rsi": rsi14,
        "atr": atr14,
        "macd": macd_line,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "support": support,
        "resistance": resistance,
        "score": score,
        "direction": direction,
    }


# ============================================================
# TRADE CALCULATION
# ============================================================

def round_price(
    value: float,
    digits: int
) -> float:

    return round(value, digits)


def calculate_trade(
    asset_key: str,
    snapshot: Dict[str, Any]
) -> Dict[str, Any]:

    asset = ASSETS[asset_key]

    price = snapshot["price"]
    atr_value = snapshot["atr"]

    if not atr_value or atr_value <= 0:
        raise RuntimeError(
            "ATR unavailable."
        )

    direction = snapshot["direction"]

    support = snapshot["support"]
    resistance = snapshot["resistance"]

    digits = asset["digits"]

    # We do not enter if the technical structure
    # is not sufficiently directional.
    if direction == "NO TRADE":

        return {
            "status": "NO TRADE",
            "reason": (
                "Technical structure is mixed. "
                "Waiting for stronger confirmation."
            ),
            "price": price,
        }

    # Volatility-based risk.
    sl_distance = atr_value * 1.35

    # Three initial targets.
    tp_distances = [
        atr_value * 1.0,
        atr_value * 1.8,
        atr_value * 2.7,
        atr_value * 3.6,
        atr_value * 4.5,
    ]

    if direction == "BUY":

        entry_low = price - atr_value * 0.20
        entry_high = price + atr_value * 0.10

        stop_loss = price - sl_distance

        take_profits = [
            price + x
            for x in tp_distances
        ]

        # Do not make TP1 weaker than nearby resistance
        # unless resistance is below the target.
        if resistance > price:
            if resistance < take_profits[0]:
                take_profits[0] = resistance

    else:

        entry_low = price - atr_value * 0.10
        entry_high = price + atr_value * 0.20

        stop_loss = price + sl_distance

        take_profits = [
            price - x
            for x in tp_distances
        ]

        if support < price:
            if support > take_profits[0]:
                take_profits[0] = support

    # Estimated duration.
    # This is an estimate, NOT a guaranteed expiry.
    volatility_ratio = (
        atr_value / price
        if price
        else 0
    )

    if volatility_ratio > 0.005:
        duration_minutes = 30

    elif volatility_ratio > 0.002:
        duration_minutes = 60

    elif volatility_ratio > 0.001:
        duration_minutes = 120

    else:
        duration_minutes = 240

    score = snapshot["score"]

    confidence = min(
        95,
        max(
            55,
            55 + abs(score) * 8
        )
    )

    return {
        "status": "OPEN",
        "asset": asset["name"],
        "symbol": asset["symbol"],
        "direction": direction,
        "entry_low": round_price(
            entry_low,
            digits
        ),
        "entry_high": round_price(
            entry_high,
            digits
        ),
        "entry_reference": round_price(
            price,
            digits
        ),
        "stop_loss": round_price(
            stop_loss,
            digits
        ),
        "take_profits": [
            round_price(x, digits)
            for x in take_profits
        ],
        "atr": atr_value,
        "confidence": confidence,
        "estimated_duration_minutes": duration_minutes,
        "analysis_score": score,
    }


# ============================================================
# STATE
# ============================================================

open_trades: Dict[str, Dict[str, Any]] = {}


def save_state():

    try:

        data = {
            "open_trades": open_trades
        }

        DATA_FILE.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

    except Exception as exc:
        logger.warning(
            "Could not save state: %s",
            exc
        )


def load_state():

    global open_trades

    if not DATA_FILE.exists():
        return

    try:

        data = json.loads(
            DATA_FILE.read_text(
                encoding="utf-8"
            )
        )

        open_trades = data.get(
            "open_trades",
            {}
        )

    except Exception as exc:

        logger.warning(
            "Could not load state: %s",
            exc
        )

        open_trades = {}


# ============================================================
# TRADE ID
# ============================================================

def create_trade_id(
    asset_key: str
) -> str:

    timestamp = int(
        datetime.now(timezone.utc).timestamp()
    )

    return f"{asset_key.upper()}-{timestamp}"


# ============================================================
# TRADE REPORT
# ============================================================

def format_price(
    value,
    digits=5
):

    if value is None:
        return "N/A"

    return f"{float(value):.{digits}f}"


def trade_report(
    trade: Dict[str, Any]
) -> str:

    digits = ASSETS.get(
        trade.get("asset_key", ""),
        {}
    ).get("digits", 5)

    tp_text = ""

    for i, tp in enumerate(
        trade.get("take_profits", []),
        1
    ):
        tp_text += (
            f"TP{i}: "
            f"{format_price(tp, digits)}\n"
        )

    return (
        "TRADE ANALYSIS\n"
        "====================\n"
        f"Trade ID: {trade.get('trade_id', 'N/A')}\n"
        f"Asset: {trade.get('asset', 'N/A')}\n"
        f"Symbol: {trade.get('symbol', 'N/A')}\n"
        f"Direction: {trade.get('direction', 'N/A')}\n"
        f"Status: {trade.get('status', 'N/A')}\n\n"

        f"Entry Zone: "
        f"{format_price(trade.get('entry_low'), digits)}"
        " - "
        f"{format_price(trade.get('entry_high'), digits)}\n"

        f"Reference Price: "
        f"{format_price(trade.get('entry_reference'), digits)}\n\n"

        f"Stop Loss: "
        f"{format_price(trade.get('stop_loss'), digits)}\n\n"

        "Take Profits:\n"
        f"{tp_text}\n"

        f"Confidence: "
        f"{trade.get('confidence', 'N/A')}%\n"

        f"Estimated duration: "
        f"{trade.get('estimated_duration_minutes', 'N/A')} minutes\n"

        f"Next re-analysis: "
        f"{trade.get('next_review', 'N/A')}\n\n"

        "Note: This is an analytical signal, "
        "not a guaranteed-profit instruction."
    )


# ============================================================
# MARKET ANALYSIS
# ============================================================

async def analyze_asset(
    asset_key: str
) -> Dict[str, Any]:

    if asset_key not in ASSETS:
        raise RuntimeError(
            "Unknown asset."
        )

    asset = ASSETS[asset_key]

    # Fetch both current timeframe and higher timeframe.
    candles, higher = await asyncio.gather(
        get_candles(
            asset["symbol"],
            asset["interval"],
            150
        ),
        get_candles(
            asset["symbol"],
            asset["higher_interval"],
            100
        )
    )

    snapshot = technical_snapshot(
        candles
    )

    higher_snapshot = technical_snapshot(
        higher
    )

    # Higher timeframe confirmation.
    if (
        snapshot["direction"] == "BUY"
        and higher_snapshot["direction"] == "SELL"
    ):
        snapshot["direction"] = "NO TRADE"

    elif (
        snapshot["direction"] == "SELL"
        and higher_snapshot["direction"] == "BUY"
    ):
        snapshot["direction"] = "NO TRADE"

    trade = calculate_trade(
        asset_key,
        snapshot
    )

    trade["asset_key"] = asset_key

    trade["higher_direction"] = (
        higher_snapshot["direction"]
    )

    trade["current_price"] = (
        snapshot["price"]
    )

    trade["snapshot"] = snapshot

    return trade


async def enhance_analysis_with_ai(
    trade: Dict[str, Any]
) -> str:

    snapshot = trade.get(
        "snapshot",
        {}
    )

    prompt = f"""
You are a disciplined market-analysis assistant.

Analyze this REAL market-data snapshot.

Asset:
{trade.get('asset')}

Symbol:
{trade.get('symbol')}

Current price:
{snapshot.get('price')}

Technical direction:
{snapshot.get('direction')}

Higher timeframe direction:
{trade.get('higher_direction')}

EMA 9:
{snapshot.get('ema9')}

EMA 21:
{snapshot.get('ema21')}

EMA 50:
{snapshot.get('ema50')}

RSI:
{snapshot.get('rsi')}

ATR:
{snapshot.get('atr')}

MACD:
{snapshot.get('macd')}

Support:
{snapshot.get('support')}

Resistance:
{snapshot.get('resistance')}

Technical score:
{snapshot.get('score')}

Entry zone:
{trade.get('entry_low')} - {trade.get('entry_high')}

Stop loss:
{trade.get('stop_loss')}

Take profits:
{trade.get('take_profits')}

Give a concise professional assessment.

Rules:
1. Never claim certainty.
2. Never invent market prices.
3. Do not change numerical levels supplied by the system.
4. Explain why the setup is valid or invalid.
5. Mention the main invalidation condition.
6. If the market is mixed, say NO TRADE.
7. Do not use Markdown.
8. Return plain text only.
"""

    return await safe_ai_generate(
        prompt,
        fallback=(
            "AI analysis unavailable. "
            "The technical engine result remains the primary signal."
        )
    )


# ============================================================
# CREATE TRADE
# ============================================================

async def create_trade(
    chat_id: int,
    asset_key: str
):

    await safe_send(
        chat_id,
        "جاري جلب بيانات السوق الحية وتحليل "
        f"{ASSETS[asset_key]['name']}..."
    )

    try:

        result = await analyze_asset(
            asset_key
        )

    except Exception as exc:

        logger.exception(
            "Market analysis failed: %s",
            exc
        )

        await safe_send(
            chat_id,
            "تعذر جلب بيانات السوق.\n\n"
            f"السبب: {exc}\n\n"
            "تأكد من TWELVE_DATA_API_KEY "
            "وأن الرمز متاح في حساب Twelve Data."
        )

        return

    if result["status"] == "NO TRADE":

        await safe_send(
            chat_id,
            "NO TRADE\n"
            "====================\n"
            f"Asset: {result.get('asset')}\n"
            f"Current price: {result.get('price')}\n\n"
            f"Reason: {result.get('reason')}\n\n"
            "لن يتم إنشاء صفقة عندما تكون الإشارات "
            "الفنية متضاربة."
        )

        return

    trade_id = create_trade_id(
        asset_key
    )

    now = datetime.now(timezone.utc)

    duration = int(
        result[
            "estimated_duration_minutes"
        ]
    )

    # Re-analysis after 10% of estimated duration.
    review_minutes = max(
        1,
        round(duration * 0.10)
    )

    next_review = (
        now
        + timedelta(minutes=review_minutes)
    )

    result["trade_id"] = trade_id
    result["chat_id"] = chat_id
    result["created_at"] = (
        now.isoformat()
    )
    result["next_review"] = (
        next_review.isoformat()
    )
    result["review_minutes"] = review_minutes
    result["status"] = "OPEN"

    # Keep only what we need.
    open_trades[trade_id] = result

    save_state()

    ai_text = await enhance_analysis_with_ai(
        result
    )

    report = trade_report(
        result
    )

    report += (
        "\n\nAI assessment:\n"
        + ai_text
    )

    await safe_send(
        chat_id,
        report
    )


# ============================================================
# TRADE PRICE MONITOR
# ============================================================

async def check_trade_price(
    trade: Dict[str, Any]
) -> Dict[str, Any]:

    price = await get_price(
        trade["symbol"]
    )

    direction = trade["direction"]

    stop = float(
        trade["stop_loss"]
    )

    tps = [
        float(x)
        for x in trade["take_profits"]
    ]

    hit = None

    if direction == "BUY":

        if price <= stop:
            hit = "SL"

        else:

            for i, tp in enumerate(
                tps,
                1
            ):
                if price >= tp:
                    hit = f"TP{i}"
                    break

    elif direction == "SELL":

        if price >= stop:
            hit = "SL"

        else:

            for i, tp in enumerate(
                tps,
                1
            ):
                if price <= tp:
                    hit = f"TP{i}"
                    break

    return {
        "price": price,
        "hit": hit,
    }


# ============================================================
# TRADE RE-ANALYSIS
# ============================================================

async def reanalyze_trade(
    trade_id: str,
    reason: str = "scheduled"
):

    trade = open_trades.get(
        trade_id
    )

    if not trade:
        return

    if trade.get("status") != "OPEN":
        return

    chat_id = trade.get("chat_id")

    try:

        market = await check_trade_price(
            trade
        )

        current_price = market["price"]
        hit = market["hit"]

        trade["last_price"] = (
            current_price
        )

        if hit:

            trade["status"] = hit

            save_state()

            await safe_send(
                chat_id,
                "TRADE UPDATE\n"
                "====================\n"
                f"Trade ID: {trade_id}\n"
                f"Asset: {trade['asset']}\n"
                f"Direction: {trade['direction']}\n"
                f"Current price: {current_price}\n"
                f"Result: {hit}\n\n"
                "The analytical trade has been closed."
            )

            return

        # Fetch fresh technical data.
        fresh = await analyze_asset(
            trade["asset_key"]
        )

        new_direction = fresh[
            "direction"
        ]

        old_direction = trade[
            "direction"
        ]

        if new_direction == "NO TRADE":

            new_status = "HOLD"

        elif new_direction != old_direction:

            new_status = "ADJUST"

        else:

            new_status = "CONTINUE"

        trade["last_review"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        trade["management_status"] = (
            new_status
        )

        duration = int(
            trade[
                "estimated_duration_minutes"
            ]
        )

        review_minutes = max(
            1,
            round(duration * 0.10)
        )

        next_review = (
            datetime.now(timezone.utc)
            + timedelta(
                minutes=review_minutes
            )
        )

        trade["next_review"] = (
            next_review.isoformat()
        )

        save_state()

        ai_text = await enhance_analysis_with_ai(
            fresh
        )

        await safe_send(
            chat_id,
            "TRADE RE-ANALYSIS\n"
            "====================\n"
            f"Trade ID: {trade_id}\n"
            f"Asset: {trade['asset']}\n"
            f"Current price: {current_price}\n"
            f"Previous direction: {old_direction}\n"
            f"New technical direction: {new_direction}\n\n"
            f"Management decision: {new_status}\n\n"
            f"Reason for review: {reason}\n\n"
            "AI assessment:\n"
            f"{ai_text}\n\n"
            f"Next review: {next_review.isoformat()}"
        )

    except Exception as exc:

        logger.exception(
            "Trade re-analysis error: %s",
            exc
        )

        # Do NOT close a trade merely because
        # the data provider temporarily failed.
        trade["last_error"] = str(
            exc
        )

        save_state()

        await safe_send(
            chat_id,
            "TRADE MONITOR WARNING\n"
            "====================\n"
            f"Trade ID: {trade_id}\n"
            "Temporary market-data error.\n\n"
            f"Reason: {exc}\n\n"
            "The bot did NOT automatically close "
            "the trade because missing data is not "
            "evidence that SL or TP was reached."
        )


# ============================================================
# TRADE MANAGER
# ============================================================

manager_running = True


async def trade_manager_loop():

    logger.info(
        "Trade manager started."
    )

    while manager_running:

        try:

            now = datetime.now(
                timezone.utc
            )

            trades = list(
                open_trades.items()
            )

            for trade_id, trade in trades:

                if trade.get("status") != "OPEN":
                    continue

                next_review_raw = trade.get(
                    "next_review"
                )

                if not next_review_raw:
                    continue

                try:
                    next_review = (
                        datetime.fromisoformat(
                            next_review_raw
                        )
                    )
                except Exception:
                    continue

                if now >= next_review:

                    await reanalyze_trade(
                        trade_id,
                        "10% scheduled review"
                    )

                    await asyncio.sleep(1)

        except asyncio.CancelledError:
            break

        except Exception as exc:

            logger.exception(
                "Trade manager loop error: %s",
                exc
            )

        await asyncio.sleep(
            MANAGER_LOOP_SECONDS
        )

    logger.info(
        "Trade manager stopped."
    )


# ============================================================
# COMMANDS
# ============================================================

START_TEXT = (
    "Trading Bot\n"
    "====================\n\n"

    "Live market analysis:\n"
    "/gold - Gold XAU/USD\n"
    "/btc - Bitcoin BTC/USD\n"
    "/eurusd - EUR/USD\n"
    "/silver - Silver XAG/USD\n"
    "/oil - WTI Oil\n"
    "/eth - Ethereum ETH/USD\n\n"

    "Trade management:\n"
    "/open_trades - Open analytical trades\n"
    "/reanalyze ID - Immediate re-analysis\n"
    "/close ID - Close analytical trade\n\n"

    "System:\n"
    "/status - System status\n"
    "/auto_backtest - Backtest status\n"
    "/weekly_table - Strategy status\n\n"

    "News:\n"
    "Forward an economic/news message to the bot.\n"
    "The bot will analyze the news and re-check "
    "open analytical trades.\n\n"

    "Important:\n"
    "The bot uses market data and technical analysis. "
    "No result is guaranteed."
)


@dp.message(Command("start"))
async def cmd_start(
    message: types.Message
):

    await safe_send(
        message.chat.id,
        START_TEXT
    )


@dp.message(Command("status"))
async def cmd_status(
    message: types.Message
):

    gemini_status = (
        "configured"
        if GEMINI_KEYS
        else "not configured"
    )

    market_status = (
        "configured"
        if TWELVE_DATA_API_KEY
        else "not configured"
    )

    open_count = sum(
        1
        for x in open_trades.values()
        if x.get("status") == "OPEN"
    )

    await safe_send(
        message.chat.id,
        "SYSTEM STATUS\n"
        "====================\n"
        f"Telegram bot: online\n"
        f"Market data: {market_status}\n"
        f"Gemini AI: {gemini_status}\n"
        f"Gemini keys: {len(GEMINI_KEYS)}\n"
        f"Open analytical trades: {open_count}\n"
        f"Manager loop: {MANAGER_LOOP_SECONDS}s\n"
        "Telegram parse mode: disabled\n"
    )


async def command_asset(
    message: types.Message,
    asset_key: str
):

    await create_trade(
        message.chat.id,
        asset_key
    )


@dp.message(Command("gold"))
async def cmd_gold(
    message: types.Message
):
    await command_asset(
        message,
        "gold"
    )


@dp.message(Command("btc"))
async def cmd_btc(
    message: types.Message
):
    await command_asset(
        message,
        "btc"
    )


@dp.message(Command("eurusd"))
async def cmd_eurusd(
    message: types.Message
):
    await command_asset(
        message,
        "eurusd"
    )


@dp.message(Command("silver"))
async def cmd_silver(
    message: types.Message
):
    await command_asset(
        message,
        "silver"
    )


@dp.message(Command("oil"))
async def cmd_oil(
    message: types.Message
):
    await command_asset(
        message,
        "oil"
    )


@dp.message(Command("eth"))
async def cmd_eth(
    message: types.Message
):
    await command_asset(
        message,
        "eth"
    )


# ============================================================
# OPEN TRADES
# ============================================================

@dp.message(Command("open_trades"))
async def cmd_open_trades(
    message: types.Message
):

    trades = [
        x for x in open_trades.values()
        if x.get("status") == "OPEN"
    ]

    if not trades:

        await safe_send(
            message.chat.id,
            "No open analytical trades."
        )

        return

    text = (
        "OPEN ANALYTICAL TRADES\n"
        "====================\n"
    )

    for trade in trades:

        text += (
            f"ID: {trade.get('trade_id')}\n"
            f"Asset: {trade.get('asset')}\n"
            f"Direction: {trade.get('direction')}\n"
            f"Status: {trade.get('management_status', 'OPEN')}\n"
            f"Entry: {trade.get('entry_reference')}\n"
            f"SL: {trade.get('stop_loss')}\n"
            f"Next review: {trade.get('next_review')}\n"
            "--------------------\n"
        )

    await safe_send(
        message.chat.id,
        text
    )


# ============================================================
# MANUAL REANALYSIS
# ============================================================

@dp.message(Command("reanalyze"))
async def cmd_reanalyze(
    message: types.Message
):

    parts = (
        message.text or ""
    ).split(maxsplit=1)

    if len(parts) < 2:

        await safe_send(
            message.chat.id,
            "Usage:\n/reanalyze TRADE_ID"
        )

        return

    trade_id = parts[1].strip()

    trade = open_trades.get(
        trade_id
    )

    if not trade:

        await safe_send(
            message.chat.id,
            "Trade ID not found."
        )

        return

    await safe_send(
        message.chat.id,
        "Starting immediate re-analysis..."
    )

    await reanalyze_trade(
        trade_id,
        "manual request"
    )


# ============================================================
# CLOSE TRADE
# ============================================================

@dp.message(Command("close"))
async def cmd_close(
    message: types.Message
):

    parts = (
        message.text or ""
    ).split(maxsplit=1)

    if len(parts) < 2:

        await safe_send(
            message.chat.id,
            "Usage:\n/close TRADE_ID"
        )

        return

    trade_id = parts[1].strip()

    trade = open_trades.get(
        trade_id
    )

    if not trade:

        await safe_send(
            message.chat.id,
            "Trade ID not found."
        )

        return

    trade["status"] = "CLOSED_MANUALLY"

    trade["closed_at"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    save_state()

    await safe_send(
        message.chat.id,
        "Analytical trade closed.\n"
        f"Trade ID: {trade_id}"
    )


# ============================================================
# BACKTEST
# ============================================================

@dp.message(Command("auto_backtest"))
async def cmd_auto_backtest(
    message: types.Message
):

    await safe_send(
        message.chat.id,
        "Real backtesting engine is not enabled in this "
        "version yet.\n\n"
        "The bot intentionally does NOT generate random "
        "win rates or fake 50-trade results.\n\n"
        "This command will only report real historical "
        "results once the historical backtest module is "
        "connected to market data."
    )


@dp.message(Command("weekly_table"))
async def cmd_weekly_table(
    message: types.Message
):

    await safe_send(
        message.chat.id,
        "Weekly strategy ranking is not generated yet.\n\n"
        "The bot intentionally refuses to create fake "
        "performance statistics.\n\n"
        "The ranking module should use real historical "
        "candles and real strategy rules before selecting "
        "a winning strategy."
    )


# ============================================================
# NEWS / FORWARDED MESSAGE
# ============================================================

def get_message_text(
    message: types.Message
) -> str:

    text = message.text

    if not text:
        text = message.caption

    return (text or "").strip()


def is_forwarded(
    message: types.Message
) -> bool:

    origin = getattr(
        message,
        "forward_origin",
        None
    )

    return origin is not None


async def analyze_news(
    text: str
) -> str:

    prompt = f"""
Analyze the following financial news.

News:
{text}

Return plain text.

Identify:
1. Main event.
2. Assets likely affected.
3. Directional bias if any.
4. Expected impact strength: LOW, MEDIUM, HIGH.
5. Expected duration: minutes, hours, days.
6. Which assets should be monitored.
7. What would invalidate the interpretation.

Do not invent facts.
Do not claim certainty.
Do not give guaranteed profit.
"""

    return await safe_ai_generate(
        prompt,
        fallback=(
            "AI news analysis unavailable."
        )
    )


async def news_trigger_reanalysis(
    news_analysis: str
):

    trades = [
        (trade_id, trade)
        for trade_id, trade
        in open_trades.items()
        if trade.get("status") == "OPEN"
    ]

    if not trades:
        return

    # The AI assessment is used as a trigger/context,
    # not as proof that a trade must be closed.
    for trade_id, trade in trades:

        chat_id = trade.get("chat_id")

        try:

            prompt = f"""
A financial news event was received.

News analysis:
{news_analysis}

Open analytical trade:
Asset: {trade.get('asset')}
Direction: {trade.get('direction')}
Entry: {trade.get('entry_reference')}
Stop: {trade.get('stop_loss')}
TPs: {trade.get('take_profits')}

Determine whether this news is likely to:
CONTINUE
HOLD
ADJUST
CLOSE
INVALIDATE

Return one of those words first, then explain briefly.

Do not invent facts.
"""

            decision = await safe_ai_generate(
                prompt,
                fallback="HOLD\nAI unavailable."
            )

            await safe_send(
                chat_id,
                "NEWS IMPACT ON OPEN TRADE\n"
                "====================\n"
                f"Trade: {trade_id}\n"
                f"Asset: {trade.get('asset')}\n\n"
                f"AI decision:\n{decision}\n\n"
                "The bot will perform a fresh market-data "
                "re-analysis rather than blindly closing "
                "the trade."
            )

            await reanalyze_trade(
                trade_id,
                "news event"
            )

        except Exception as exc:

            logger.exception(
                "News trade reanalysis error: %s",
                exc
            )


@dp.message(F.text)
async def handle_text(
    message: types.Message
):

    text = get_message_text(
        message
    )

    if not text:
        return

    # Commands are handled above.
    if text.startswith("/"):
        return

    forwarded = is_forwarded(
        message
    )

    if forwarded:

        await safe_send(
            message.chat.id,
            "Forwarded message received.\n"
            "Analyzing the financial/news impact..."
        )

        analysis = await analyze_news(
            text
        )

        try:

            with NEWS_FILE.open(
                "a",
                encoding="utf-8"
            ) as f:

                f.write(
                    "\n"
                    + "=" * 60
                    + "\n"
                    + datetime.now(
                        timezone.utc
                    ).isoformat()
                    + "\n"
                    + "NEWS:\n"
                    + text
                    + "\n\n"
                    + "ANALYSIS:\n"
                    + analysis
                    + "\n"
                )

        except Exception:
            logger.exception(
                "Could not save news."
            )

        await safe_send(
            message.chat.id,
            "NEWS ANALYSIS\n"
            "====================\n"
            + analysis
        )

        await news_trigger_reanalysis(
            analysis
        )

        return

    # Normal text that contains a URL.
    if (
        "http://" in text
        or "https://" in text
    ):

        await safe_send(
            message.chat.id,
            "Strategy/link received.\n"
            "The URL has been stored for strategy analysis."
        )

        try:

            with STRATEGY_FILE.open(
                "a",
                encoding="utf-8"
            ) as f:

                f.write(
                    "\n"
                    + "=" * 60
                    + "\n"
                    + datetime.now(
                        timezone.utc
                    ).isoformat()
                    + "\n"
                    + text
                    + "\n"
                )

        except Exception:
            logger.exception(
                "Could not save strategy."
            )

        # Do not falsely claim we watched a video.
        analysis = await safe_ai_generate(
            f"""
Analyze this strategy URL as far as the supplied
information allows.

URL:
{text}

If you cannot access the actual strategy/video content,
say so clearly.

Do not invent what is inside the video.

Return plain text.
""",
            fallback=(
                "The link was stored. "
                "The bot cannot verify the video contents "
                "from the URL alone."
            )
        )

        await safe_send(
            message.chat.id,
            "STRATEGY LINK ANALYSIS\n"
            "====================\n"
            + analysis
        )

        return

    # Normal non-forwarded text.
    analysis = await safe_ai_generate(
        f"""
Analyze this user message in the context of financial
markets.

Message:
{text}

If it is a market/news question, answer usefully.
Do not invent live prices.
Do not claim guaranteed profit.
Return plain text.
""",
        fallback=(
            "Message received. "
            "Use /gold, /btc, /eurusd, /silver, /oil "
            "or /eth for live market analysis."
        )
    )

    await safe_send(
        message.chat.id,
        analysis
    )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(
    request
):

    return web.Response(
        text="Trading Bot is online."
    )


async def status_json(
    request
):

    return web.json_response(
        {
            "status": "online",
            "open_trades": sum(
                1
                for x in open_trades.values()
                if x.get("status") == "OPEN"
            ),
            "gemini_keys": len(
                GEMINI_KEYS
            ),
            "market_data": bool(
                TWELVE_DATA_API_KEY
            ),
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
        }
    )


app = web.Application()

app.router.add_get(
    "/",
    health
)

app.router.add_get(
    "/health",
    health
)

app.router.add_get(
    "/status",
    status_json
)


async def start_web_server():

    runner = web.AppRunner(
        app
    )

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

    return runner


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def main():

    global manager_running
    global http_session

    load_state()

    logger.info(
        "Starting Trading Bot..."
    )

    logger.info(
        "Gemini keys configured: %s",
        len(GEMINI_KEYS)
    )

    logger.info(
        "Market API configured: %s",
        bool(TWELVE_DATA_API_KEY)
    )

    web_runner = await start_web_server()

    manager_task = asyncio.create_task(
        trade_manager_loop()
    )

    try:

        # Ensure webhook mode does not interfere
        # with long polling.
        try:

            await bot.delete_webhook(
                drop_pending_updates=False
            )

            logger.info(
                "Webhook cleared. Starting polling."
            )

        except Exception as exc:

            logger.warning(
                "Could not clear webhook: %s",
                exc
            )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    except asyncio.CancelledError:

        logger.info(
            "Polling cancelled."
        )

    except Exception as exc:

        logger.exception(
            "Fatal polling error: %s",
            exc
        )

    finally:

        manager_running = False

        manager_task.cancel()

        try:
            await manager_task
        except asyncio.CancelledError:
            pass

        save_state()

        if http_session and not http_session.closed:
            await http_session.close()

        try:
            await web_runner.cleanup()
        except Exception:
            pass

        try:
            await bot.session.close()
        except Exception:
            pass

        logger.info(
            "Trading Bot stopped."
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Stopped by keyboard interrupt."
            )
