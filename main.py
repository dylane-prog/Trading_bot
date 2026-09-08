import os
import asyncio
import logging
import json
import math
import uuid
import re
import subprocess
import tempfile
import shutil
import sys
from pathlib import Path
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

try:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
except Exception:
    TelegramBadRequest = Exception
    TelegramForbiddenError = Exception

try:
    from google import genai
except Exception:
    genai = None

# ============================================================
# CONFIGURATION
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

raw_keys = os.getenv("GEMINI_API_KEYS", "").strip()
if raw_keys:
    GEMINI_KEYS = [x.strip() for x in raw_keys.split(",") if x.strip()]
else:
    one_key = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_KEYS = [one_key] if one_key else []

GEMINI_MODELS = [
    x.strip() for x in os.getenv(
        "GEMINI_MODELS",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash"
    ).split(",") if x.strip()
]

PORT = int(os.getenv("PORT", "10000"))
TD_INTERVAL = os.getenv("TD_INTERVAL", "5min")
TD_OUTPUTSIZE = int(os.getenv("TD_OUTPUTSIZE", "300"))
REANALYSIS_PERCENT = 0.10
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "6"))
MIN_CONFIDENCE_TO_OPEN = float(os.getenv("MIN_CONFIDENCE_TO_OPEN", "62"))
MARKET_CACHE_SECONDS = int(os.getenv("MARKET_CACHE_SECONDS", "30"))
PRICE_CACHE_SECONDS = int(os.getenv("PRICE_CACHE_SECONDS", "8"))
MONITOR_SECONDS = int(os.getenv("MONITOR_SECONDS", "30"))
# Market-close protection: trades are never allowed to outlive the next
# configured market close. Times are UTC. Crypto is 24/7 and has no close.
MARKET_CLOSE_PROTECTION = os.getenv("MARKET_CLOSE_PROTECTION", "true").lower() in ("1", "true", "yes", "on")
FOREX_CLOSE_UTC = os.getenv("FOREX_CLOSE_UTC", "22:00")
METALS_CLOSE_UTC = os.getenv("METALS_CLOSE_UTC", "22:00")
OIL_CLOSE_UTC = os.getenv("OIL_CLOSE_UTC", "22:00")
MARKET_CLOSE_BUFFER_MINUTES = int(os.getenv("MARKET_CLOSE_BUFFER_MINUTES", "5"))
STATE_FILE = os.getenv("STATE_FILE", "trading_state.json")
STRATEGY_DB_FILE = os.getenv("STRATEGY_DB_FILE", "strategies_db.json")
TRAINING_TESTS = max(70, int(os.getenv("TRAINING_TESTS", "70")))
STRATEGY_MIN_TRADES = int(os.getenv("STRATEGY_MIN_TRADES", "10"))
STRATEGY_TRAINING_LOCK = asyncio.Lock()
training_status = {
    "running": False,
    "strategy_id": "",
    "strategy_name": "",
    "tests": 0,
    "trades": 0,
    "message": "idle",
}
LOCAL_TZ = ZoneInfo("Africa/Algiers")

ASSETS = {
    "gold": {"name": "Gold XAU/USD", "symbol": os.getenv("TD_SYMBOL_GOLD", "XAU/USD")},
    "btc": {"name": "Bitcoin BTC/USD", "symbol": os.getenv("TD_SYMBOL_BTC", "BTC/USD")},
    "eurusd": {"name": "EUR/USD", "symbol": os.getenv("TD_SYMBOL_EURUSD", "EUR/USD")},
    "silver": {"name": "Silver XAG/USD", "symbol": os.getenv("TD_SYMBOL_SILVER", "XAG/USD")},
    "oil": {"name": "Crude Oil WTI", "symbol": os.getenv("TD_SYMBOL_OIL", "WTI")},
    "eth": {"name": "Ethereum ETH/USD", "symbol": os.getenv("TD_SYMBOL_ETH", "ETH/USD")},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | TradingBot | %(message)s")
logger = logging.getLogger("TradingBot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()

# ============================================================
# TELEGRAM HELPERS
# ============================================================
TELEGRAM_LIMIT = 3900

def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks

async def safe_send(message: types.Message, text: str):
    for chunk in split_text(text):
        try:
            await message.answer(chunk, parse_mode=None)
        except TelegramBadRequest:
            await message.answer(chunk.replace("\x00", ""), parse_mode=None)

async def safe_reply(chat_id: int, text: str):
    for chunk in split_text(text):
        try:
            await bot.send_message(chat_id, chunk, parse_mode=None)
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)
            break

# ============================================================
# GEMINI KEY MANAGER
# ============================================================
class KeyManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.index = 0
        self.cooldowns: Dict[str, datetime] = {}
        self.lock = asyncio.Lock()

    async def next_key(self) -> Optional[str]:
        if not self.keys:
            return None
        async with self.lock:
            now = datetime.now(timezone.utc)
            for _ in range(len(self.keys)):
                key = self.keys[self.index]
                self.index = (self.index + 1) % len(self.keys)
                until = self.cooldowns.get(key)
                if not until or now >= until:
                    return key
            return self.keys[self.index % len(self.keys)]

    def cooldown(self, key: str, seconds: int = 60):
        self.cooldowns[key] = datetime.now(timezone.utc) + timedelta(seconds=seconds)

key_manager = KeyManager(GEMINI_KEYS)

async def safe_ai_generate(prompt: str) -> str:
    if genai is None or not GEMINI_KEYS:
        return ""
    last_error = ""
    for _ in range(max(1, len(GEMINI_KEYS))):
        key = await key_manager.next_key()
        if not key:
            return ""
        try:
            client = genai.Client(api_key=key)
            for model in GEMINI_MODELS:
                try:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=prompt,
                    )
                    text = getattr(response, "text", None)
                    if text:
                        return text.strip()
                except Exception as exc:
                    last_error = str(exc)
                    low = last_error.lower()
                    if any(x in low for x in ("429", "quota", "rate limit", "resource exhausted", "too many requests")):
                        key_manager.cooldown(key, 60)
                        break
                    if any(x in low for x in ("not found", "404", "unsupported", "does not exist", "invalid model")):
                        continue
        except Exception as exc:
            last_error = str(exc)
            key_manager.cooldown(key, 30)
    logger.warning("Gemini unavailable: %s", last_error[:500])
    return ""

# ============================================================
# MARKET DATA
# ============================================================
market_cache: Dict[str, Tuple[datetime, List[dict]]] = {}
price_cache: Dict[str, Tuple[datetime, float]] = {}
market_lock = asyncio.Lock()
price_lock = asyncio.Lock()

async def td_get(path: str, params: dict) -> dict:
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured in Render Environment Variables.")
    params = dict(params)
    params["apikey"] = TWELVE_DATA_API_KEY
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://api.twelvedata.com/" + path, params=params) as response:
            data = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Twelve Data HTTP {response.status}: {data}")
            if data.get("status") == "error" or "code" in data:
                raise RuntimeError(f"Twelve Data error: {data.get('message') or data}")
            return data

async def get_price(symbol: str, use_cache: bool = True) -> float:
    now = datetime.now(timezone.utc)
    cached = price_cache.get(symbol)
    if use_cache and cached and (now - cached[0]).total_seconds() < PRICE_CACHE_SECONDS:
        return cached[1]
    async with price_lock:
        cached = price_cache.get(symbol)
        now = datetime.now(timezone.utc)
        if use_cache and cached and (now - cached[0]).total_seconds() < PRICE_CACHE_SECONDS:
            return cached[1]
        data = await td_get("price", {"symbol": symbol, "format": "JSON"})
        value = float(data["price"])
        price_cache[symbol] = (datetime.now(timezone.utc), value)
        return value

async def get_time_series(symbol: str, outputsize: int = TD_OUTPUTSIZE) -> List[dict]:
    cache_key = f"{symbol}:{TD_INTERVAL}:{outputsize}"
    now = datetime.now(timezone.utc)
    cached = market_cache.get(cache_key)
    if cached and (now - cached[0]).total_seconds() < MARKET_CACHE_SECONDS:
        return cached[1]
    async with market_lock:
        now = datetime.now(timezone.utc)
        cached = market_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < MARKET_CACHE_SECONDS:
            return cached[1]
        data = await td_get("time_series", {
            "symbol": symbol,
            "interval": TD_INTERVAL,
            "outputsize": min(max(outputsize, 60), 5000),
            "format": "JSON",
            "timezone": "UTC",
        })
        raw = data.get("values") or []
        values = []
        for row in reversed(raw):
            try:
                values.append({
                    "datetime": row.get("datetime", ""),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0) or 0),
                })
            except (ValueError, TypeError, KeyError):
                continue
        if len(values) < 60:
            raise RuntimeError(f"Not enough candles for {symbol}. Received {len(values)}.")
        market_cache[cache_key] = (datetime.now(timezone.utc), values)
        return values

# ============================================================
# INDICATORS
# ============================================================
def ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * alpha + result[-1] * (1 - alpha))
    return result

def ema(values: List[float], period: int) -> Optional[float]:
    s = ema_series(values, period)
    return s[-1] if s else None

def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return sum(trs[-period:]) / period

def macd(values: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < 35:
        return None, None, None
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)
    line = [a - b for a, b in zip(fast, slow)]
    sig = ema_series(line, 9)
    return line[-1], sig[-1], line[-1] - sig[-1]

def bollinger(values: List[float], period: int = 20, mult: float = 2):
    if len(values) < period:
        return None, None, None
    w = values[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((x - mid) ** 2 for x in w) / period)
    return mid, mid + mult * std, mid - mult * std

def support_resistance(highs: List[float], lows: List[float], lookback: int = 60):
    return min(lows[-lookback:]), max(highs[-lookback:])

def format_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 10:
        return f"{p:.4f}"
    return f"{p:.6f}"

# ============================================================
# ANALYSIS
# ============================================================
@dataclass
class Analysis:
    asset_key: str
    asset_name: str
    symbol: str
    price: float
    signal: str
    confidence: float
    entry_low: float
    entry_high: float
    sl: float
    tps: List[float]
    atr_value: float
    rsi_value: float
    ema20: float
    ema50: float
    macd_value: float
    macd_signal: float
    macd_hist: float
    support: float
    resistance: float
    duration_minutes: int
    score: int
    reasons: List[str]
    generated_at: str


def analyze_market(asset_key: str, candles: List[dict], live_price: Optional[float] = None) -> Analysis:
    cfg = ASSETS[asset_key]
    closes = [x["close"] for x in candles]
    highs = [x["high"] for x in candles]
    lows = [x["low"] for x in candles]
    price = float(live_price if live_price is not None else closes[-1])
    e20, e50 = ema(closes, 20), ema(closes, 50)
    r = rsi(closes, 14)
    a = atr(highs, lows, closes, 14)
    m_line, m_signal, m_hist = macd(closes)
    _, _, _ = bollinger(closes)
    support, resistance = support_resistance(highs, lows, 60)
    if None in (e20, e50, r, a, m_line, m_signal, m_hist) or a <= 0:
        raise RuntimeError("Insufficient indicator data")

    score = 0
    reasons = []
    if price > e20:
        score += 1; reasons.append("السعر فوق EMA20")
    else:
        score -= 1; reasons.append("السعر تحت EMA20")
    if e20 > e50:
        score += 2; reasons.append("EMA20 فوق EMA50")
    else:
        score -= 2; reasons.append("EMA20 تحت EMA50")
    if m_hist > 0:
        score += 2; reasons.append("MACD histogram إيجابي")
    else:
        score -= 2; reasons.append("MACD histogram سلبي")
    if 55 < r < 75:
        score += 2; reasons.append(f"RSI صاعد ومتوازن: {r:.1f}")
    elif 25 < r < 45:
        score -= 2; reasons.append(f"RSI هابط ومتوازن: {r:.1f}")
    elif r >= 75:
        score -= 1; reasons.append(f"RSI مرتفع جدًا: {r:.1f}")
    elif r <= 25:
        score += 1; reasons.append(f"RSI منخفض جدًا: {r:.1f}")
    momentum = price - closes[-6]
    if momentum > 0:
        score += 1; reasons.append("الزخم القصير إيجابي")
    elif momentum < 0:
        score -= 1; reasons.append("الزخم القصير سلبي")

    signal = "BUY" if score >= 4 else "SELL" if score <= -4 else "NO TRADE"
    confidence = min(95.0, max(35.0, 50.0 + abs(score) * 7.0))
    half = a * 0.20
    entry_low, entry_high = price - half, price + half
    if signal == "BUY":
        sl = price - a * 1.20
        tps = [price + a * x for x in (0.8, 1.2, 1.6, 2.0, 2.4, 2.8, 3.2, 3.6, 4.0, 4.5)]
    elif signal == "SELL":
        sl = price + a * 1.20
        tps = [price - a * x for x in (0.8, 1.2, 1.6, 2.0, 2.4, 2.8, 3.2, 3.6, 4.0, 4.5)]
    else:
        sl, tps = price, []
    duration = 0 if signal == "NO TRADE" else 45 if confidence >= 80 else 60 if confidence >= 70 else 90
    return Analysis(
        asset_key=asset_key, asset_name=cfg["name"], symbol=cfg["symbol"], price=price,
        signal=signal, confidence=confidence, entry_low=entry_low, entry_high=entry_high,
        sl=sl, tps=tps, atr_value=a, rsi_value=r, ema20=e20, ema50=e50,
        macd_value=m_line, macd_signal=m_signal, macd_hist=m_hist,
        support=support, resistance=resistance, duration_minutes=duration,
        score=score, reasons=reasons, generated_at=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )

async def get_market_snapshot(asset_key: str) -> Tuple[Analysis, List[dict]]:
    cfg = ASSETS[asset_key]
    live_price, candles = await asyncio.gather(
        get_price(cfg["symbol"], use_cache=False),
        get_time_series(cfg["symbol"]),
    )
    return analyze_market(asset_key, candles, live_price=live_price), candles

async def improve_analysis_with_ai(analysis: Analysis) -> str:
    prompt = f"""
أنت محلل أسواق. استخدم فقط البيانات التالية. لا تخترع سعرًا أو خبرًا.
الأصل: {analysis.asset_name}
السعر الحالي: {analysis.price}
الإشارة الحسابية: {analysis.signal}
الثقة الحسابية: {analysis.confidence:.1f}%
ATR: {analysis.atr_value}
RSI: {analysis.rsi_value:.2f}
EMA20: {analysis.ema20}
EMA50: {analysis.ema50}
MACD: {analysis.macd_value}
MACD Signal: {analysis.macd_signal}
MACD Histogram: {analysis.macd_hist}
Support: {analysis.support}
Resistance: {analysis.resistance}
أجب في 5 نقاط قصيرة: الأفضلية، أهم عامل، الخطر، ما يجب مراقبته، وهل السوق مناسب لصفقة قصيرة.
لا تضمن الربح ولا تغير الإشارة الحسابية.
"""
    return await safe_ai_generate(prompt)

def analysis_message(analysis: Analysis, ai_note: str = "", trade_id: Optional[str] = None) -> str:
    lines = [
        "📊 تحليل السوق",
        f"الأصل: {analysis.asset_name}",
        f"السعر الحي المستخدم: {format_price(analysis.price)}",
        f"وقت التحليل: {analysis.generated_at}",
        "",
        f"الإشارة: {analysis.signal}",
        f"الثقة الحسابية: {analysis.confidence:.1f}%",
        f"Score: {analysis.score}",
        "",
        "منطقة الدخول:",
        f"{format_price(analysis.entry_low)} إلى {format_price(analysis.entry_high)}",
    ]
    if analysis.signal != "NO TRADE":
        lines += ["", f"وقف الخسارة: {format_price(analysis.sl)}", "الأهداف:"]
        lines += [f"TP{i}: {format_price(tp)}" for i, tp in enumerate(analysis.tps, 1)]
        lines += ["", f"المدة التقديرية: {analysis.duration_minutes} دقيقة",
                  f"إعادة التحليل: كل {max(1, round(analysis.duration_minutes * REANALYSIS_PERCENT))} دقائق"]
    else:
        lines += ["", "لا توجد صفقة ذات أفضلية كافية الآن."]
    lines += ["", "المؤشرات:", f"RSI: {analysis.rsi_value:.2f}",
              f"EMA20: {format_price(analysis.ema20)}", f"EMA50: {format_price(analysis.ema50)}",
              f"MACD Histogram: {analysis.macd_hist:.6f}", f"ATR: {format_price(analysis.atr_value)}",
              f"الدعم: {format_price(analysis.support)}", f"المقاومة: {format_price(analysis.resistance)}",
              "", "الأسباب:"]
    lines += [f"- {x}" for x in analysis.reasons]
    if trade_id:
        lines += ["", f"Trade ID: {trade_id}"]
    if ai_note:
        lines += ["", "مراجعة Gemini:", ai_note]
    lines += ["", "تنبيه: هذا تحليل آلي وليس ضمانًا للربح. تحقق من السبريد والسيولة والتنفيذ لدى وسيطك."]
    return "\n".join(lines)

# ============================================================
# TRADE STATE / PERSISTENCE
# ============================================================
@dataclass
class Trade:
    id: str
    chat_id: int
    asset_key: str
    asset_name: str
    symbol: str
    side: str
    entry_low: float
    entry_high: float
    entry_price: float
    sl: float
    tps: List[float]
    opened_at: str
    estimated_duration_minutes: int
    next_reanalysis_at: str
    market_close_at: str = ""
    status: str = "OPEN"
    last_price: float = 0.0
    last_action: str = "OPENED"
    hit_tps: List[int] = field(default_factory=list)
    notified_tps: List[int] = field(default_factory=list)
    last_review_at: str = ""
    management: str = "OPEN"
    score: int = 0
    confidence: float = 0.0

open_trades: Dict[str, Trade] = {}
trade_lock = asyncio.Lock()


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def dt_from_string(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)

def save_state():
    try:
        data = {k: asdict(v) for k, v in open_trades.items()}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        logger.exception("Could not save state")

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for tid, raw in data.items():
            raw.setdefault("hit_tps", [])
            raw.setdefault("notified_tps", [])
            raw.setdefault("last_review_at", "")
            raw.setdefault("management", raw.get("last_action", "OPEN"))
            raw.setdefault("score", 0)
            raw.setdefault("confidence", 0.0)
            raw.setdefault("market_close_at", "")
            open_trades[tid] = Trade(**raw)
        logger.info("Loaded %s trades from state", len(open_trades))
    except Exception:
        logger.exception("Could not load state")


def _parse_hhmm(value: str) -> Tuple[int, int]:
    try:
        h, m = value.strip().split(":", 1)
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return 22, 0

def next_market_close(asset_key: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Return the next close time for the asset in local timezone.

    Crypto is 24/7, so it returns None. For FX/metals the default is the
    weekly Friday close. Oil is also treated as a weekly-close instrument
    here; the exact venue schedule can be changed with OIL_CLOSE_UTC.
    """
    if not MARKET_CLOSE_PROTECTION or asset_key in ("btc", "eth"):
        return None
    now = now or now_local()
    close_text = METALS_CLOSE_UTC if asset_key in ("gold", "silver") else OIL_CLOSE_UTC if asset_key == "oil" else FOREX_CLOSE_UTC
    hour, minute = _parse_hhmm(close_text)
    utc_now = now.astimezone(timezone.utc)
    days_until_friday = (4 - utc_now.weekday()) % 7
    candidate = (utc_now + timedelta(days=days_until_friday)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= utc_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(LOCAL_TZ)

def calculate_trade_expiry(trade_opened_at: datetime, duration_minutes: int, asset_key: str) -> Tuple[datetime, str]:
    normal_expiry = trade_opened_at + timedelta(minutes=max(1, duration_minutes))
    close = next_market_close(asset_key, trade_opened_at)
    if close is not None:
        protected_close = close - timedelta(minutes=max(0, MARKET_CLOSE_BUFFER_MINUTES))
        if protected_close <= trade_opened_at:
            return protected_close, "MARKET CLOSE"
        if protected_close < normal_expiry:
            return protected_close, "MARKET CLOSE"
    return normal_expiry, "TIME LIMIT"

def create_trade(chat_id: int, analysis: Analysis) -> Trade:
    now = now_local()
    mins = max(1, round(analysis.duration_minutes * REANALYSIS_PERCENT))
    expiry, _reason = calculate_trade_expiry(now, analysis.duration_minutes, analysis.asset_key)
    market_close = next_market_close(analysis.asset_key, now)
    return Trade(
        id=uuid.uuid4().hex[:8].upper(), chat_id=chat_id,
        asset_key=analysis.asset_key, asset_name=analysis.asset_name, symbol=analysis.symbol,
        side=analysis.signal, entry_low=analysis.entry_low, entry_high=analysis.entry_high,
        entry_price=analysis.price, sl=analysis.sl, tps=analysis.tps,
        opened_at=now.isoformat(), estimated_duration_minutes=analysis.duration_minutes,
        next_reanalysis_at=(now + timedelta(minutes=mins)).isoformat(),
        market_close_at=market_close.isoformat() if market_close else "",
        last_price=analysis.price, score=analysis.score, confidence=analysis.confidence,
    )

def trade_status_text(trade: Trade) -> str:
    next_time = dt_from_string(trade.next_reanalysis_at).strftime("%Y-%m-%d %H:%M:%S")
    reached = ", ".join(f"TP{x}" for x in trade.hit_tps) if trade.hit_tps else "none"
    return (
        f"{trade.asset_name} | {trade.side} | {trade.status}\n"
        f"ID: {trade.id}\n"
        f"Entry: {format_price(trade.entry_price)}\n"
        f"SL: {format_price(trade.sl)}\n"
        f"Last price: {format_price(trade.last_price)}\n"
        f"Management: {trade.management}\n"
        f"Reached TP: {reached}\n"
        f"Next reanalysis: {next_time}\n"
        f"Market close: {(dt_from_string(trade.market_close_at).strftime('%Y-%m-%d %H:%M:%S') if trade.market_close_at else '24/7 / no close')}\n"
        f"Last action: {trade.last_action}"
    )

def trade_expired(trade: Trade, now: Optional[datetime] = None) -> bool:
    if trade.status != "OPEN":
        return False
    now = now or now_local()
    opened = dt_from_string(trade.opened_at)
    expiry, reason = calculate_trade_expiry(opened, trade.estimated_duration_minutes, trade.asset_key)
    if now >= expiry:
        trade.last_action = f"EXPIRED - {reason}"
        return True
    return False


def evaluate_trade_price(trade: Trade, price: float) -> List[str]:
    events = []
    trade.last_price = price
    if trade.side == "BUY":
        if price <= trade.sl:
            trade.status = "CLOSED"; trade.management = "CLOSE"; trade.last_action = "CLOSE - SL"; return ["SL"]
        for i, tp in enumerate(trade.tps, 1):
            if i not in trade.hit_tps and price >= tp:
                trade.hit_tps.append(i); events.append(f"TP{i}")
    elif trade.side == "SELL":
        if price >= trade.sl:
            trade.status = "CLOSED"; trade.management = "CLOSE"; trade.last_action = "CLOSE - SL"; return ["SL"]
        for i, tp in enumerate(trade.tps, 1):
            if i not in trade.hit_tps and price <= tp:
                trade.hit_tps.append(i); events.append(f"TP{i}")
    if events:
        trade.last_action = events[-1] + " HIT"
        if len(trade.hit_tps) >= len(trade.tps):
            trade.status = "CLOSED"; trade.management = "CLOSE"; trade.last_action = "CLOSE - FINAL TP"
            events.append("FINAL TP")
    return events

# ============================================================
# TRADE REANALYSIS
# ============================================================
async def reanalyze_trade(trade: Trade, reason: str = "scheduled") -> Tuple[Trade, Analysis, List[str]]:
    now = now_local()
    if trade_expired(trade, now):
        trade.status = "EXPIRED"
        trade.management = "CLOSE"
        if not trade.last_action.startswith("EXPIRED -"):
            trade.last_action = "EXPIRED - TIME LIMIT"
        trade.last_review_at = now.isoformat()
        save_state()
        # Analysis is only needed by callers that expect it; fetch one current snapshot.
    analysis, _candles = await get_market_snapshot(trade.asset_key)
    events = evaluate_trade_price(trade, analysis.price)
    trade.last_review_at = now.isoformat()

    if trade.status != "OPEN":
        trade.next_reanalysis_at = now.isoformat()
        save_state()
        return trade, analysis, events

    old_side = trade.side
    if analysis.signal == "NO TRADE":
        trade.management = "HOLD"
        trade.last_action = "HOLD - NO TRADE SIGNAL"
    elif analysis.signal == old_side:
        trade.management = "CONTINUE"
        trade.last_action = "CONTINUE"
    else:
        trade.management = "ADJUST"
        trade.last_action = "ADJUST - DIRECTION CHANGED"
        # Apply the new technical plan instead of merely reporting it.
        trade.side = analysis.signal
        trade.entry_low = analysis.entry_low
        trade.entry_high = analysis.entry_high
        trade.entry_price = analysis.price
        trade.sl = analysis.sl
        trade.tps = analysis.tps
        trade.hit_tps = []
        trade.notified_tps = []
        trade.estimated_duration_minutes = max(1, analysis.duration_minutes)
        close_at = next_market_close(trade.asset_key, now)
        trade.market_close_at = close_at.isoformat() if close_at else ""

    trade.last_price = analysis.price
    trade.score = analysis.score
    trade.confidence = analysis.confidence
    mins = max(1, round(trade.estimated_duration_minutes * REANALYSIS_PERCENT))
    trade.next_reanalysis_at = (now + timedelta(minutes=mins)).isoformat()
    save_state()
    return trade, analysis, events

# ============================================================
# ASSET COMMANDS
# ============================================================
async def perform_asset_analysis(message: types.Message, asset_key: str, create_new_trade: bool = True):
    if not TWELVE_DATA_API_KEY:
        await safe_send(message, "❌ TWELVE_DATA_API_KEY غير موجود في Render Environment.")
        return
    cfg = ASSETS[asset_key]
    await safe_send(message, f"🔄 جاري جلب السعر الحي والبيانات وتحليل {cfg['name']}...")
    try:
        analysis, _ = await get_market_snapshot(asset_key)
    except Exception as exc:
        logger.exception("Market analysis failed")
        await safe_send(message, f"❌ تعذر تحليل {cfg['name']}.\nالسبب: {str(exc)[:700]}")
        return
    ai_note = await improve_analysis_with_ai(analysis)

    if create_new_trade and analysis.signal in ("BUY", "SELL") and analysis.confidence >= MIN_CONFIDENCE_TO_OPEN:
        async with trade_lock:
            existing = [t for t in open_trades.values() if t.asset_key == asset_key and t.status == "OPEN"]
            if existing:
                await safe_send(message, analysis_message(analysis, ai_note, existing[0].id) +
                                 "\n\n⚠️ توجد صفقة مفتوحة بالفعل لهذا الأصل. لم يتم فتح صفقة ثانية.")
                return
            if len([t for t in open_trades.values() if t.status == "OPEN"]) >= MAX_OPEN_TRADES:
                await safe_send(message, "⚠️ تم الوصول إلى الحد الأقصى للصفقات المفتوحة.")
                return
            trade = create_trade(message.chat.id, analysis)
            open_trades[trade.id] = trade
            save_state()
        await safe_send(message, analysis_message(analysis, ai_note, trade.id) +
                         "\n\n🟢 تم تسجيل الصفقة في مدير الصفقات.\nالمتابعة الحية كل 30 ثانية، وإعادة التحليل كل 10% من المدة.")
    else:
        text = analysis_message(analysis, ai_note)
        if analysis.signal in ("BUY", "SELL"):
            text += "\n\n⚠️ الإشارة موجودة لكن الثقة أقل من حد فتح الصفقة الآلية."
        await safe_send(message, text)

# ============================================================
# COMMANDS
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await safe_send(message,
        "👑 مرحبًا بك في Trading Bot\n\n"
        "📈 التحليل الحي:\n/gold\n/btc\n/eurusd\n/silver\n/oil\n/eth\n\n"
        "📌 إدارة الصفقات:\n/trades\n/close ALL\n/close TRADE_ID\n/reanalyze TRADE_ID\n/status\n\n"
        "🧪 الاختبار والتعلم:\n/auto_backtest\n/weekly_table\n/strategies\n/strategy STRATEGY_ID\n/training\n/retrain STRATEGY_ID\n\n"
        "🎥 أرسل رابط فيديو للاستراتيجية؛ سيُستخرج النص المتاح ويُختبر تاريخيًا في منطقة التدريب فقط.\n"
        "📰 أرسل أو أعد توجيه خبر إلى البوت لتحليل تأثيره.\n\n"
        "⏰ حماية إغلاق السوق مفعلة: الصفقة تنتهي تلقائيًا قبل وقت الإغلاق المحدد للسوق. Crypto يعمل 24/7.\n\n"
        "⚠️ لا يوجد ضمان للربح."
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    open_count = len([t for t in open_trades.values() if t.status == "OPEN"])
    await safe_send(message,
        "حالة Trading Bot\n\n"
        "Telegram: ONLINE\n"
        f"Twelve Data: {'CONFIGURED' if TWELVE_DATA_API_KEY else 'MISSING API KEY'}\n"
        f"Gemini keys: {len(GEMINI_KEYS)}\n"
        f"Gemini models: {', '.join(GEMINI_MODELS)}\n"
        f"Open trades: {open_count} / {MAX_OPEN_TRADES}\n"
        f"Monitor: {MONITOR_SECONDS}s\n"
        "Timezone: Africa/Algiers\n"
        f"Market interval: {TD_INTERVAL}\n"
        f"Market candles: {TD_OUTPUTSIZE}\n"
        f"State file: {STATE_FILE}"
    )

@dp.message(Command("gold"))
async def c_gold(message: types.Message): await perform_asset_analysis(message, "gold")
@dp.message(Command("btc"))
async def c_btc(message: types.Message): await perform_asset_analysis(message, "btc")
@dp.message(Command("eurusd"))
async def c_eurusd(message: types.Message): await perform_asset_analysis(message, "eurusd")
@dp.message(Command("silver"))
async def c_silver(message: types.Message): await perform_asset_analysis(message, "silver")
@dp.message(Command("oil"))
async def c_oil(message: types.Message): await perform_asset_analysis(message, "oil")
@dp.message(Command("eth"))
async def c_eth(message: types.Message): await perform_asset_analysis(message, "eth")

@dp.message(Command("trades"))
async def cmd_trades(message: types.Message):
    trades = [t for t in open_trades.values() if t.status == "OPEN"]
    if not trades:
        await safe_send(message, "لا توجد صفقات مفتوحة حاليًا.")
        return
    await safe_send(message, "📋 الصفقات المفتوحة\n\n" + "\n\n".join(trade_status_text(t) for t in trades))

@dp.message(Command("reanalyze"))
async def cmd_reanalyze(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe_send(message, "الاستخدام: /reanalyze TRADE_ID")
        return
    tid = parts[1].strip().upper()
    trade = open_trades.get(tid)
    if not trade or trade.status != "OPEN":
        await safe_send(message, "❌ الصفقة غير موجودة أو ليست مفتوحة.")
        return
    try:
        old_action = trade.last_action
        trade, analysis, events = await reanalyze_trade(trade, reason="manual")
        await safe_send(message,
            f"🔄 إعادة تحليل فورية\nTrade ID: {trade.id}\n"
            f"السعر الحي: {format_price(analysis.price)}\n"
            f"الإشارة: {analysis.signal}\n"
            f"الحالة: {trade.status}\n"
            f"Management: {trade.management}\n"
            f"Action: {trade.last_action}\n"
            f"TP events: {', '.join(events) if events else 'none'}\n"
            f"Old action: {old_action}"
        )
    except Exception as exc:
        await safe_send(message, f"❌ فشلت إعادة التحليل: {str(exc)[:700]}")

@dp.message(Command("close"))
async def cmd_close(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe_send(message, "الاستخدام: /close ALL أو /close TRADE_ID")
        return
    target = parts[1].strip().upper()
    closed = 0
    async with trade_lock:
        if target == "ALL":
            for t in open_trades.values():
                if t.status == "OPEN":
                    t.status = "CLOSED"; t.management = "CLOSE"; t.last_action = "CLOSE - USER"; closed += 1
        else:
            t = open_trades.get(target)
            if t and t.status == "OPEN":
                t.status = "CLOSED"; t.management = "CLOSE"; t.last_action = "CLOSE - USER"; closed = 1
        save_state()
    await safe_send(message, f"تم إغلاق {closed} صفقة مسجلة.")

# ============================================================
# LEARNED STRATEGY COMMANDS
# ============================================================
@dp.message(Command("strategies"))
async def cmd_strategies(message: types.Message):
    rows = top_strategies(10)
    if not rows:
        await safe_send(message, "🧠 لا توجد استراتيجيات مدرّبة بعد.\nأرسل رابط فيديو مع كلمة Strategy أو استراتيجية.")
        return

    lines = ["🏆 أفضل 10 استراتيجيات تم تحليلها وتدريبها", ""]
    for i, (strategy, result) in enumerate(rows, 1):
        lines.append(
            f"{i}. {strategy.name}\n"
            f"   ID: {strategy.id}\n"
            f"   Score: {result.score}/100 | PF: {result.profit_factor}\n"
            f"   Win rate: {result.win_rate}% | Net R: {result.net_r}\n"
            f"   Trades: {result.trades} | Verdict: {result.verdict}\n"
        )
    await safe_send(message, "\n".join(lines))


@dp.message(Command("strategy"))
async def cmd_strategy(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe_send(message, "الاستخدام: /strategy STRATEGY_ID")
        return
    sid = parts[1].strip().upper()
    strategy = strategy_from_db(sid)
    if not strategy:
        await safe_send(message, "❌ الاستراتيجية غير موجودة.")
        return
    await safe_send(message, strategy_display(strategy, result_from_db(sid)))


@dp.message(Command("training"))
async def cmd_training(message: types.Message):
    if not training_status["running"]:
        await safe_send(
            message,
            f"🧪 حالة التدريب: IDLE\n"
            f"آخر رسالة: {training_status['message']}\n"
            f"الاستراتيجيات المحفوظة: {len(strategies_db.get('strategies', {}))}"
        )
        return
    await safe_send(
        message,
        "🧪 التدريب يعمل الآن\n"
        f"Strategy: {training_status['strategy_name']}\n"
        f"ID: {training_status['strategy_id']}\n"
        f"Tests completed: {training_status['tests']}\n"
        f"Trades found: {training_status['trades']}\n"
        f"Status: {training_status['message']}"
    )


@dp.message(Command("retrain"))
async def cmd_retrain(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe_send(message, "الاستخدام: /retrain STRATEGY_ID")
        return
    sid = parts[1].strip().upper()
    strategy = strategy_from_db(sid)
    if not strategy:
        await safe_send(message, "❌ الاستراتيجية غير موجودة.")
        return
    if training_status["running"]:
        await safe_send(message, "⚠️ يوجد تدريب آخر يعمل حاليًا. استخدم /training لمتابعته.")
        return
    await safe_send(message, f"🔄 إعادة تدريب {strategy.name}...")
    try:
        result = await train_strategy(strategy)
        await safe_send(message, strategy_display(strategy, result))
    except Exception as exc:
        logger.exception("Retrain failed")
        await safe_send(message, f"❌ فشل إعادة التدريب: {str(exc)[:700]}")


# ============================================================
# BACKTEST
# ============================================================
def backtest_ema_rsi(candles: List[dict]) -> dict:
    closes = [x["close"] for x in candles]; highs = [x["high"] for x in candles]; lows = [x["low"] for x in candles]
    if len(candles) < 120:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "note": "Not enough historical candles."}
    trades = wins = losses = 0
    i = 60
    while i < len(candles) - 10:
        c, h, l = closes[:i+1], highs[:i+1], lows[:i+1]
        e20, e50, rr, aa = ema(c,20), ema(c,50), rsi(c,14), atr(h,l,c,14)
        if None in (e20,e50,rr,aa) or aa <= 0:
            i += 1; continue
        price = c[-1]
        side = "BUY" if price > e20 > e50 and rr > 55 else "SELL" if price < e20 < e50 and rr < 45 else None
        if not side:
            i += 1; continue
        trades += 1
        sl = price - aa*1.2 if side == "BUY" else price + aa*1.2
        tp = price + aa*1.6 if side == "BUY" else price - aa*1.6
        result = None
        for j in range(i+1, min(i+11, len(candles))):
            bar = candles[j]
            if side == "BUY":
                if bar["low"] <= sl: result = "LOSS"; break
                if bar["high"] >= tp: result = "WIN"; break
            else:
                if bar["high"] >= sl: result = "LOSS"; break
                if bar["low"] <= tp: result = "WIN"; break
        if result == "WIN": wins += 1
        else: losses += 1
        i += 10
    return {"trades": trades, "wins": wins, "losses": losses, "win_rate": round(wins/trades*100,2) if trades else 0,
            "note": "Historical walk-forward simulation using returned candles; not a future-performance guarantee."}

@dp.message(Command("auto_backtest"))
async def cmd_auto_backtest(message: types.Message):
    if not TWELVE_DATA_API_KEY:
        await safe_send(message, "❌ TWELVE_DATA_API_KEY غير موجود، لذلك لا يمكن تنفيذ Backtest حقيقي.")
        return
    await safe_send(message, "🧪 جاري تنفيذ Backtest حقيقي من البيانات التاريخية المتاحة...")
    results = []
    for _, cfg in ASSETS.items():
        try:
            result = backtest_ema_rsi(await get_time_series(cfg["symbol"], 500))
        except Exception as exc:
            result = {"trades":0,"wins":0,"losses":0,"win_rate":0,"note":str(exc)[:180]}
        results.append((cfg["name"], result))
    text = "🧪 نتائج Backtest الحقيقي\n\n"
    for name, r in results:
        text += f"{name}\nTrades: {r['trades']}\nWins: {r['wins']}\nLosses: {r['losses']}\nWin rate: {r['win_rate']}%\nNote: {r['note']}\n\n"
    await safe_send(message, text + "⚠️ الاختبار تاريخي وليس ضمانًا للنتائج المستقبلية.")

@dp.message(Command("weekly_table"))
async def cmd_weekly_table(message: types.Message):
    if not TWELVE_DATA_API_KEY:
        await safe_send(message, "❌ TWELVE_DATA_API_KEY غير موجود."); return
    rows=[]
    for _,cfg in ASSETS.items():
        try:
            r=backtest_ema_rsi(await get_time_series(cfg["symbol"],500)); rows.append((cfg["name"],r["trades"],r["win_rate"]))
        except Exception:
            rows.append((cfg["name"],0,0))
    rows.sort(key=lambda x:x[2],reverse=True)
    text="📅 جدول الأداء الحالي\n\n"+"\n\n".join(f"{i}. {n}\n   Trades: {t}\n   Win rate: {w}%" for i,(n,t,w) in enumerate(rows,1))
    await safe_send(message,text+"\n\nمبني على Backtest فعلي للبيانات المتاحة.")

# ============================================================
# STRATEGY LEARNING / VIDEO TRAINING ENGINE
# ============================================================
@dataclass
class StrategyDefinition:
    id: str
    name: str
    source_url: str
    source_text: str
    description: str
    indicators: List[str]
    buy_rules: List[str]
    sell_rules: List[str]
    sl_atr: float
    tp_atr: float
    max_hold_bars: int
    created_at: str
    updated_at: str


@dataclass
class StrategyResult:
    strategy_id: str
    tests: int
    trades: int
    wins: int
    losses: int
    neutral: int
    win_rate: float
    profit_factor: float
    net_r: float
    max_drawdown_r: float
    expectancy_r: float
    score: float
    robustness: float
    verdict: str
    trained_at: str
    assets_tested: int = 0
    r_values: List[float] = field(default_factory=list)


strategies_db: Dict[str, dict] = {"strategies": {}, "results": {}}
strategy_db_lock = asyncio.Lock()


def load_strategies():
    global strategies_db
    if not os.path.exists(STRATEGY_DB_FILE):
        return
    try:
        with open(STRATEGY_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            strategies_db = {
                "strategies": data.get("strategies", {}),
                "results": data.get("results", {}),
            }
        logger.info("Loaded %s learned strategies", len(strategies_db["strategies"]))
    except Exception:
        logger.exception("Could not load strategy database")


def save_strategies():
    try:
        tmp = STRATEGY_DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(strategies_db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STRATEGY_DB_FILE)
    except Exception:
        logger.exception("Could not save strategy database")


def extract_url(text: str) -> Optional[str]:
    match = re.search(r"https?://[^\s<>\"]+", text or "", re.IGNORECASE)
    if not match:
        return None
    return match.group(0).rstrip(").,]}>\"'")


def is_video_url(url: str) -> bool:
    if not url:
        return False
    low = url.lower()
    video_hosts = (
        "youtube.com", "youtu.be", "youtube-nocookie.com",
        "vimeo.com", "dailymotion.com", "tiktok.com",
        "instagram.com", "facebook.com", "fb.watch"
    )
    return any(host in low for host in video_hosts)


def clean_vtt_text(raw: str) -> str:
    lines = []
    previous = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or "-->" in line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"&nbsp;", " ", line)
        line = re.sub(r"&amp;", "&", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == previous:
            continue
        lines.append(line)
        previous = line
    return "\n".join(lines)


async def extract_video_text(url: str) -> str:
    """
    Extracts available subtitles only. It never claims that the video was watched.
    If subtitles are unavailable, returns an empty string.
    """
    if not shutil.which(sys.executable):
        return ""
    with tempfile.TemporaryDirectory(prefix="strategy_video_") as tmp:
        output_template = str(Path(tmp) / "%(id)s.%(ext)s")
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en.*,ar.*,fr.*",
            "--sub-format", "vtt",
            "--no-warnings",
            "--quiet",
            "-o", output_template,
            url,
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except Exception as exc:
            logger.warning("Video subtitle extraction failed: %s", exc)
            return ""

        if proc.returncode != 0:
            logger.warning("yt-dlp failed: %s", (proc.stderr or "")[-500:])
            return ""

        parts = []
        for vtt in Path(tmp).glob("*.vtt"):
            try:
                text = clean_vtt_text(vtt.read_text(encoding="utf-8", errors="ignore"))
                if text:
                    parts.append(text)
            except Exception:
                continue

        if not parts:
            return ""
        # Avoid feeding the same auto-subtitle content repeatedly.
        unique = []
        seen = set()
        for part in parts:
            key = part[:1000]
            if key not in seen:
                seen.add(key)
                unique.append(part)
        return "\n".join(unique)[:30000]


def extract_json_from_ai(text: str) -> Optional[dict]:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


SUPPORTED_RULE_PATTERNS = [
    re.compile(r"^price\s*(>=|<=|>|<)\s*(ema20|ema50)$", re.I),
    re.compile(r"^ema20\s*(>=|<=|>|<)\s*ema50$", re.I),
    re.compile(r"^rsi\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", re.I),
    re.compile(r"^macd\s*hist(?:ogram)?\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", re.I),
    re.compile(r"^momentum6\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", re.I),
]


def normalize_rule(rule: str) -> str:
    r = str(rule or "").strip()
    r = r.replace(" ", "")
    r = r.replace("MACDHistogram", "MACDhist").replace("macdhistogram", "MACDhist")
    r = r.replace("EMA20", "EMA20").replace("EMA50", "EMA50")
    r = r.replace("RSI", "RSI").replace("Price", "price").replace("PRICE", "price")
    r = r.replace("Momentum6", "momentum6").replace("MOMENTUM6", "momentum6")
    return r


def rule_supported(rule: str) -> bool:
    r = normalize_rule(rule)
    return any(p.fullmatch(r) for p in SUPPORTED_RULE_PATTERNS)


def evaluate_rule(rule: str, price: float, ema20_v: float, ema50_v: float,
                  rsi_v: float, macd_hist_v: float, momentum6_v: float) -> bool:
    r = normalize_rule(rule)
    m = re.fullmatch(r"price(>=|<=|>|<)(ema20|ema50)", r, re.I)
    if m:
        left = price
        right = ema20_v if m.group(2).lower() == "ema20" else ema50_v
    else:
        m = re.fullmatch(r"ema20(>=|<=|>|<)ema50", r, re.I)
        if m:
            left, right = ema20_v, ema50_v
        else:
            m = re.fullmatch(r"rsi(>=|<=|>|<)(-?\d+(?:\.\d+)?)", r, re.I)
            if m:
                left, right = rsi_v, float(m.group(2))
            else:
                m = re.fullmatch(r"macdhist(?:ogram)?(>=|<=|>|<)(-?\d+(?:\.\d+)?)", r, re.I)
                if m:
                    left, right = macd_hist_v, float(m.group(2))
                else:
                    m = re.fullmatch(r"momentum6(>=|<=|>|<)(-?\d+(?:\.\d+)?)", r, re.I)
                    if m:
                        left, right = momentum6_v, float(m.group(2))
                    else:
                        raise ValueError(f"Unsupported rule: {rule}")

    op = m.group(1)
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    return False


async def convert_content_to_strategy(source_url: str, source_text: str) -> Optional[StrategyDefinition]:
    prompt = f"""
أنت مهندس استراتيجيات تداول. استخرج فقط استراتيجية قابلة للاختبار من المحتوى التالي.
لا تخترع قواعد غير موجودة في المصدر. إذا كانت قاعدة غير قابلة للتمثيل بالقواعد المدعومة أدناه، ضعها في unsupported_rules ولا تضعها داخل buy_rules أو sell_rules.

القواعد المدعومة حرفيًا فقط:
- price>EMA20 / price<EMA20 / price>=EMA20 / price<=EMA20
- price>EMA50 / price<EMA50 / price>=EMA50 / price<=EMA50
- EMA20>EMA50 / EMA20<EMA50 / EMA20>=EMA50 / EMA20<=EMA50
- RSI>NUMBER / RSI<NUMBER / RSI>=NUMBER / RSI<=NUMBER
- MACDhist>NUMBER / MACDhist<NUMBER / MACDhist>=NUMBER / MACDhist<=NUMBER
- momentum6>NUMBER / momentum6<NUMBER / momentum6>=NUMBER / momentum6<=NUMBER

أخرج JSON فقط:
{{
  "name": "اسم الاستراتيجية",
  "description": "وصف قصير دقيق",
  "indicators": ["EMA20", "RSI"],
  "buy_rules": ["price>EMA20"],
  "sell_rules": ["price<EMA20"],
  "unsupported_rules": [],
  "sl_atr": 1.2,
  "tp_atr": 1.6,
  "max_hold_bars": 12
}}

مهم:
- buy_rules وsell_rules يجب أن تحتويان على كل الشروط اللازمة كما وردت في المصدر.
- إذا لم توجد استراتيجية واضحة، أعد "buy_rules":[] و"sell_rules":[].
- sl_atr وtp_atr يجب أن يعكسا المصدر إن كان واضحًا. إذا لم يذكر المصدر قيمًا، استخدم 1.2 و1.6 فقط كإعداد اختبار قياسي، واذكر ذلك في description.
- لا تدّع أنك شاهدت الفيديو؛ المصدر المتاح هو النص فقط.

SOURCE URL:
{source_url}

SOURCE CONTENT:
{source_text[:30000]}
"""
    ai = await safe_ai_generate(prompt)
    data = extract_json_from_ai(ai)
    if not data:
        return None

    buy_rules = [normalize_rule(x) for x in data.get("buy_rules", []) if str(x).strip()]
    sell_rules = [normalize_rule(x) for x in data.get("sell_rules", []) if str(x).strip()]
    unsupported = [str(x) for x in data.get("unsupported_rules", []) if str(x).strip()]

    for rule in buy_rules + sell_rules:
        if not rule_supported(rule):
            unsupported.append(rule)

    if not buy_rules and not sell_rules:
        return None
    if unsupported:
        # Do not silently backtest an incomplete strategy.
        logger.warning("Strategy contains unsupported rules: %s", unsupported)
        return None

    try:
        sl_atr = float(data.get("sl_atr", 1.2))
        tp_atr = float(data.get("tp_atr", 1.6))
        max_hold = int(data.get("max_hold_bars", 12))
    except (TypeError, ValueError):
        sl_atr, tp_atr, max_hold = 1.2, 1.6, 12

    if sl_atr <= 0 or tp_atr <= 0 or max_hold < 1:
        return None

    now = now_local().isoformat()
    sid = "STR-" + uuid.uuid4().hex[:8].upper()
    return StrategyDefinition(
        id=sid,
        name=str(data.get("name") or "Learned Strategy")[:120],
        source_url=source_url,
        source_text=source_text[:30000],
        description=str(data.get("description") or "")[:1500],
        indicators=[str(x) for x in data.get("indicators", [])][:20],
        buy_rules=buy_rules,
        sell_rules=sell_rules,
        sl_atr=sl_atr,
        tp_atr=tp_atr,
        max_hold_bars=max_hold,
        created_at=now,
        updated_at=now,
    )


def strategy_signal(strategy: StrategyDefinition, candles: List[dict], index: int) -> Optional[str]:
    if index < 60 or index >= len(candles):
        return None
    closes = [x["close"] for x in candles[:index + 1]]
    highs = [x["high"] for x in candles[:index + 1]]
    lows = [x["low"] for x in candles[:index + 1]]
    price = closes[-1]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    rr = rsi(closes, 14)
    aa = atr(highs, lows, closes, 14)
    _, _, mh = macd(closes)
    momentum6 = price - closes[-7] if len(closes) >= 7 else 0.0
    if None in (e20, e50, rr, aa, mh) or aa <= 0:
        return None

    try:
        if strategy.buy_rules and all(
            evaluate_rule(rule, price, e20, e50, rr, mh, momentum6)
            for rule in strategy.buy_rules
        ):
            return "BUY"
        if strategy.sell_rules and all(
            evaluate_rule(rule, price, e20, e50, rr, mh, momentum6)
            for rule in strategy.sell_rules
        ):
            return "SELL"
    except ValueError:
        return None
    return None


def simulate_strategy_test(strategy: StrategyDefinition, candles: List[dict], start_index: int) -> float:
    if start_index < 60 or start_index >= len(candles) - 1:
        return 0.0
    side = strategy_signal(strategy, candles, start_index)
    if side is None:
        return 0.0

    closes = [x["close"] for x in candles[:start_index + 1]]
    highs = [x["high"] for x in candles[:start_index + 1]]
    lows = [x["low"] for x in candles[:start_index + 1]]
    price = closes[-1]
    aa = atr(highs, lows, closes, 14)
    if aa is None or aa <= 0:
        return 0.0

    if side == "BUY":
        sl = price - aa * strategy.sl_atr
        tp = price + aa * strategy.tp_atr
    else:
        sl = price + aa * strategy.sl_atr
        tp = price - aa * strategy.tp_atr

    end = min(len(candles), start_index + 1 + strategy.max_hold_bars)
    for j in range(start_index + 1, end):
        bar = candles[j]
        # Conservative rule: if both levels are touched in one candle,
        # count SL first because OHLC does not reveal intrabar order.
        if side == "BUY":
            if bar["low"] <= sl:
                return -1.0
            if bar["high"] >= tp:
                return strategy.tp_atr / strategy.sl_atr
        else:
            if bar["high"] >= sl:
                return -1.0
            if bar["low"] <= tp:
                return strategy.tp_atr / strategy.sl_atr
    return 0.0


def calculate_strategy_metrics(strategy_id: str, r_values: List[float], tests: int, assets_tested: int) -> StrategyResult:
    trades = [r for r in r_values if r != 0.0]
    wins = [r for r in trades if r > 0]
    losses = [r for r in trades if r < 0]
    neutral = tests - len(trades)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    net_r = sum(trades)
    expectancy = net_r / len(trades) if trades else 0.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    sample_factor = min(1.0, len(trades) / 50.0)
    pf_factor = min(1.0, max(pf, 0.0) / 2.0)
    wr_factor = min(1.0, win_rate / 70.0)
    dd_factor = max(0.0, 1.0 - max_dd / max(5.0, abs(net_r) + 5.0))
    expectancy_factor = min(1.0, max(0.0, expectancy) / 0.6)
    score = 100.0 * (
        0.25 * pf_factor +
        0.20 * wr_factor +
        0.20 * max(0.0, min(1.0, (net_r + 10.0) / 40.0)) +
        0.15 * dd_factor +
        0.10 * expectancy_factor +
        0.10 * sample_factor
    )
    robustness = 100.0 * (
        0.6 * sample_factor +
        0.4 * max(0.0, min(1.0, assets_tested / max(1, len(ASSETS))))
    )

    if len(trades) < STRATEGY_MIN_TRADES:
        verdict = "INSUFFICIENT DATA"
    elif score >= 80 and pf >= 1.5 and net_r > 0:
        verdict = "STRONG"
    elif score >= 65 and pf >= 1.15 and net_r > 0:
        verdict = "PROMISING"
    elif score >= 50:
        verdict = "WEAK"
    else:
        verdict = "FAILED"

    return StrategyResult(
        strategy_id=strategy_id,
        tests=tests,
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        neutral=neutral,
        win_rate=round(win_rate, 2),
        profit_factor=round(pf, 3),
        net_r=round(net_r, 3),
        max_drawdown_r=round(max_dd, 3),
        expectancy_r=round(expectancy, 4),
        score=round(score, 2),
        robustness=round(robustness, 2),
        verdict=verdict,
        trained_at=now_local().isoformat(),
        assets_tested=assets_tested,
        r_values=[round(x, 6) for x in r_values],
    )


async def train_strategy(strategy: StrategyDefinition) -> StrategyResult:
    async with STRATEGY_TRAINING_LOCK:
        training_status.update({
            "running": True,
            "strategy_id": strategy.id,
            "strategy_name": strategy.name,
            "tests": 0,
            "trades": 0,
            "message": "loading historical candles",
        })
        all_r = []
        assets_tested = 0

        try:
            for asset_key, cfg in ASSETS.items():
                try:
                    candles = await get_time_series(cfg["symbol"], 500)
                    max_index = len(candles) - strategy.max_hold_bars - 1
                    first_index = 60
                    if max_index <= first_index:
                        continue

                    available = max_index - first_index + 1
                    count = min(TRAINING_TESTS, available)

                    # Evenly distributed walk-forward samples across the history.
                    if count == 1:
                        indices = [first_index]
                    else:
                        indices = sorted(set(
                            first_index + round(i * (available - 1) / (count - 1))
                            for i in range(count)
                        ))

                    for idx in indices:
                        r = simulate_strategy_test(strategy, candles, idx)
                        all_r.append(r)

                    assets_tested += 1
                    training_status["tests"] = len(all_r)
                    training_status["trades"] = sum(1 for x in all_r if x != 0.0)
                    training_status["message"] = f"testing {asset_key}"
                except Exception as exc:
                    logger.warning("Strategy training failed on %s: %s", asset_key, exc)

            result = calculate_strategy_metrics(strategy.id, all_r, len(all_r), assets_tested)
            strategies_db["results"][strategy.id] = asdict(result)
            strategies_db["strategies"][strategy.id] = asdict(strategy)
            save_strategies()
            return result
        finally:
            training_status["running"] = False
            training_status["message"] = "complete"


def strategy_from_db(strategy_id: str) -> Optional[StrategyDefinition]:
    raw = strategies_db.get("strategies", {}).get(strategy_id)
    if not raw:
        return None
    try:
        return StrategyDefinition(**raw)
    except Exception:
        logger.exception("Invalid strategy record %s", strategy_id)
        return None


def result_from_db(strategy_id: str) -> Optional[StrategyResult]:
    raw = strategies_db.get("results", {}).get(strategy_id)
    if not raw:
        return None
    try:
        return StrategyResult(**raw)
    except Exception:
        return None


def strategy_display(strategy: StrategyDefinition, result: Optional[StrategyResult]) -> str:
    lines = [
        f"🧠 {strategy.name}",
        f"ID: {strategy.id}",
        f"الوصف: {strategy.description or 'غير متوفر'}",
        f"Indicators: {', '.join(strategy.indicators) if strategy.indicators else '—'}",
        f"BUY rules: {', '.join(strategy.buy_rules) if strategy.buy_rules else '—'}",
        f"SELL rules: {', '.join(strategy.sell_rules) if strategy.sell_rules else '—'}",
        f"SL ATR: {strategy.sl_atr}",
        f"TP ATR: {strategy.tp_atr}",
        f"Max hold: {strategy.max_hold_bars} bars",
    ]
    if result:
        lines += [
            "",
            "نتيجة التدريب:",
            f"Tests: {result.tests}",
            f"Trades: {result.trades}",
            f"Wins/Losses: {result.wins}/{result.losses}",
            f"Win rate: {result.win_rate}%",
            f"Profit factor: {result.profit_factor}",
            f"Net R: {result.net_r}",
            f"Max drawdown R: {result.max_drawdown_r}",
            f"Expectancy R: {result.expectancy_r}",
            f"Score: {result.score}/100",
            f"Robustness: {result.robustness}/100",
            f"Verdict: {result.verdict}",
            f"Assets tested: {result.assets_tested}",
            f"Trained: {result.trained_at}",
        ]
    return "\n".join(lines)


async def process_strategy_video(message: types.Message, url: str, original_text: str):
    await safe_send(message, "🎥 تم اكتشاف رابط فيديو. أحاول استخراج الترجمة/النص المتاح ثم تحويله إلى استراتيجية قابلة للاختبار...")

    transcript = await extract_video_text(url)
    if not transcript:
        await safe_send(
            message,
            "❌ لم أستطع استخراج نص/ترجمة من الفيديو.\n"
            "لم أشاهد الفيديو ولم أختلق محتواه.\n"
            "أرسل رابط فيديو يحتوي على ترجمة متاحة، أو أرسل نص الاستراتيجية مباشرة."
        )
        return

    strategy = await convert_content_to_strategy(url, transcript)
    if not strategy:
        await safe_send(
            message,
            "❌ تم استخراج النص، لكن لم أستطع تحويله إلى قواعد تداول قابلة للاختبار "
            "بالصيغة المدعومة دون اختراع قواعد."
        )
        return

    strategies_db["strategies"][strategy.id] = asdict(strategy)
    save_strategies()

    await safe_send(
        message,
        f"🧠 تم استخراج الاستراتيجية: {strategy.name}\n"
        f"ID: {strategy.id}\n\n"
        f"🧪 سأختبرها على الأقل {TRAINING_TESTS} حالة تاريخية لكل أصل متاح "
        f"وفي منطقة التدريب فقط. لن تدخل هذه الاستراتيجية في التداول الحي تلقائيًا."
    )

    try:
        result = await train_strategy(strategy)
        await safe_send(
            message,
            strategy_display(strategy, result)
            + "\n\n⚠️ الاختبار تاريخي، والنتيجة لا تضمن الأداء المستقبلي."
        )
    except Exception as exc:
        logger.exception("Strategy training failed")
        await safe_send(message, f"❌ فشل تدريب الاستراتيجية: {str(exc)[:700]}")


def top_strategies(limit: int = 10):
    rows = []
    for sid, raw in strategies_db.get("results", {}).items():
        try:
            result = StrategyResult(**raw)
            strategy = strategy_from_db(sid)
            if strategy:
                rows.append((strategy, result))
        except Exception:
            continue
    rows.sort(key=lambda pair: (pair[1].score, pair[1].profit_factor, pair[1].net_r), reverse=True)
    return rows[:limit]


# ============================================================
# NEWS / FORWARDED TEXT
# ============================================================
def detect_assets_in_text(text: str) -> List[str]:
    t=(text or "").lower(); found=[]
    keywords={
        "gold":["gold","xau","ذهب","الذهب"], "btc":["bitcoin","btc","بيتكوين","البتكوين"],
        "eurusd":["eurusd","eur/usd","euro","اليورو"], "silver":["silver","xag","فضة","الفضة"],
        "oil":["oil","wti","crude","نفط","النفط"], "eth":["ethereum","eth","ايثيريوم","إيثيريوم"]}
    for k,words in keywords.items():
        if any(w in t for w in words): found.append(k)
    return found

async def process_news_or_strategy(message: types.Message, text: str):
    forward_origin = getattr(message, "forward_origin", None)
    is_forward = forward_origin is not None
    url = extract_url(text)
    if url and is_video_url(url):
        await process_strategy_video(message, url, text)
        return

    is_strategy_text = "استراتيجية" in text.lower() or "strategy" in text.lower()
    kind = "خبر مُعاد توجيهه" if is_forward else ("استراتيجية/نص" if is_strategy_text else "خبر")
    await safe_send(message, f"🧠 جاري تحليل {kind}...")

    prompt = f"""
حلل النص التالي كمحلل مخاطر للأسواق. لا تخترع تفاصيل.
النص:
{text}
حدد: نوع المحتوى، الأصول المتأثرة، اتجاه التأثير bullish/bearish/mixed/unknown،
قوة 1-5، المدة دقائق/ساعات/أيام، وما يجب مراقبته.
لا تضمن الربح.
إذا كان النص يحتوي رابط فيديو ولم يتوفر محتواه الفعلي، لا تدّع أنك شاهدت الفيديو.
"""
    ai = await safe_ai_generate(prompt) or "تعذر الوصول إلى Gemini حاليًا. تم استلام النص ويمكن إعادة المحاولة."

    os.makedirs("memory", exist_ok=True)
    filename = "memory/strategies_memory.txt" if is_strategy_text else "memory/news_memory.txt"
    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(
                f"\n[{now_local().isoformat()}]\nTYPE: {kind}\nTEXT:\n{text}\n"
                f"ANALYSIS:\n{ai}\n" + "=" * 70 + "\n"
            )
    except Exception:
        logger.exception("Could not save memory")

    await safe_send(message, f"📰 تحليل {kind}\n\n{ai}")

    affected = detect_assets_in_text(text)
    for trade in [t for t in list(open_trades.values()) if t.status == "OPEN" and t.asset_key in affected]:
        try:
            trade, analysis, events = await reanalyze_trade(trade, reason="news")
            await safe_reply(
                trade.chat_id,
                f"⚡ إعادة تحليل فورية بسبب خبر\nالأصل: {trade.asset_name}\nTrade ID: {trade.id}\n"
                f"السعر: {format_price(analysis.price)}\nالإشارة: {analysis.signal}\n"
                f"الحالة: {trade.status}\nالإجراء: {trade.last_action}\n"
                f"TP events: {', '.join(events) if events else 'none'}"
            )
        except Exception:
            logger.exception("News reanalysis failed")


@dp.message(F.text)
async def handle_text(message: types.Message):
    text=message.text or ""
    if text and not text.startswith("/"):
        await process_news_or_strategy(message,text)

@dp.message(F.caption)
async def handle_caption(message: types.Message):
    text=message.caption or ""
    if text:
        await process_news_or_strategy(message,text)

# ============================================================
# CONTINUOUS TRADE MONITOR
# ============================================================
async def monitor_one_trade(trade: Trade):
    try:
        # Market-close protection is checked before the next price request.
        # This guarantees a trade cannot remain OPEN after the configured close.
        if trade_expired(trade):
            save_state()
            await safe_reply(trade.chat_id,
                f"⏰ إغلاق تلقائي بسبب إغلاق السوق\nالأصل: {trade.asset_name}\n"
                f"Trade ID: {trade.id}\nالحالة: {trade.status}\n"
                f"الإجراء: {trade.last_action}")
            return
        # Price-only polling every 30 seconds: protects SL/TP between scheduled reviews.
        price = await get_price(trade.symbol, use_cache=False)
        events = evaluate_trade_price(trade, price)
        changed = False
        notifications=[]
        for event in events:
            if event.startswith("TP"):
                n=int(event[2:])
                if n not in trade.notified_tps:
                    trade.notified_tps.append(n); notifications.append(event); changed=True
            else:
                notifications.append(event); changed=True
        if trade.status != "OPEN":
            changed=True
        if changed:
            save_state()
            if notifications:
                await safe_reply(trade.chat_id,
                    f"📡 تحديث مدير الصفقة\nالأصل: {trade.asset_name}\nTrade ID: {trade.id}\n"
                    f"السعر الحي: {format_price(trade.last_price)}\nالأحداث: {', '.join(notifications)}\n"
                    f"الحالة: {trade.status}\nالإجراء: {trade.last_action}")
    except Exception as exc:
        logger.warning("Price monitor failed for %s: %s", trade.id, exc)

async def trade_monitor():
    await asyncio.sleep(10)
    while True:
        try:
            trades=[t for t in list(open_trades.values()) if t.status=="OPEN"]
            # First: lightweight live price monitoring for every open trade.
            await asyncio.gather(*(monitor_one_trade(t) for t in trades), return_exceptions=True)
            now=now_local()
            due=[t for t in list(open_trades.values()) if t.status=="OPEN" and dt_from_string(t.next_reanalysis_at)<=now]
            for trade in due:
                try:
                    old_action=trade.last_action
                    trade,analysis,events=await reanalyze_trade(trade,reason="scheduled")
                    # Notify every scheduled review; this makes management visible.
                    msg=(f"🔄 إعادة تحليل الصفقة\nالأصل: {trade.asset_name}\nTrade ID: {trade.id}\n"
                         f"السعر الحي: {format_price(analysis.price)}\nالإشارة: {analysis.signal}\n"
                         f"الحالة: {trade.status}\nManagement: {trade.management}\nAction: {trade.last_action}\n"
                         f"TPs hit: {len(trade.hit_tps)}/{len(trade.tps)}\n"
                         f"الأحداث: {', '.join(events) if events else 'none'}")
                    if trade.status=="OPEN":
                        msg += f"\nالمراجعة القادمة: {dt_from_string(trade.next_reanalysis_at).strftime('%H:%M:%S')}"
                    else:
                        msg += "\nالصفقة لم تعد مفتوحة."
                    if old_action!=trade.last_action or events or trade.status!="OPEN" or trade.management in ("ADJUST","HOLD"):
                        await safe_reply(trade.chat_id,msg)
                except Exception as exc:
                    logger.exception("Scheduled reanalysis failed for %s: %s",trade.id,exc)
                    trade.last_action="DATA ERROR - RETRY"
                    save_state()
            # Keep at most 100 closed/expired records.
            closed=[tid for tid,t in open_trades.items() if t.status!="OPEN"]
            if len(closed)>100:
                for tid in closed[:-100]: open_trades.pop(tid,None)
                save_state()
        except Exception:
            logger.exception("Trade monitor loop error")
        await asyncio.sleep(MONITOR_SECONDS)

# ============================================================
# HEALTH SERVER
# ============================================================
async def health(request: web.Request):
    return web.json_response({
        "status":"ok", "bot":"Trading Bot",
        "open_trades":len([t for t in open_trades.values() if t.status=="OPEN"]),
        "market_data_configured":bool(TWELVE_DATA_API_KEY), "gemini_keys":len(GEMINI_KEYS),
        "time":now_local().isoformat(), "monitor_seconds":MONITOR_SECONDS,
    })

async def start_web_server():
    app=web.Application(); app.add_routes([web.get("/",health),web.get("/health",health)])
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",PORT); await site.start()
    logger.info("Health server listening on port %s",PORT)
    return runner

# ============================================================
# STARTUP / SHUTDOWN
# ============================================================
async def main():
    load_state()
    load_strategies()
    if not TWELVE_DATA_API_KEY: logger.warning("TWELVE_DATA_API_KEY is missing.")
    if not GEMINI_KEYS: logger.warning("No Gemini API keys configured.")
    runner=await start_web_server()
    monitor_task=asyncio.create_task(trade_monitor())
    try:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            logger.warning("delete_webhook warning: %s",exc)
        logger.info("Starting Telegram polling")
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        monitor_task.cancel()
        try: await monitor_task
        except asyncio.CancelledError: pass
        save_state()
        try: await runner.cleanup()
        except Exception: pass
        await bot.session.close()
        logger.info("Trading Bot stopped")

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
