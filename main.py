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
import time
import sqlite3
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
    import psycopg
except Exception:
    psycopg = None

try:
    from google import genai
except Exception:
    genai = None

# ============================================================
# CONFIGURATION
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
def get_twelve_data_api_key() -> str:
    return os.getenv("TWELVE_DATA_API_KEY", "").strip()

TWELVE_DATA_API_KEY = get_twelve_data_api_key()

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
# Risk management for virtual/paper trading. Position size is expressed in
# base units and assumes USD-quoted instruments. It is not broker lot sizing.
ACCOUNT_BALANCE = max(0.0, float(os.getenv("TRADING_ACCOUNT_BALANCE", "1000")))
RISK_PER_TRADE_PCT = max(0.0, float(os.getenv("RISK_PER_TRADE_PCT", "1.0")))
MAX_TOTAL_OPEN_RISK_PCT = max(0.0, float(os.getenv("MAX_TOTAL_OPEN_RISK_PCT", "4.0")))
DAILY_RISK_LIMIT_PCT = max(0.0, float(os.getenv("DAILY_RISK_LIMIT_PCT", "5.0")))
MIN_RR = max(0.0, float(os.getenv("MIN_RR", "0.0")))
MARKET_CACHE_SECONDS = int(os.getenv("MARKET_CACHE_SECONDS", "30"))
PRICE_CACHE_SECONDS = int(os.getenv("PRICE_CACHE_SECONDS", "8"))
MONITOR_SECONDS = int(os.getenv("MONITOR_SECONDS", "30"))
MIN_TRADE_DURATION_MINUTES = max(1, int(os.getenv("MIN_TRADE_DURATION_MINUTES", "1")))
MAX_TRADE_DURATION_MINUTES = min(72 * 60, max(MIN_TRADE_DURATION_MINUTES, int(os.getenv("MAX_TRADE_DURATION_MINUTES", str(72 * 60)))))
VIDEO_TRADE_MAX = max(1, min(50, int(os.getenv("VIDEO_TRADE_MAX", "20"))))
VIDEO_AGENTIC = os.getenv("VIDEO_AGENTIC", "true").lower() in ("1", "true", "yes", "on")
SIGNAL_SCORE_THRESHOLD = max(1, int(os.getenv("SIGNAL_SCORE_THRESHOLD", "3")))
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
STRATEGY_MIN_TRADES = max(70, int(os.getenv("STRATEGY_MIN_TRADES", "70")))
OOS_MIN_TRADES = max(20, int(os.getenv("OOS_MIN_TRADES", "20")))
USE_LEARNED_STRATEGY = os.getenv("USE_LEARNED_STRATEGY", "true").lower() in ("1", "true", "yes", "on")
LEARNED_MIN_SCORE = float(os.getenv("LEARNED_MIN_SCORE", "65"))
TP_ALLOCATION = [float(x) for x in os.getenv("TP_ALLOCATION", "0.10,0.10,0.10,0.10,0.10,0.10,0.10,0.10,0.10,0.10").split(",")]
if len(TP_ALLOCATION) != 10 or abs(sum(TP_ALLOCATION)-1.0) > 1e-6:
    TP_ALLOCATION = [0.10] * 10
BREAK_EVEN_AFTER_TP = max(1, int(os.getenv("BREAK_EVEN_AFTER_TP", "1")))
TRAILING_AFTER_TP = max(1, int(os.getenv("TRAILING_AFTER_TP", "3")))
TRAILING_ATR_MULT = max(0.1, float(os.getenv("TRAILING_ATR_MULT", "0.8")))
STALE_PRICE_SECONDS = max(5, int(os.getenv("STALE_PRICE_SECONDS", "45")))
NEWS_DB_FILE = os.getenv("NEWS_DB_FILE", "news_learning.json")
# Persistent storage. On Render, set DATABASE_URL to a Render Postgres database.
# SQLite is retained only as a local/fallback store.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PERSISTENCE_DB_FILE = os.getenv("PERSISTENCE_DB_FILE", "tradingbot.sqlite3")
SELF_PING_ENABLED = os.getenv("SELF_PING_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SELF_PING_INTERVAL_SECONDS = max(60, int(os.getenv("SELF_PING_INTERVAL_SECONDS", "300")))
PUBLIC_BASE_URL = os.getenv("RENDER_EXTERNAL_URL", os.getenv("PUBLIC_BASE_URL", "")).strip().rstrip("/")
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
    key = get_twelve_data_api_key()
    if not key:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured in Render Environment Variables.")
    params = dict(params)
    params["apikey"] = key
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
async def get_time_series_interval(symbol: str, interval: str, outputsize: int = 300) -> List[dict]:
    cache_key=f"{symbol}:{interval}:{outputsize}"
    now=datetime.now(timezone.utc); cached=market_cache.get(cache_key)
    if cached and (now-cached[0]).total_seconds()<MARKET_CACHE_SECONDS: return cached[1]
    async with market_lock:
        cached=market_cache.get(cache_key); now=datetime.now(timezone.utc)
        if cached and (now-cached[0]).total_seconds()<MARKET_CACHE_SECONDS: return cached[1]
        data=await td_get("time_series",{"symbol":symbol,"interval":interval,"outputsize":min(max(outputsize,60),5000),"format":"JSON","timezone":"UTC"})
        values=[]
        for row in reversed(data.get("values") or []):
            try: values.append({"datetime":row.get("datetime",""),"open":float(row["open"]),"high":float(row["high"]),"low":float(row["low"]),"close":float(row["close"]),"volume":float(row.get("volume",0) or 0)})
            except Exception: continue
        if len(values)<60: raise RuntimeError(f"Insufficient {interval} candles for {symbol}")
        market_cache[cache_key]=(datetime.now(timezone.utc),values); return values

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


def candle_interval_minutes(interval: str) -> int:
    """Convert Twelve Data interval text to minutes."""
    m = re.fullmatch(r"\s*(\d+)\s*(min|h|day|week)\s*", (interval or "5min").lower())
    if not m:
        return 5
    n, unit = int(m.group(1)), m.group(2)
    return {"min": n, "h": n * 60, "day": n * 1440, "week": n * 10080}[unit]

def estimate_trade_duration_minutes(
    signal: str,
    confidence: float,
    score: int,
    price: float,
    atr_value: float,
    ema20: float,
    ema50: float,
    momentum: float,
    candle_interval_minutes: int,
) -> int:
    """Estimate a variable holding time from market structure, not fixed buckets.

    The estimate is intentionally bounded to 1 minute .. 72 hours. It uses
    volatility, directional momentum, EMA structure, confidence and signal
    strength. It is an estimate, not a guarantee of how long a trade will need.
    """
    if signal not in ("BUY", "SELL") or atr_value <= 0 or price <= 0:
        return 0

    # Expected directional movement per candle. ATR is the volatility envelope;
    # momentum supplies a directional-speed component.
    momentum_speed = abs(momentum) / max(1, min(6, 6))
    movement_per_candle = max(atr_value * 0.12, momentum_speed * 0.85)

    # Trend alignment increases expected persistence; conflicting EMA structure
    # shortens the expected holding time.
    if signal == "BUY":
        aligned = price > ema20 and ema20 > ema50
    else:
        aligned = price < ema20 and ema20 < ema50
    structure_factor = 1.35 if aligned else 0.82

    strength = min(1.0, abs(score) / 8.0)
    confidence_factor = 0.80 + min(0.55, max(0.0, confidence - 50.0) / 100.0)

    # Use the midpoint of the TP ladder as the primary expected horizon.
    # Strong momentum reduces the time; weak momentum increases it.
    target_distance = atr_value * 2.4
    raw_bars = target_distance / max(movement_per_candle, atr_value * 0.03)
    raw_bars *= (1.10 - 0.35 * strength)
    raw_bars /= structure_factor
    raw_bars /= confidence_factor

    minutes = raw_bars * max(1, candle_interval_minutes)

    # Give exceptionally strong/weak setups room to naturally span intraday
    # through multi-day horizons, while never exceeding the requested 72h.
    if strength >= 0.875 and aligned:
        minutes *= 0.75
    elif strength <= 0.50 or not aligned:
        minutes *= 1.35

    return int(min(MAX_TRADE_DURATION_MINUTES, max(MIN_TRADE_DURATION_MINUTES, round(minutes))))

def format_duration_minutes(minutes: int) -> str:
    """Human-readable duration for Telegram output."""
    if minutes < 60:
        return f"{minutes} دقيقة"
    if minutes < 1440:
        hours = minutes / 60.0
        if abs(hours - round(hours)) < 1e-9:
            return f"{int(round(hours))} ساعة"
        return f"{hours:.1f} ساعة"
    days = minutes / 1440.0
    if abs(days - round(days)) < 1e-9:
        return f"{int(round(days))} يوم"
    return f"{days:.1f} يوم"

def analyze_market(asset_key: str, candles: List[dict], live_price: Optional[float] = None, interval_minutes: Optional[int] = None) -> Analysis:
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

    signal = "BUY" if score >= SIGNAL_SCORE_THRESHOLD else "SELL" if score <= -SIGNAL_SCORE_THRESHOLD else "NO TRADE"
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
    duration = estimate_trade_duration_minutes(
        signal=signal,
        confidence=confidence,
        score=score,
        price=price,
        atr_value=a,
        ema20=e20,
        ema50=e50,
        momentum=momentum,
        candle_interval_minutes=(interval_minutes or candle_interval_minutes(TD_INTERVAL)),
    )
    return Analysis(
        asset_key=asset_key, asset_name=cfg["name"], symbol=cfg["symbol"], price=price,
        signal=signal, confidence=confidence, entry_low=entry_low, entry_high=entry_high,
        sl=sl, tps=tps, atr_value=a, rsi_value=r, ema20=e20, ema50=e50,
        macd_value=m_line, macd_signal=m_signal, macd_hist=m_hist,
        support=support, resistance=resistance, duration_minutes=duration,
        score=score, reasons=reasons, generated_at=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )

async def get_market_snapshot(asset_key: str) -> Tuple[Analysis, List[dict]]:
    cfg=ASSETS[asset_key]
    live_price=await get_price(cfg["symbol"],use_cache=False)
    base=await get_time_series(cfg["symbol"])
    preliminary=analyze_market(asset_key,base,live_price=live_price,interval_minutes=candle_interval_minutes(TD_INTERVAL))
    if preliminary.signal not in ("BUY","SELL"):
        return preliminary,base
    d=preliminary.duration_minutes
    if d <= 15: interval="1min"
    elif d <= 180: interval="5min"
    elif d <= 720: interval="15min"
    else: interval="1h"
    if interval == TD_INTERVAL:
        return preliminary,base
    try:
        candles=await get_time_series_interval(cfg["symbol"],interval,max(300,TD_OUTPUTSIZE))
        final=analyze_market(asset_key,candles,live_price=live_price,interval_minutes=candle_interval_minutes(interval))
        final.reasons.append(f"MTF timeframe: {interval}")
        return final,candles
    except Exception as exc:
        logger.warning("MTF %s fallback to %s: %s",asset_key,TD_INTERVAL,exc)
        preliminary.reasons.append("MTF fallback: base timeframe")
        return preliminary,base

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
        lines += ["", f"المدة التقديرية: {format_duration_minutes(analysis.duration_minutes)} ({analysis.duration_minutes} دقيقة)",
                  f"إعادة التحليل: كل {format_duration_minutes(max(1, round(analysis.duration_minutes * REANALYSIS_PERCENT)))}"]
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
    risk_percent: float = 0.0
    risk_amount: float = 0.0
    position_size: float = 0.0
    rr_tp1: float = 0.0
    rr_tp10: float = 0.0
    realized_pnl: float = 0.0
    realized_r: float = 0.0
    close_price: float = 0.0
    closed_at: str = ""
    remaining_position_size: float = 0.0
    closed_quantity: float = 0.0
    tp_allocations: List[float] = field(default_factory=lambda: [0.10] * 10)
    tp_realized_pnl: Dict[str, float] = field(default_factory=dict)
    break_even_price: float = 0.0
    trailing_stop: float = 0.0
    trailing_active: bool = False
    peak_price: float = 0.0
    expiry_at: str = ""
    expiry_reason: str = "TIME LIMIT"

open_trades: Dict[str, Trade] = {}
trade_lock = asyncio.Lock()

@dataclass
class VideoTradeSetup:
    id: str
    source_url: str
    source_timestamp: str
    asset_key: str
    asset_name: str
    side: str
    entry_price: float
    sl: float
    tps: List[float]
    duration_minutes: int
    confidence: float
    rationale: str
    evidence: str = ""
    completeness: float = 0.0
    rr_tp1: float = 0.0
    rr_tp10: float = 0.0
    rank_score: float = 0.0
    status: str = "EXTRACTED"
    created_at: str = ""

video_trade_setups: Dict[str, VideoTradeSetup] = {}
video_trade_lock = asyncio.Lock()


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def dt_from_string(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)

# ============================================================
# PERSISTENT STORAGE
# ============================================================
# Render's local filesystem is ephemeral. When DATABASE_URL is configured,
# all durable bot state is stored in Postgres. The local JSON/SQLite paths are
# kept as a development/fallback mechanism and are also used for one-time
# migration of old state.

_persistence_initialized = False
_persistence_backend = "local"


def _pg_connect():
    if not DATABASE_URL or psycopg is None:
        return None
    return psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10)


def init_persistence():
    global _persistence_initialized, _persistence_backend
    if _persistence_initialized:
        return
    if DATABASE_URL and psycopg is not None:
        try:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS bot_kv (
                            key TEXT PRIMARY KEY,
                            value JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
            _persistence_backend = "postgres"
            _persistence_initialized = True
            logger.info("Persistent storage: PostgreSQL")
            return
        except Exception:
            logger.exception("PostgreSQL initialization failed; using local fallback")
    elif DATABASE_URL and psycopg is None:
        logger.error("DATABASE_URL is set but psycopg is not installed; using local fallback")

    try:
        with sqlite3.connect(PERSISTENCE_DB_FILE, timeout=10) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS bot_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.commit()
        _persistence_backend = "sqlite"
        _persistence_initialized = True
        logger.warning("Persistent storage: local SQLite fallback (%s); Render persistence requires DATABASE_URL", PERSISTENCE_DB_FILE)
    except Exception:
        logger.exception("Could not initialize local persistence")
        _persistence_backend = "local"
        _persistence_initialized = True


def _persistent_get(key: str):
    init_persistence()
    try:
        if _persistence_backend == "postgres":
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM bot_kv WHERE key=%s", (key,))
                    row = cur.fetchone()
                    return row[0] if row else None
        if _persistence_backend == "sqlite":
            with sqlite3.connect(PERSISTENCE_DB_FILE, timeout=10) as conn:
                row = conn.execute("SELECT value FROM bot_kv WHERE key=?", (key,)).fetchone()
                return json.loads(row[0]) if row else None
    except Exception:
        logger.exception("Persistent read failed for key %s", key)
    return None


def _persistent_set(key: str, value):
    init_persistence()
    try:
        if _persistence_backend == "postgres":
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO bot_kv(key,value,updated_at) VALUES (%s,%s::jsonb,NOW())
                        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                    """, (key, json.dumps(value, ensure_ascii=False)))
            return True
        if _persistence_backend == "sqlite":
            with sqlite3.connect(PERSISTENCE_DB_FILE, timeout=10) as conn:
                conn.execute("""
                    INSERT INTO bot_kv(key,value,updated_at) VALUES (?,?,datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
                """, (key, json.dumps(value, ensure_ascii=False)))
                conn.commit()
            return True
    except Exception:
        logger.exception("Persistent write failed for key %s", key)
    return False


def persistence_status() -> str:
    init_persistence()
    return _persistence_backend


def migrate_legacy_json_to_persistence():
    """Import old JSON files once, without overwriting newer DB state."""
    init_persistence()
    if persistence_status() == "postgres":
        if _persistent_get("trades") is None and os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                if isinstance(legacy, dict):
                    _persistent_set("trades", legacy)
                    logger.info("Migrated %s legacy trades to PostgreSQL", len(legacy))
            except Exception:
                logger.exception("Legacy trade migration failed")
        if _persistent_get("strategies") is None and os.path.exists(STRATEGY_DB_FILE):
            try:
                with open(STRATEGY_DB_FILE, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                if isinstance(legacy, dict):
                    _persistent_set("strategies", legacy)
                    logger.info("Migrated legacy strategies database to PostgreSQL")
            except Exception:
                logger.exception("Legacy strategy migration failed")
        if _persistent_get("news_learning") is None and os.path.exists(NEWS_DB_FILE):
            try:
                with open(NEWS_DB_FILE, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                if isinstance(legacy, dict):
                    _persistent_set("news_learning", legacy)
                    logger.info("Migrated legacy news learning database to PostgreSQL")
            except Exception:
                logger.exception("Legacy news migration failed")


def save_video_trade_setups():
    try:
        data = {k: asdict(v) for k, v in video_trade_setups.items()}
        if _persistent_set("video_trade_setups", data):
            return
        tmp = "video_trade_setups.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, "video_trade_setups.json")
    except Exception:
        logger.exception("Could not save video trade setups")


def load_video_trade_setups():
    global video_trade_setups
    try:
        data = _persistent_get("video_trade_setups")
        if data is None and os.path.exists("video_trade_setups.json"):
            with open("video_trade_setups.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict):
            return
        for sid, raw in data.items():
            if not isinstance(raw, dict):
                continue
            raw.setdefault("source_timestamp", "")
            raw.setdefault("evidence", "")
            raw.setdefault("completeness", 0.0)
            raw.setdefault("rr_tp1", 0.0)
            raw.setdefault("rr_tp10", 0.0)
            raw.setdefault("rank_score", 0.0)
            raw.setdefault("status", "EXTRACTED")
            raw.setdefault("created_at", now_local().isoformat())
            try:
                video_trade_setups[sid] = VideoTradeSetup(**raw)
            except Exception:
                logger.warning("Skipping invalid video trade setup %s", sid)
        logger.info("Loaded %s video trade setups from %s", len(video_trade_setups), persistence_status())
    except Exception:
        logger.exception("Could not load video trade setups")


def save_state():
    try:
        data = {k: asdict(v) for k, v in open_trades.items()}
        if _persistent_set("trades", data):
            return
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        logger.exception("Could not save state")


def load_state():
    try:
        data = _persistent_get("trades")
        if data is None and os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict):
            return
        for tid, raw in data.items():
            if not isinstance(raw, dict):
                continue
            raw.setdefault("hit_tps", [])
            raw.setdefault("notified_tps", [])
            raw.setdefault("last_review_at", "")
            raw.setdefault("management", raw.get("last_action", "OPEN"))
            raw.setdefault("score", 0)
            raw.setdefault("confidence", 0.0)
            raw.setdefault("market_close_at", "")
            raw.setdefault("risk_percent", 0.0)
            raw.setdefault("risk_amount", 0.0)
            raw.setdefault("position_size", 0.0)
            raw.setdefault("rr_tp1", 0.0)
            raw.setdefault("rr_tp10", 0.0)
            raw.setdefault("realized_pnl", 0.0)
            raw.setdefault("realized_r", 0.0)
            raw.setdefault("close_price", 0.0)
            raw.setdefault("closed_at", "")
            raw.setdefault("remaining_position_size", raw.get("position_size", 0.0))
            raw.setdefault("closed_quantity", 0.0)
            raw.setdefault("tp_allocations", [0.10] * 10)
            raw.setdefault("tp_realized_pnl", {})
            raw.setdefault("break_even_price", raw.get("entry_price", 0.0))
            raw.setdefault("trailing_stop", 0.0)
            raw.setdefault("trailing_active", False)
            raw.setdefault("peak_price", raw.get("entry_price", 0.0))
            raw.setdefault("expiry_at", "")
            raw.setdefault("expiry_reason", "TIME LIMIT")
            open_trades[tid] = Trade(**raw)
        logger.info("Loaded %s trades from %s", len(open_trades), persistence_status())
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

def calculate_trade_risk(analysis: Analysis) -> Tuple[float, float, float, float]:
    """Calculate planned dollar risk, virtual position size and R:R values."""
    if analysis.signal not in ("BUY", "SELL") or ACCOUNT_BALANCE <= 0:
        return 0.0, 0.0, 0.0, 0.0
    stop_distance = abs(analysis.price - analysis.sl)
    if stop_distance <= 0:
        return 0.0, 0.0, 0.0, 0.0
    risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE_PCT / 100.0
    position_size = risk_amount / stop_distance
    rr_values = [abs(tp - analysis.price) / stop_distance for tp in analysis.tps]
    return risk_amount, position_size, (rr_values[0] if rr_values else 0.0), (rr_values[-1] if rr_values else 0.0)


def current_open_risk() -> float:
    return sum(max(0.0, float(t.risk_amount)) for t in open_trades.values() if t.status == "OPEN")


def today_realized_loss() -> float:
    today = now_local().date()
    total = 0.0
    for trade in open_trades.values():
        if not trade.closed_at:
            continue
        try:
            if dt_from_string(trade.closed_at).date() == today and trade.realized_pnl < 0:
                total += abs(float(trade.realized_pnl))
        except Exception:
            continue
    return total


def today_open_risk() -> float:
    today = now_local().date()
    total = 0.0
    for trade in open_trades.values():
        if trade.status != "OPEN":
            continue
        try:
            if dt_from_string(trade.opened_at).date() == today:
                total += max(0.0, float(trade.risk_amount))
        except Exception:
            continue
    return total


def daily_risk_exposure() -> float:
    return today_realized_loss() + today_open_risk()


def risk_gate(analysis: Analysis) -> Tuple[bool, str, dict]:
    risk_amount, position_size, rr_tp1, rr_tp10 = calculate_trade_risk(analysis)
    if ACCOUNT_BALANCE <= 0:
        return False, "TRADING_ACCOUNT_BALANCE يجب أن يكون أكبر من صفر.", {}
    if RISK_PER_TRADE_PCT <= 0:
        return False, "RISK_PER_TRADE_PCT يجب أن يكون أكبر من صفر.", {}
    if MIN_RR > 0 and rr_tp1 < MIN_RR:
        return False, f"R:R إلى TP1 = {rr_tp1:.2f} أقل من الحد {MIN_RR:.2f}.", {"risk_amount": risk_amount, "position_size": position_size, "rr_tp1": rr_tp1, "rr_tp10": rr_tp10}
    open_after = current_open_risk() + risk_amount
    max_open = ACCOUNT_BALANCE * MAX_TOTAL_OPEN_RISK_PCT / 100.0
    if MAX_TOTAL_OPEN_RISK_PCT > 0 and open_after > max_open + 1e-9:
        return False, f"مخاطر الصفقات المفتوحة بعد الإضافة {open_after:.2f}$ تتجاوز الحد {MAX_TOTAL_OPEN_RISK_PCT:.2f}% ({max_open:.2f}$).", {"risk_amount": risk_amount, "position_size": position_size, "rr_tp1": rr_tp1, "rr_tp10": rr_tp10}
    daily_after = daily_risk_exposure() + risk_amount
    max_daily = ACCOUNT_BALANCE * DAILY_RISK_LIMIT_PCT / 100.0
    if DAILY_RISK_LIMIT_PCT > 0 and daily_after > max_daily + 1e-9:
        return False, f"التعرض اليومي بعد الإضافة {daily_after:.2f}$ يتجاوز حد اليوم {DAILY_RISK_LIMIT_PCT:.2f}% ({max_daily:.2f}$).", {"risk_amount": risk_amount, "position_size": position_size, "rr_tp1": rr_tp1, "rr_tp10": rr_tp10}
    return True, "OK", {"risk_amount": risk_amount, "position_size": position_size, "rr_tp1": rr_tp1, "rr_tp10": rr_tp10}


def refresh_trade_risk(trade: Trade, analysis: Analysis):
    """Keep dollar risk fixed while recalculating size after an SL adjustment."""
    stop_distance = abs(analysis.price - analysis.sl)
    if stop_distance <= 0:
        return
    if trade.risk_amount <= 0:
        trade.risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE_PCT / 100.0
    trade.risk_percent = RISK_PER_TRADE_PCT
    trade.position_size = trade.risk_amount / stop_distance
    trade.rr_tp1 = abs(analysis.tps[0] - analysis.price) / stop_distance if analysis.tps else 0.0
    trade.rr_tp10 = abs(analysis.tps[-1] - analysis.price) / stop_distance if analysis.tps else 0.0


def _realize_quantity(trade: Trade, price: float, quantity: float, label: str) -> float:
    quantity = max(0.0, min(quantity, trade.remaining_position_size))
    if quantity <= 0:
        return 0.0
    pnl = (price - trade.entry_price) * quantity if trade.side == "BUY" else (trade.entry_price - price) * quantity
    trade.realized_pnl += pnl
    trade.closed_quantity += quantity
    trade.remaining_position_size = max(0.0, trade.remaining_position_size - quantity)
    trade.tp_realized_pnl[label] = trade.tp_realized_pnl.get(label, 0.0) + pnl
    return pnl


def _activate_management(trade: Trade, price: float, atr_value: Optional[float] = None):
    if len(trade.hit_tps) >= BREAK_EVEN_AFTER_TP and trade.break_even_price > 0:
        if trade.side == "BUY":
            trade.sl = max(trade.sl, trade.break_even_price)
        else:
            trade.sl = min(trade.sl, trade.break_even_price)
        trade.management = "BREAK-EVEN"
    if len(trade.hit_tps) >= TRAILING_AFTER_TP and atr_value and atr_value > 0:
        trail = atr_value * TRAILING_ATR_MULT
        if trade.side == "BUY":
            trade.peak_price = max(trade.peak_price, price)
            new_sl = trade.peak_price - trail
            trade.sl = max(trade.sl, new_sl)
            trade.trailing_stop = trade.sl
        else:
            trade.peak_price = min(trade.peak_price, price) if trade.peak_price else price
            new_sl = trade.peak_price + trail
            trade.sl = min(trade.sl, new_sl)
            trade.trailing_stop = trade.sl
        trade.trailing_active = True
        trade.management = "TRAILING"


def mark_trade_closed(trade: Trade, price: float, action: str, status: str = "CLOSED"):
    price = float(price)
    if trade.status == "OPEN" and trade.remaining_position_size > 0:
        _realize_quantity(trade, price, trade.remaining_position_size, status)
    trade.status = status
    trade.management = "CLOSE"
    trade.last_action = action
    trade.close_price = price
    trade.closed_at = now_local().isoformat()
    initial_risk = max(float(trade.risk_amount), 1e-12)
    trade.realized_r = trade.realized_pnl / initial_risk


def create_trade(chat_id: int, analysis: Analysis, risk: Optional[dict] = None) -> Trade:
    now = now_local()
    mins = max(1, round(analysis.duration_minutes * REANALYSIS_PERCENT))
    expiry, expiry_reason = calculate_trade_expiry(now, analysis.duration_minutes, analysis.asset_key)
    market_close = next_market_close(analysis.asset_key, now)
    size = float((risk or {}).get("position_size", 0.0))
    return Trade(
        id=uuid.uuid4().hex[:8].upper(), chat_id=chat_id,
        asset_key=analysis.asset_key, asset_name=analysis.asset_name, symbol=analysis.symbol,
        side=analysis.signal, entry_low=analysis.entry_low, entry_high=analysis.entry_high,
        entry_price=analysis.price, sl=analysis.sl, tps=list(analysis.tps),
        opened_at=now.isoformat(), estimated_duration_minutes=analysis.duration_minutes,
        next_reanalysis_at=(now + timedelta(minutes=mins)).isoformat(),
        market_close_at=market_close.isoformat() if market_close else "",
        status="OPEN", last_price=analysis.price, score=analysis.score, confidence=analysis.confidence,
        risk_percent=RISK_PER_TRADE_PCT, risk_amount=float((risk or {}).get("risk_amount", 0.0)),
        position_size=size, remaining_position_size=size,
        rr_tp1=float((risk or {}).get("rr_tp1", 0.0)), rr_tp10=float((risk or {}).get("rr_tp10", 0.0)),
        tp_allocations=list(TP_ALLOCATION), break_even_price=analysis.price, peak_price=analysis.price,
        expiry_at=expiry.isoformat(), expiry_reason=expiry_reason,
    )


def trade_status_text(trade: Trade) -> str:
    next_time = dt_from_string(trade.next_reanalysis_at).strftime("%Y-%m-%d %H:%M:%S")
    reached = ", ".join(f"TP{x}" for x in trade.hit_tps) if trade.hit_tps else "none"
    return (f"{trade.asset_name} | {trade.side} | {trade.status}\nID: {trade.id}\n"
            f"Entry: {format_price(trade.entry_price)}\nSL: {format_price(trade.sl)}\n"
            f"Last price: {format_price(trade.last_price)}\nRisk: {trade.risk_amount:.2f}$ ({trade.risk_percent:.2f}%)\n"
            f"Initial size: {trade.position_size:.6f} | Remaining: {trade.remaining_position_size:.6f}\n"
            f"Realized PnL: {trade.realized_pnl:.2f}$ | Realized R: {trade.realized_r:.2f}\n"
            f"R:R TP1 / TP10: {trade.rr_tp1:.2f} / {trade.rr_tp10:.2f}\nManagement: {trade.management}\n"
            f"Reached TP: {reached}\nNext reanalysis: {next_time}\n"
            f"Expiry: {trade.expiry_at or '—'} ({trade.expiry_reason})\nLast action: {trade.last_action}")


def trade_expired(trade: Trade, now: Optional[datetime] = None) -> bool:
    if trade.status != "OPEN": return False
    now = now or now_local()
    opened = dt_from_string(trade.opened_at)
    expiry, reason = calculate_trade_expiry(opened, trade.estimated_duration_minutes, trade.asset_key)
    trade.expiry_at, trade.expiry_reason = expiry.isoformat(), reason
    if now >= expiry:
        trade.last_action = f"EXPIRED - {reason}"
        return True
    return False


def evaluate_trade_price(trade: Trade, price: float, atr_value: Optional[float] = None) -> List[str]:
    events=[]; trade.last_price=float(price)
    if trade.status != "OPEN": return events
    if trade.side == "BUY":
        trade.peak_price=max(trade.peak_price, price)
        if price <= trade.sl:
            mark_trade_closed(trade, price, "CLOSE - SL"); return ["SL"]
    else:
        trade.peak_price=min(trade.peak_price or price, price)
        if price >= trade.sl:
            mark_trade_closed(trade, price, "CLOSE - SL"); return ["SL"]
    for i,tp in enumerate(trade.tps,1):
        if i in trade.hit_tps: continue
        hit=(trade.side=="BUY" and price>=tp) or (trade.side=="SELL" and price<=tp)
        if hit:
            trade.hit_tps.append(i); alloc=trade.tp_allocations[i-1] if i-1<len(trade.tp_allocations) else 0.1
            qty=trade.position_size*alloc
            if i==len(trade.tps): qty=trade.remaining_position_size
            pnl=_realize_quantity(trade, price, qty, f"TP{i}")
            events.append(f"TP{i}")
            trade.last_action=f"TP{i} HIT | PnL {pnl:.2f}$"
            _activate_management(trade, price, atr_value)
    if len(trade.hit_tps)>=len(trade.tps) or trade.remaining_position_size <= max(1e-12, trade.position_size*0.001):
        trade.remaining_position_size=0.0
        trade.realized_r=trade.realized_pnl/max(trade.risk_amount,1e-12)
        trade.status="CLOSED"; trade.management="CLOSE"; trade.last_action="CLOSE - FINAL TP"; trade.close_price=price; trade.closed_at=now_local().isoformat(); events.append("FINAL TP")
    return events

# ============================================================
# TRADE REANALYSIS
# ============================================================
async def reanalyze_trade(trade: Trade, reason: str = "scheduled") -> Tuple[Trade, Analysis, List[str]]:
    now = now_local()
    if trade_expired(trade, now):
        mark_trade_closed(trade, trade.last_price, trade.last_action or "EXPIRED - TIME LIMIT", status="EXPIRED")
        trade.last_review_at = now.isoformat()
        save_state()
        # Analysis is only needed by callers that expect it; fetch one current snapshot.
    analysis, _candles = await get_market_snapshot(trade.asset_key)
    events = evaluate_trade_price(trade, analysis.price, analysis.atr_value)
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
        trade.notified_tps = []
        trade.estimated_duration_minutes = max(1, analysis.duration_minutes)
        refresh_trade_risk(trade, analysis)
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
    if not get_twelve_data_api_key():
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
            allowed, risk_reason, risk = risk_gate(analysis)
            if not allowed:
                await safe_send(message, analysis_message(analysis, ai_note) + f"\n\n🛡️ لم تُفتح الصفقة بسبب إدارة المخاطر: {risk_reason}")
                return
            trade = create_trade(message.chat.id, analysis, risk)
            open_trades[trade.id] = trade
            save_state()
        await safe_send(message, analysis_message(analysis, ai_note, trade.id) +
                         f"\n\n🟢 تم تسجيل الصفقة في مدير الصفقات.\n"
                         f"المخاطرة: {trade.risk_amount:.2f}$ ({trade.risk_percent:.2f}%)\n"
                         f"حجم المركز: {trade.position_size:.6f} وحدة\n"
                         f"R:R TP1/TP10: {trade.rr_tp1:.2f}/{trade.rr_tp10:.2f}\n"
                         "المتابعة الحية كل 30 ثانية، وإعادة التحليل كل 10% من المدة.")
    else:
        text = analysis_message(analysis, ai_note)
        if analysis.signal in ("BUY", "SELL"):
            text += "\n\n⚠️ الإشارة موجودة لكن الثقة أقل من حد فتح الصفقة الآلية."
        await safe_send(message, text)

# ============================================================
# BUTTON-BASED TELEGRAM UI
# ============================================================
def trade_priority_key(trade: Trade):
    now = now_local()
    try:
        due = dt_from_string(trade.next_reanalysis_at)
        overdue = 1 if due <= now else 0
        minutes_to_review = (due - now).total_seconds() / 60.0
    except Exception:
        overdue, minutes_to_review = 0, 999999.0
    # Highest priority: overdue reviews, then low confidence, then open risk,
    # then realized R. This is a management priority, not a claim of profit.
    return (overdue, -minutes_to_review, -float(trade.confidence), float(trade.risk_amount), float(trade.realized_r))


async def reanalyze_all_open_trades(reason: str = "manual") -> List[Trade]:
    async with trade_lock:
        trades = sorted([t for t in open_trades.values() if t.status == "OPEN"], key=trade_priority_key, reverse=True)
    results = []
    for trade in trades:
        try:
            updated, _, _ = await reanalyze_trade(trade, reason=reason)
            results.append(updated)
        except Exception as exc:
            trade.last_action = "DATA ERROR - RETRY"
            logger.exception("Batch reanalysis failed for %s: %s", trade.id, exc)
    save_state()
    return sorted(results, key=trade_priority_key, reverse=True)


def main_keyboard():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🥇 Gold",callback_data="asset:gold"),types.InlineKeyboardButton(text="₿ BTC",callback_data="asset:btc")],
        [types.InlineKeyboardButton(text="💶 EUR/USD",callback_data="asset:eurusd"),types.InlineKeyboardButton(text="🥈 Silver",callback_data="asset:silver")],
        [types.InlineKeyboardButton(text="🛢 Oil",callback_data="asset:oil"),types.InlineKeyboardButton(text="Ξ ETH",callback_data="asset:eth")],
        [types.InlineKeyboardButton(text="📋 Open Trades",callback_data="trades"),types.InlineKeyboardButton(text="🎯 Video Trades",callback_data="video_trades")],
        [types.InlineKeyboardButton(text="⚙️ Status",callback_data="status")],
        [types.InlineKeyboardButton(text="🛡 Risk",callback_data="risk"),types.InlineKeyboardButton(text="🧠 Strategies",callback_data="strategies")],
        [types.InlineKeyboardButton(text="🧪 Training",callback_data="training"),types.InlineKeyboardButton(text="🏆 Ranking",callback_data="ranking")],
        [types.InlineKeyboardButton(text="📊 Backtest",callback_data="backtest"),types.InlineKeyboardButton(text="📅 Performance",callback_data="performance")],
        [types.InlineKeyboardButton(text="📰 News",callback_data="news_help"),types.InlineKeyboardButton(text="🎥 Video",callback_data="video_help")],
        [types.InlineKeyboardButton(text="🔄 Refresh",callback_data="menu")],
    ])

def trade_keyboard(trade: Trade):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Reanalyze",callback_data=f"reanalyze:{trade.id}"),types.InlineKeyboardButton(text="❌ Close",callback_data=f"close:{trade.id}")],
        [types.InlineKeyboardButton(text="⬅️ Trades",callback_data="trades"),types.InlineKeyboardButton(text="🏠 Home",callback_data="menu")]
    ])

async def send_menu(chat_id:int):
    await bot.send_message(chat_id,"👑 Trading Bot\n\nتحكم كامل من الأزرار.\n\nاختر العملية:",reply_markup=main_keyboard())

@dp.callback_query(F.data == "menu")
async def cb_menu(call: types.CallbackQuery):
    await call.answer(); await call.message.edit_text("👑 Trading Bot\n\nتحكم كامل من الأزرار.\n\nاختر العملية:",reply_markup=main_keyboard())

@dp.callback_query(F.data.startswith("asset:"))
async def cb_asset(call: types.CallbackQuery):
    await call.answer("جاري التحليل...")
    asset=call.data.split(":",1)[1]
    if asset not in ASSETS: return
    try:
        analysis,_=await get_market_snapshot(asset); ai=await improve_analysis_with_ai(analysis)
        text=analysis_message(analysis,ai)
        buttons=[[types.InlineKeyboardButton(text="🟢 فتح صفقة ورقية",callback_data=f"open:{asset}")]] if analysis.signal in ("BUY","SELL") and analysis.confidence>=MIN_CONFIDENCE_TO_OPEN else []
        buttons += [[types.InlineKeyboardButton(text="🔄 إعادة التحليل",callback_data=f"asset:{asset}")],[types.InlineKeyboardButton(text="⬅️ Home",callback_data="menu")]]
        await call.message.edit_text(text,reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as exc:
        await call.message.edit_text(f"❌ فشل التحليل: {str(exc)[:700]}",reply_markup=main_keyboard())

@dp.callback_query(F.data.startswith("open:"))
async def cb_open(call: types.CallbackQuery):
    await call.answer("فتح الصفقة...")
    asset=call.data.split(":",1)[1]
    if asset not in ASSETS: return
    try:
        analysis,_=await get_market_snapshot(asset)
        if analysis.signal not in ("BUY","SELL") or analysis.confidence<MIN_CONFIDENCE_TO_OPEN:
            await call.message.edit_text("⚠️ لم تعد الإشارة مؤهلة لفتح صفقة.",reply_markup=main_keyboard()); return
        async with trade_lock:
            if any(t.asset_key==asset and t.status=="OPEN" for t in open_trades.values()):
                await call.message.edit_text("⚠️ توجد صفقة مفتوحة لهذا الأصل بالفعل.",reply_markup=main_keyboard()); return
            if sum(t.status=="OPEN" for t in open_trades.values())>=MAX_OPEN_TRADES:
                await call.message.edit_text("⚠️ تم الوصول إلى الحد الأقصى للصفقات المفتوحة.",reply_markup=main_keyboard()); return
            ok,reason,risk=risk_gate(analysis)
            if not ok:
                await call.message.edit_text(f"🛡️ لم تُفتح الصفقة بسبب إدارة المخاطر:\n{reason}",reply_markup=main_keyboard()); return
            trade=create_trade(call.message.chat.id,analysis,risk); open_trades[trade.id]=trade; save_state()
        await call.message.edit_text(trade_status_text(trade),reply_markup=trade_keyboard(trade))
    except Exception as exc:
        await call.message.edit_text(f"❌ فشل فتح الصفقة: {str(exc)[:700]}",reply_markup=main_keyboard())

@dp.callback_query(F.data == "status")
async def cb_status(call: types.CallbackQuery):
    await call.answer(); open_count=sum(t.status=="OPEN" for t in open_trades.values())
    text=(f"⚙️ Status\n\nTelegram: ONLINE\nTwelve Data: {'CONFIGURED' if get_twelve_data_api_key() else 'MISSING'}\nGemini keys: {len(GEMINI_KEYS)}\nOpen trades: {open_count}/{MAX_OPEN_TRADES}\nRisk/trade: {RISK_PER_TRADE_PCT:.2f}%\nOpen risk: {current_open_risk():.2f}$\nDaily exposure: {daily_risk_exposure():.2f}$\nMonitor: {MONITOR_SECONDS}s\nDuration: {MIN_TRADE_DURATION_MINUTES}m → {MAX_TRADE_DURATION_MINUTES}m\nTraining minimum: {STRATEGY_MIN_TRADES} actual trades")
    await call.message.edit_text(text,reply_markup=main_keyboard())

@dp.callback_query(F.data == "risk")
async def cb_risk(call: types.CallbackQuery):
    await call.answer(); await call.message.edit_text(f"🛡 Risk\n\nBalance: {ACCOUNT_BALANCE:.2f}$\nRisk/trade: {RISK_PER_TRADE_PCT:.2f}%\nOpen risk: {current_open_risk():.2f}$ / {ACCOUNT_BALANCE*MAX_TOTAL_OPEN_RISK_PCT/100:.2f}$\nDaily: {daily_risk_exposure():.2f}$ / {ACCOUNT_BALANCE*DAILY_RISK_LIMIT_PCT/100:.2f}$",reply_markup=main_keyboard())

@dp.callback_query(F.data == "trades")
async def cb_trades(call: types.CallbackQuery):
    await call.answer(); trades=[t for t in open_trades.values() if t.status=="OPEN"]
    if not trades: await call.message.edit_text("📋 لا توجد صفقات مفتوحة.",reply_markup=main_keyboard()); return
    trades.sort(key=trade_priority_key, reverse=True)
    kb=[[types.InlineKeyboardButton(text="🔄 Reanalyze ALL",callback_data="reanalyze_all")]]
    for t in trades: kb.append([types.InlineKeyboardButton(text=f"{t.asset_name} {t.side} • {t.id} • {t.confidence:.0f}%",callback_data=f"trade:{t.id}")])
    kb.append([types.InlineKeyboardButton(text="⚠️ Close ALL",callback_data="close_all_confirm")]); kb.append([types.InlineKeyboardButton(text="🏠 Home",callback_data="menu")]); await call.message.edit_text("📋 الصفقات المفتوحة — مرتبة حسب أولوية المتابعة",reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "reanalyze_all")
async def cb_reanalyze_all(call: types.CallbackQuery):
    await call.answer("إعادة تحليل كل الصفقات...")
    try:
        results = await reanalyze_all_open_trades("batch-button")
        if not results:
            await call.message.edit_text("📋 لا توجد صفقات مفتوحة لإعادة تحليلها.", reply_markup=main_keyboard())
            return
        lines = ["🔄 تمت إعادة تحليل كل الصفقات المفتوحة", ""]
        for i, t in enumerate(results, 1):
            lines.append(f"{i}. {t.asset_name} {t.side} | {t.id} | {t.management} | {t.confidence:.0f}% | R {t.realized_r:.2f}")
        await call.message.edit_text("\n".join(lines), reply_markup=main_keyboard())
    except Exception as exc:
        await call.message.edit_text(f"❌ فشل إعادة التحليل الجماعي: {str(exc)[:700]}", reply_markup=main_keyboard())


@dp.callback_query(F.data == "video_trades")
async def cb_video_trades(call: types.CallbackQuery):
    await call.answer()
    rows = sorted(video_trade_setups.values(), key=lambda x: (x.rank_score, x.confidence), reverse=True)[:VIDEO_TRADE_MAX]
    if not rows:
        await call.message.edit_text("🎯 لا توجد صفقات مستخرجة من الفيديوهات بعد.", reply_markup=main_keyboard())
        return
    await call.message.edit_text(video_trade_summary(rows, VIDEO_TRADE_MAX), reply_markup=main_keyboard())


@dp.callback_query(F.data.startswith("trade:"))
async def cb_trade(call: types.CallbackQuery):
    await call.answer(); tid=call.data.split(":",1)[1]; t=open_trades.get(tid)
    if not t: await call.message.edit_text("❌ الصفقة غير موجودة",reply_markup=main_keyboard()); return
    await call.message.edit_text(trade_status_text(t),reply_markup=trade_keyboard(t))

@dp.callback_query(F.data.startswith("reanalyze:"))
async def cb_reanalyze(call: types.CallbackQuery):
    await call.answer("إعادة التحليل..."); tid=call.data.split(":",1)[1]; t=open_trades.get(tid)
    if not t or t.status!="OPEN": await call.message.edit_text("❌ الصفقة غير مفتوحة",reply_markup=main_keyboard()); return
    try:
        t,a,e=await reanalyze_trade(t,"button"); await call.message.edit_text(trade_status_text(t)+"\n\nEvents: "+(", ".join(e) or "none"),reply_markup=trade_keyboard(t) if t.status=="OPEN" else main_keyboard())
    except Exception as exc: await call.message.edit_text(f"❌ {str(exc)[:700]}",reply_markup=main_keyboard())

@dp.callback_query(F.data.startswith("close:"))
async def cb_close(call: types.CallbackQuery):
    await call.answer(); tid=call.data.split(":",1)[1]; t=open_trades.get(tid)
    if not t or t.status!="OPEN": await call.message.edit_text("❌ الصفقة غير مفتوحة",reply_markup=main_keyboard()); return
    mark_trade_closed(t,t.last_price,"CLOSE - USER"); save_state(); await call.message.edit_text(trade_status_text(t),reply_markup=main_keyboard())

@dp.callback_query(F.data == "strategies")
async def cb_strategies(call: types.CallbackQuery):
    await call.answer(); rows=top_strategies(10)
    if not rows:
        await call.message.edit_text("🧠 لا توجد استراتيجيات مدربة بعد.\n\nأرسل رابط فيديو للاستراتيجية.",reply_markup=main_keyboard()); return
    text="🏆 أفضل الاستراتيجيات\n\n"; kb=[]
    for i,(st,r) in enumerate(rows,1):
        text+=f"{i}. {st.name}\nScore {r.score} | OOS {r.oos_score} | WR {r.win_rate}% | Trades {r.trades} | {r.verdict}\n\n"
        kb.append([types.InlineKeyboardButton(text=f"🔎 {i}. {st.name[:28]}",callback_data=f"strategy:{st.id}"),types.InlineKeyboardButton(text="🔄 Retrain",callback_data=f"retrain:{st.id}")])
    kb.append([types.InlineKeyboardButton(text="🏠 Home",callback_data="menu")]); await call.message.edit_text(text,reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("strategy:"))
async def cb_strategy_detail(call: types.CallbackQuery):
    await call.answer(); sid=call.data.split(":",1)[1]; st=strategy_from_db(sid)
    if not st: await call.message.edit_text("❌ الاستراتيجية غير موجودة",reply_markup=main_keyboard()); return
    kb=[[types.InlineKeyboardButton(text="🔄 Retrain",callback_data=f"retrain:{sid}")],[types.InlineKeyboardButton(text="⬅️ Ranking",callback_data="ranking")],[types.InlineKeyboardButton(text="🏠 Home",callback_data="menu")]]
    await call.message.edit_text(strategy_display(st,result_from_db(sid)),reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("retrain:"))
async def cb_retrain(call: types.CallbackQuery):
    await call.answer("بدء إعادة التدريب...")
    sid=call.data.split(":",1)[1]; st=strategy_from_db(sid)
    if not st: await call.message.edit_text("❌ الاستراتيجية غير موجودة",reply_markup=main_keyboard()); return
    if training_status["running"]: await call.message.edit_text("⚠️ يوجد تدريب جارٍ بالفعل.",reply_markup=main_keyboard()); return
    await call.message.edit_text(f"🧪 إعادة تدريب {st.name}...\nسيتم اختبار ≥{STRATEGY_MIN_TRADES} صفقة فعلية في منطقة التدريب ثم OOS.")
    try:
        r=await train_strategy(st); await call.message.edit_text(strategy_display(st,r),reply_markup=main_keyboard())
    except Exception as exc: await call.message.edit_text(f"❌ فشل التدريب: {str(exc)[:700]}",reply_markup=main_keyboard())

@dp.callback_query(F.data == "training")
async def cb_training(call: types.CallbackQuery):
    await call.answer(); await call.message.edit_text(f"🧪 Training\n\nRunning: {training_status['running']}\nStrategy: {training_status['strategy_name'] or '—'}\nTests: {training_status['tests']}\nActual trades: {training_status['trades']}\nMinimum required: {STRATEGY_MIN_TRADES}\nStatus: {training_status['message']}",reply_markup=main_keyboard())

@dp.callback_query(F.data == "ranking")
async def cb_ranking(call: types.CallbackQuery):
    await cb_strategies(call)

@dp.callback_query(F.data == "backtest")
async def cb_backtest(call: types.CallbackQuery):
    await call.answer("جاري Backtest...")
    if not get_twelve_data_api_key(): await call.message.edit_text("❌ Twelve Data غير مهيأ.",reply_markup=main_keyboard()); return
    lines=["📊 Backtest"]
    for _,cfg in ASSETS.items():
        try:
            r=backtest_ema_rsi(await get_time_series(cfg["symbol"],500)); lines.append(f"\n{cfg['name']}\nTrades {r['trades']} | WR {r['win_rate']}% | Net R {r['net_r']} | DD {r['max_drawdown_r']}")
        except Exception as exc: lines.append(f"\n{cfg['name']}: ERROR {str(exc)[:120]}")
    lines.append("\n⚠️ تاريخي وليس ضمانًا للأداء المستقبلي."); await call.message.edit_text("".join(lines),reply_markup=main_keyboard())

@dp.callback_query(F.data == "performance")
async def cb_performance(call: types.CallbackQuery):
    await call.answer(); closed=[t for t in open_trades.values() if t.status!="OPEN"]
    wins=sum(t.realized_pnl>0 for t in closed); losses=sum(t.realized_pnl<0 for t in closed); pnl=sum(t.realized_pnl for t in closed)
    await call.message.edit_text(f"📅 Performance\n\nClosed trades: {len(closed)}\nWins: {wins}\nLosses: {losses}\nWin rate: {(wins/len(closed)*100 if closed else 0):.2f}%\nRealized PnL: {pnl:.2f}$\nOpen trades: {sum(t.status=='OPEN' for t in open_trades.values())}",reply_markup=main_keyboard())

@dp.callback_query(F.data == "close_all_confirm")
async def cb_close_all_confirm(call: types.CallbackQuery):
    await call.answer(); await call.message.edit_text("⚠️ هل تريد إغلاق جميع الصفقات المفتوحة؟",reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="❌ نعم، أغلق الكل",callback_data="close_all")],[types.InlineKeyboardButton(text="↩️ إلغاء",callback_data="trades")]]))

@dp.callback_query(F.data == "close_all")
async def cb_close_all(call: types.CallbackQuery):
    await call.answer(); count=0
    for t in open_trades.values():
        if t.status=="OPEN": mark_trade_closed(t,t.last_price,"CLOSE ALL - USER"); count+=1
    save_state(); await call.message.edit_text(f"تم إغلاق {count} صفقة.",reply_markup=main_keyboard())

@dp.callback_query(F.data == "news_help")
async def cb_news(call: types.CallbackQuery):
    await call.answer(); await call.message.edit_text("📰 تحليل الأخبار\n\nأرسل خبرًا أو أعد توجيهه إلى البوت. سيتم تحليل الأصول والاتجاه والقوة والأفق، ثم حفظ التوقع وقياس النتيجة لاحقًا للتعلم.",reply_markup=main_keyboard())

@dp.callback_query(F.data == "video_help")
async def cb_video(call: types.CallbackQuery):
    await call.answer(); await call.message.edit_text("🎥 تحليل الاستراتيجيات من الفيديو\n\nأرسل رابط YouTube/Vimeo/Dailymotion/TikTok/Instagram/Facebook. إذا لم توجد ترجمة، سيُنزل البوت الفيديو ويحلله بصريًا وصوتيًا عبر Gemini لاستخراج الشموع والمؤشرات وقواعد الدخول/الخروج، ثم يحولها إلى استراتيجية قابلة للاختبار والتدريب ≥70 صفقة فعلية + OOS + ترتيب.",reply_markup=main_keyboard())

# ============================================================
# COMMANDS
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👑 مرحبًا بك في Trading Bot\n\nتحكم كامل عبر الأزرار:", reply_markup=main_keyboard())

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    open_count = len([t for t in open_trades.values() if t.status == "OPEN"])
    await safe_send(message,
        "حالة Trading Bot\n\n"
        "Telegram: ONLINE\n"
        f"Twelve Data: {'CONFIGURED' if get_twelve_data_api_key() else 'MISSING API KEY'}\n"
        f"Gemini keys: {len(GEMINI_KEYS)}\n"
        f"Gemini models: {', '.join(GEMINI_MODELS)}\n"
        f"Open trades: {open_count} / {MAX_OPEN_TRADES}\n"
        f"Risk: {RISK_PER_TRADE_PCT:.2f}%/trade | Open {current_open_risk():.2f}$ / {ACCOUNT_BALANCE * MAX_TOTAL_OPEN_RISK_PCT / 100.0:.2f}$\n"
        f"Daily risk: {daily_risk_exposure():.2f}$ / {ACCOUNT_BALANCE * DAILY_RISK_LIMIT_PCT / 100.0:.2f}$\n"
        f"Monitor: {MONITOR_SECONDS}s\n"
        "Timezone: Africa/Algiers\n"
        f"Market interval: {TD_INTERVAL}\n"
        f"Market candles: {TD_OUTPUTSIZE}\n"
        f"State file: {STATE_FILE}"
    )

@dp.message(Command("risk"))
async def cmd_risk(message: types.Message):
    open_risk = current_open_risk()
    max_open = ACCOUNT_BALANCE * MAX_TOTAL_OPEN_RISK_PCT / 100.0
    daily = daily_risk_exposure()
    max_daily = ACCOUNT_BALANCE * DAILY_RISK_LIMIT_PCT / 100.0
    await safe_send(message,
        "🛡️ إدارة المخاطر\n\n"
        f"الحساب الافتراضي: {ACCOUNT_BALANCE:.2f}$\n"
        f"مخاطرة الصفقة: {RISK_PER_TRADE_PCT:.2f}% = {ACCOUNT_BALANCE * RISK_PER_TRADE_PCT / 100.0:.2f}$\n"
        f"مخاطر الصفقات المفتوحة: {open_risk:.2f}$ / {max_open:.2f}$\n"
        f"التعرض اليومي: {daily:.2f}$ / {max_daily:.2f}$\n"
        f"الحد الأدنى R:R إلى TP1: {MIN_RR:.2f}\n\n"
        "حجم المركز المعروض وحدات افتراضية للأصول المسعّرة بالدولار، وليس lot size خاصًا بوسيط."
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
        await safe_send(message, "الاستخدام: /reanalyze TRADE_ID أو /reanalyze ALL")
        return
    tid = parts[1].strip().upper()
    if tid == "ALL":
        results = await reanalyze_all_open_trades("manual-all")
        if not results:
            await safe_send(message, "📋 لا توجد صفقات مفتوحة لإعادة تحليلها.")
            return
        await safe_send(message, "🔄 إعادة تحليل كل الصفقات\n\n" + "\n".join(f"{i}. {t.asset_name} {t.side} | {t.id} | {t.management} | {t.confidence:.0f}%" for i,t in enumerate(results,1)))
        return
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
                    mark_trade_closed(t, t.last_price, "CLOSE - USER"); closed += 1
        else:
            t = open_trades.get(target)
            if t and t.status == "OPEN":
                mark_trade_closed(t, t.last_price, "CLOSE - USER"); closed = 1
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
    if len(candles) < 160:
        return {"trades":0,"wins":0,"losses":0,"win_rate":0,"net_r":0,"max_drawdown_r":0,"expectancy_r":0,"note":"Not enough historical candles."}
    r_values=[]; i=60; stride=1
    while i < len(candles)-15:
        c=[x["close"] for x in candles[:i+1]]; h=[x["high"] for x in candles[:i+1]]; l=[x["low"] for x in candles[:i+1]]
        e20,e50,rr,aa=ema(c,20),ema(c,50),rsi(c,14),atr(h,l,c,14)
        if None in (e20,e50,rr,aa) or aa<=0: i+=stride; continue
        price=c[-1]; side="BUY" if price>e20>e50 and rr>55 else "SELL" if price<e20<e50 and rr<45 else None
        if not side: i+=stride; continue
        sl=price-aa*1.2 if side=="BUY" else price+aa*1.2
        tps=[price+aa*x for x in (0.8,1.2,1.6,2,2.4,2.8,3.2,3.6,4,4.5)] if side=="BUY" else [price-aa*x for x in (0.8,1.2,1.6,2,2.4,2.8,3.2,3.6,4,4.5)]
        remaining=1.0; realized=0.0; hit=set(); peak=price; trail=None; result=None
        for j in range(i+1,min(len(candles),i+1+max(10,int(analysis_duration_for_backtest(aa))))):
            bar=candles[j]
            # Conservative intrabar ordering: SL before TP.
            if side=="BUY":
                peak=max(peak,bar["high"])
                active_sl=max(sl,trail) if trail is not None else sl
                if bar["low"]<=active_sl: result=realized-remaining; break
            else:
                peak=min(peak,bar["low"])
                active_sl=min(sl,trail) if trail is not None else sl
                if bar["high"]>=active_sl: result=realized-remaining; break
            for n,tp in enumerate(tps,1):
                if n in hit: continue
                touched=(bar["high"]>=tp if side=="BUY" else bar["low"]<=tp)
                if touched:
                    alloc=TP_ALLOCATION[n-1] if n-1<len(TP_ALLOCATION) else .1
                    alloc=min(alloc,remaining); r=(abs(tp-price)/(aa*1.2))*alloc
                    realized += r; remaining=max(0.0,remaining-alloc); hit.add(n)
                    if n>=BREAK_EVEN_AFTER_TP: sl=max(sl,price) if side=="BUY" else min(sl,price)
                    if n>=TRAILING_AFTER_TP:
                        dist=aa*TRAILING_ATR_MULT; trail=(peak-dist if side=="BUY" else peak+dist)
                        sl=max(sl,trail) if side=="BUY" else min(sl,trail)
                    if n==10 or remaining<=1e-9: result=realized; remaining=0; break
            if result is not None: break
        if result is None: result=realized-remaining
        r_values.append(result); i += 10
    m=_metrics_for_values(r_values)
    return {"trades":m["trades"],"wins":m["wins"],"losses":m["losses"],"win_rate":round(m["win_rate"],2),"net_r":round(m["net_r"],3),"max_drawdown_r":round(m["dd"],3),"expectancy_r":round(m["expectancy"],4),"note":"Walk-forward paper simulation with TP1-TP10, partial exits, break-even and trailing; conservative SL-first on ambiguous OHLC bars."}

def analysis_duration_for_backtest(atr_value: float) -> int:
    # Fixed 10-bar minimum for the EMA/RSI benchmark; strategy-specific tests use max_hold_bars.
    return 10

@dp.message(Command("auto_backtest"))
async def cmd_auto_backtest(message: types.Message):
    if not get_twelve_data_api_key():
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
    if not get_twelve_data_api_key():
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
    training_trades: int = 0
    oos_tests: int = 0
    oos_trades: int = 0
    oos_win_rate: float = 0.0
    oos_net_r: float = 0.0
    oos_expectancy_r: float = 0.0
    oos_score: float = 0.0


strategies_db: Dict[str, dict] = {"strategies": {}, "results": {}}
strategy_db_lock = asyncio.Lock()


def load_strategies():
    global strategies_db
    try:
        data = _persistent_get("strategies")
        if data is None and os.path.exists(STRATEGY_DB_FILE):
            with open(STRATEGY_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        if isinstance(data, dict):
            strategies_db = {"strategies": data.get("strategies", {}), "results": data.get("results", {})}
        logger.info("Loaded %s learned strategies from %s", len(strategies_db["strategies"]), persistence_status())
    except Exception:
        logger.exception("Could not load strategy database")


def save_strategies():
    try:
        if _persistent_set("strategies", strategies_db):
            return
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
    Extract available subtitles when the platform exposes them.
    This is only the fast text path; when subtitles are missing,
    process_strategy_video() falls back to Gemini visual/audio video understanding.
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


def _download_video_for_ai(url: str, tmp_dir: str) -> Optional[str]:
    """Download a public video with yt-dlp for Gemini multimodal analysis.

    We deliberately avoid requiring subtitles: the downloaded media contains the
    visual frames and audio track, which Gemini can inspect directly.
    """
    output_template = str(Path(tmp_dir) / "video.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings", "--quiet",
        "--no-playlist",
        "--max-filesize", os.getenv("VIDEO_MAX_DOWNLOAD", "500M"),
        "-f", "best[ext=mp4]/best",
        "-o", output_template,
        url,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=int(os.getenv("VIDEO_DOWNLOAD_TIMEOUT", "600"))
        )
    except Exception as exc:
        logger.warning("Video download failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning("yt-dlp video download failed: %s", (proc.stderr or "")[-1000:])
        return None
    files = [x for x in Path(tmp_dir).glob("video.*") if x.is_file()]
    if not files:
        return None
    return str(files[0])


VIDEO_LAST_ERROR = ""

def _record_video_error(exc: Exception):
    global VIDEO_LAST_ERROR
    VIDEO_LAST_ERROR = str(exc)[:1200]


def _gemini_video_url_analyze_sync(url: str, prompt: str, key: str, models: List[str]) -> str:
    """Analyze a public YouTube video directly through Gemini.

    Uses the current Interactions video input first, then the documented
    generate_content/file_data form as a compatibility fallback. This avoids
    making YouTube downloading a hard dependency for public YouTube videos.
    """
    if genai is None or not key:
        return ""
    client = genai.Client(api_key=key)
    last_error = ""
    for model in models:
        # Current Interactions API.
        try:
            interaction = client.interactions.create(
                model=model,
                input=[
                    {"type": "video", "uri": url, **({"processing": "agentic"} if VIDEO_AGENTIC else {})},
                    {"type": "text", "text": prompt},
                ],
            )
            text = getattr(interaction, "output_text", None)
            if text:
                return str(text).strip()
        except Exception as exc:
            last_error = str(exc)
            _record_video_error(exc)

        # Compatibility fallback documented for the Generate Content API.
        try:
            from google.genai import types
            response = client.models.generate_content(
                model=model,
                contents=types.Content(parts=[
                    types.Part(file_data=types.FileData(file_uri=url)),
                    types.Part(text=prompt),
                ]),
            )
            text = getattr(response, "text", None)
            if text:
                return str(text).strip()
        except Exception as exc:
            last_error = str(exc)
            _record_video_error(exc)

        low = last_error.lower()
        if any(x in low for x in ("not found", "404", "unsupported", "does not exist", "invalid model")):
            continue

    if last_error:
        raise RuntimeError(last_error)
    return ""


def _gemini_video_analyze_sync(video_path: str, prompt: str, key: str, models: List[str]) -> str:
    """Synchronous Gemini Files API video analysis, executed in a worker thread."""
    if genai is None or not key:
        return ""
    client = genai.Client(api_key=key)
    uploaded = client.files.upload(file=video_path)
    # Video files may need server-side processing before they can be queried.
    deadline = time.monotonic() + float(os.getenv("VIDEO_PROCESSING_TIMEOUT", "900"))
    while True:
        state = getattr(uploaded, "state", None)
        state_name = getattr(state, "name", str(state or "")).upper()
        if state_name == "ACTIVE":
            break
        if state_name in {"FAILED", "ERROR"}:
            raise RuntimeError(f"Gemini video processing failed: {state_name}")
        if time.monotonic() >= deadline:
            raise TimeoutError("Gemini video processing timeout")
        time.sleep(3)
        uploaded = client.files.get(name=uploaded.name)

    last_error = ""
    for model in models:
        try:
            # Current Gemini video-understanding API.
            interaction = client.interactions.create(
                model=model,
                input=[
                    {
                        "type": "video",
                        "uri": uploaded.uri,
                        "mime_type": getattr(uploaded, "mime_type", "video/mp4"),
                        **({"processing": "agentic"} if VIDEO_AGENTIC else {}),
                    },
                    {"type": "text", "text": prompt},
                ],
            )
            text = getattr(interaction, "output_text", None)
            if text:
                return str(text).strip()
        except Exception as exc:
            last_error = str(exc)
            low = last_error.lower()
            if any(x in low for x in ("not found", "404", "unsupported", "does not exist", "invalid model")):
                continue
            raise
    if last_error:
        raise RuntimeError(last_error)
    return ""


def _video_analysis_prompt() -> str:
    return """
أنت محلل فيديو متخصص في استخراج استراتيجيات التداول. حلّل الفيديو نفسه بصريًا وزمنيًا
وصوتيًا، ولا تعتمد على وجود ترجمة. إذا كان هناك كلام مسموع فاستخرج مضمونه المفيد،
وإذا ظهرت شاشات تداول/شموع/مؤشرات/رسوم فحللها بصريًا. ابحث تحديدًا عن:

1) اسم أو فكرة الاستراتيجية.
2) شروط BUY وشروط SELL كما شرحها صاحب الفيديو.
3) المؤشرات والقيم/المستويات المستخدمة.
4) شروط الدخول والخروج، وقف الخسارة، جني الأرباح، وإدارة الصفقة.
5) إدارة المخاطر وحجم المركز والمدة إن ذكرت.
6) أي قواعد تظهر على الشاشة حتى لو لم تُنطق.
7) التوقيت التقريبي (timestamp) للمقاطع التي تثبت كل قاعدة.
8) فرّق بوضوح بين ما شاهدته/سمعته فعلاً وبين ما لا يمكن قراءته. لا تخترع أرقامًا أو قواعد.

أعد تقريرًا منظمًا بالعربية، ثم في النهاية JSON صالح بالمفاتيح:
strategy_name, description, indicators, buy_rules, sell_rules, sl_atr, tp_atr, max_hold_bars,
evidence_timestamps, confidence,
trade_setups: [
  {
    "timestamp": "MM:SS", "asset": "gold|btc|eurusd|silver|oil|eth",
    "side": "BUY|SELL", "entry": null, "sl": null,
    "tp1": null, "tp2": null, "tp3": null, "tp4": null, "tp5": null,
    "tp6": null, "tp7": null, "tp8": null, "tp9": null, "tp10": null,
    "duration_minutes": null, "confidence": 0,
    "rationale": "", "evidence": ""
  }
].
في trade_setups أدرج فقط الصفقات التي شاهدت أو سمعت تفاصيلها فعلاً.
إذا لم يذكر الفيديو سعر الدخول/SL/TP، اتركه null ولا تخمّن.
إذا ظهرت صفقة متعددة الأهداف، احتفظ بكل الأهداف التي أمكن قراءتها.
"""


async def analyze_video_visually(url: str) -> str:
    """
    Full multimodal fallback for videos without subtitles.

    For YouTube, Gemini can consume the public URL directly, which is the most
    reliable path on Render. Other supported social platforms fall back to a
    yt-dlp download and then Gemini Files API.
    """
    if genai is None or not GEMINI_KEYS:
        return ""

    prompt = _video_analysis_prompt()
    youtube = any(host in url.lower() for host in ("youtube.com", "youtu.be", "youtube-nocookie.com"))

    async def try_keyed_call(callable_fn):
        for _ in range(max(1, len(GEMINI_KEYS))):
            key = await key_manager.next_key()
            if not key:
                return ""
            try:
                return await asyncio.to_thread(callable_fn, key)
            except Exception as exc:
                _record_video_error(exc)
                low = str(exc).lower()
                if any(x in low for x in ("429", "quota", "rate limit", "resource exhausted", "too many requests")):
                    key_manager.cooldown(key, 60)
                    continue
                logger.warning("Gemini video analysis failed: %s", str(exc)[:1000])
                key_manager.cooldown(key, 30)
        return ""

    # Preferred path: no video download at all. Gemini fetches the public YouTube URL.
    if youtube:
        result = await try_keyed_call(
            lambda key: _gemini_video_url_analyze_sync(url, prompt, key, GEMINI_MODELS)
        )
        if result:
            return result

    # Fallback for YouTube failures and for Vimeo/Dailymotion/TikTok/etc.
    with tempfile.TemporaryDirectory(prefix="strategy_video_media_") as tmp:
        video_path = await asyncio.to_thread(_download_video_for_ai, url, tmp)
        if not video_path:
            return ""
        return await try_keyed_call(
            lambda key: _gemini_video_analyze_sync(video_path, prompt, key, GEMINI_MODELS)
        )


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


def _metrics_for_values(values: List[float]) -> dict:
    trades=[r for r in values if r!=0.0]; wins=[r for r in trades if r>0]; losses=[r for r in trades if r<0]
    gp=sum(wins); gl=abs(sum(losses)); pf=gp/gl if gl>0 else (99.0 if gp>0 else 0.0)
    wr=len(wins)/len(trades)*100 if trades else 0.0; net=sum(trades); exp=net/len(trades) if trades else 0.0
    eq=peak=dd=0.0
    for r in values:
        eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq)
    return {"trades":len(trades),"wins":len(wins),"losses":len(losses),"win_rate":wr,"pf":pf,"net_r":net,"expectancy":exp,"dd":dd}


def calculate_strategy_metrics(strategy_id: str, r_values: List[float], tests: int, assets_tested: int, oos_values: Optional[List[float]]=None) -> StrategyResult:
    m=_metrics_for_values(r_values); o=_metrics_for_values(oos_values or [])
    sample=min(1.0,m["trades"]/max(70.0,STRATEGY_MIN_TRADES)); pf_factor=min(1.0,max(m["pf"],0.0)/2.0); wr_factor=min(1.0,m["win_rate"]/70.0)
    dd_factor=max(0.0,1.0-m["dd"]/max(5.0,abs(m["net_r"])+5.0)); exp_factor=min(1.0,max(0.0,m["expectancy"])/0.6)
    oos_quality=max(0.0,min(1.0,(o["net_r"]+5.0)/20.0)) if o["trades"] else 0.0
    score=100*(.22*pf_factor+.18*wr_factor+.18*max(0,min(1,(m["net_r"]+10)/40))+.12*dd_factor+.10*exp_factor+.10*sample+.10*oos_quality)
    robustness=100*(.55*sample+.25*min(1,assets_tested/max(1,len(ASSETS)))+.20*(1.0 if o["trades"]>=OOS_MIN_TRADES else 0.0))
    if m["trades"] < STRATEGY_MIN_TRADES: verdict="INSUFFICIENT DATA"
    elif o["trades"] < OOS_MIN_TRADES or o["net_r"] <= 0: verdict="OOS FAILED"
    elif score>=80 and m["pf"]>=1.5 and m["net_r"]>0: verdict="STRONG"
    elif score>=65 and m["pf"]>=1.15 and m["net_r"]>0: verdict="PROMISING"
    elif score>=50: verdict="WEAK"
    else: verdict="FAILED"
    return StrategyResult(strategy_id=strategy_id,tests=tests,trades=m["trades"],wins=m["wins"],losses=m["losses"],neutral=max(0,tests-m["trades"]),win_rate=round(m["win_rate"],2),profit_factor=round(m["pf"],3),net_r=round(m["net_r"],3),max_drawdown_r=round(m["dd"],3),expectancy_r=round(m["expectancy"],4),score=round(score,2),robustness=round(robustness,2),verdict=verdict,trained_at=now_local().isoformat(),assets_tested=assets_tested,r_values=[round(x,6) for x in r_values],training_trades=m["trades"],oos_tests=len(oos_values or []),oos_trades=o["trades"],oos_win_rate=round(o["win_rate"],2),oos_net_r=round(o["net_r"],3),oos_expectancy_r=round(o["expectancy"],4),oos_score=round(max(0,min(100,50+o["expectancy"]*40+o["win_rate"]*.2)),2))


async def train_strategy(strategy: StrategyDefinition) -> StrategyResult:
    async with STRATEGY_TRAINING_LOCK:
        training_status.update({"running":True,"strategy_id":strategy.id,"strategy_name":strategy.name,"tests":0,"trades":0,"message":"loading historical candles"})
        train_values=[]; oos_values=[]; assets_tested=0
        try:
            for asset_key,cfg in ASSETS.items():
                try:
                    candles=await get_time_series(cfg["symbol"], max(800, TD_OUTPUTSIZE))
                    first=60; last=len(candles)-strategy.max_hold_bars-2
                    if last<=first+5: continue
                    # 80/20 chronological split; only training partition contributes to training result.
                    split=first+int((last-first+1)*0.80)
                    train_indices=list(range(first,split)); oos_indices=list(range(split,last+1))
                    # Sample up to 120 training candidates per asset, but continue across assets until >=70 actual trades.
                    step=max(1,len(train_indices)//120)
                    for idx in train_indices[::step]:
                        r=simulate_strategy_test(strategy,candles,idx); train_values.append(r)
                        training_status["tests"]+=1; training_status["trades"]=sum(x!=0 for x in train_values); training_status["message"]=f"training {asset_key}"
                    # OOS capped at 120 candidates/asset.
                    step2=max(1,len(oos_indices)//120)
                    for idx in oos_indices[::step2]: oos_values.append(simulate_strategy_test(strategy,candles,idx))
                    assets_tested+=1
                except Exception as exc: logger.warning("Strategy training failed on %s: %s",asset_key,exc)
            # If <70 actual trades, mark insufficient rather than pretending tests are trades.
            result=calculate_strategy_metrics(strategy.id,train_values, len(train_values), assets_tested, oos_values)
            if result.trades < STRATEGY_MIN_TRADES: result.verdict="INSUFFICIENT DATA"
            strategies_db["results"][strategy.id]=asdict(result); strategies_db["strategies"][strategy.id]=asdict(strategy); save_strategies()
            return result
        finally:
            training_status["running"]=False; training_status["message"]="complete"


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
    raw = dict(raw)
    defaults = {"assets_tested":0,"r_values":[],"training_trades":raw.get("trades",0),"oos_tests":0,"oos_trades":0,"oos_win_rate":0.0,"oos_net_r":0.0,"oos_expectancy_r":0.0,"oos_score":0.0}
    for k,v in defaults.items(): raw.setdefault(k,v)
    try:
        return StrategyResult(**raw)
    except Exception:
        logger.exception("Invalid strategy result %s", strategy_id)
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
            f"OOS: {result.oos_trades}/{result.oos_tests} trades | WR {result.oos_win_rate}% | Net R {result.oos_net_r} | Score {result.oos_score}",
            f"Trained: {result.trained_at}",
        ]
    return "\n".join(lines)


def _video_asset_key(value: str) -> Optional[str]:
    text = str(value or "").lower().strip()
    aliases = {
        "gold": "gold", "xau": "gold", "xau/usd": "gold", "ذهب": "gold",
        "btc": "btc", "bitcoin": "btc", "btc/usd": "btc",
        "eurusd": "eurusd", "eur/usd": "eurusd", "euro": "eurusd",
        "silver": "silver", "xag": "silver", "xag/usd": "silver",
        "oil": "oil", "wti": "oil", "crude": "oil",
        "eth": "eth", "ethereum": "eth", "eth/usd": "eth",
    }
    return aliases.get(text)


def _safe_float(value, default=0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _rank_video_trade_setup(setup: VideoTradeSetup) -> float:
    score = 0.0
    score += min(30.0, max(0.0, setup.confidence) * 0.30)
    score += min(30.0, max(0.0, setup.completeness) * 0.30)
    if setup.rr_tp1 > 0:
        score += min(15.0, setup.rr_tp1 * 6.0)
    if setup.rr_tp10 > 0:
        score += min(15.0, setup.rr_tp10 * 3.0)
    if setup.source_timestamp:
        score += 5.0
    if setup.rationale:
        score += 5.0
    return round(min(100.0, score), 2)


async def extract_video_trade_setups(source_url: str, source_text: str) -> List[VideoTradeSetup]:
    # First reuse the structured JSON already returned by the multimodal video
    # analysis. This avoids a second Gemini call and prevents losing trades when
    # the abstract strategy parser rejects the video. If no usable JSON exists,
    # ask Gemini's text model to extract the setups from the visual-analysis text.
    embedded = extract_json_from_ai(source_text) or {}
    rows = embedded.get("trade_setups") if isinstance(embedded.get("trade_setups"), list) else embedded.get("trades")
    if not isinstance(rows, list):
        rows = None

    prompt = f"""
استخرج صفقات التداول التي قُدمت فعليًا داخل محتوى الفيديو/التحليل التالي.
لا تنشئ صفقة جديدة من عندك ولا تحوّل مجرد رأي عام إلى صفقة.
لكل صفقة أعطِ timestamp إن توفر، الأصل، BUY/SELL، الدخول، SL، TP1..TP10، المدة، الثقة، والسبب.
الأرقام غير الواضحة يجب أن تكون null. لا تملأ أرقامًا بالتخمين.
أعد JSON فقط بالشكل:
{{
  "trades": [{{
    "timestamp":"MM:SS", "asset":"gold|btc|eurusd|silver|oil|eth",
    "side":"BUY|SELL", "entry":null, "sl":null,
    "tp1":null,"tp2":null,"tp3":null,"tp4":null,"tp5":null,
    "tp6":null,"tp7":null,"tp8":null,"tp9":null,"tp10":null,
    "duration_minutes":null,"confidence":0,"rationale":"","evidence":""
  }}]
}}

المحتوى:
{source_text[:50000]}
"""
    if rows is None:
        ai = await safe_ai_generate(prompt)
        obj = extract_json_from_ai(ai) or {}
        rows = obj.get("trades") if isinstance(obj.get("trades"), list) else obj.get("trade_setups")
        if not isinstance(rows, list):
            rows = []
    result: List[VideoTradeSetup] = []
    for raw in rows[:VIDEO_TRADE_MAX]:
        if not isinstance(raw, dict):
            continue
        asset_key = _video_asset_key(raw.get("asset"))
        side = str(raw.get("side", "")).upper().strip()
        if asset_key not in ASSETS or side not in ("BUY", "SELL"):
            continue
        entry = _safe_float(raw.get("entry"), 0.0)
        sl = _safe_float(raw.get("sl"), 0.0)
        tps = []
        for i in range(1, 11):
            v = raw.get(f"tp{i}")
            if v is not None and str(v).strip() not in ("", "null", "None"):
                fv = _safe_float(v, 0.0)
                if fv > 0:
                    tps.append(fv)
        duration = max(0, int(_safe_float(raw.get("duration_minutes"), 0)))
        confidence = max(0.0, min(100.0, _safe_float(raw.get("confidence"), 0.0)))
        complete_fields = 2 + min(10, len(tps))
        possible_fields = 12
        if entry > 0: complete_fields += 1
        if sl > 0: complete_fields += 1
        completeness = min(100.0, complete_fields / possible_fields * 100.0)
        rr1 = abs(tps[0] - entry) / abs(entry - sl) if entry > 0 and sl > 0 and tps and abs(entry-sl) > 0 else 0.0
        rr10 = abs(tps[-1] - entry) / abs(entry - sl) if entry > 0 and sl > 0 and tps and abs(entry-sl) > 0 else 0.0
        setup = VideoTradeSetup(
            id=uuid.uuid4().hex[:10].upper(),
            source_url=source_url,
            source_timestamp=str(raw.get("timestamp", "") or ""),
            asset_key=asset_key,
            asset_name=ASSETS[asset_key]["name"],
            side=side, entry_price=entry, sl=sl, tps=tps,
            duration_minutes=duration, confidence=confidence,
            rationale=str(raw.get("rationale", "") or "")[:1000],
            evidence=str(raw.get("evidence", "") or "")[:1000],
            completeness=round(completeness, 2), rr_tp1=round(rr1, 3), rr_tp10=round(rr10, 3),
            created_at=now_local().isoformat(),
        )
        setup.rank_score = _rank_video_trade_setup(setup)
        setup.status = "COMPLETE" if entry > 0 and sl > 0 and tps else "PARTIAL"
        result.append(setup)
    result.sort(key=lambda x: (x.rank_score, x.confidence, x.rr_tp10), reverse=True)
    return result


def video_trade_summary(setups: List[VideoTradeSetup], limit: int = 10) -> str:
    if not setups:
        return "🎯 لم يتم العثور على صفقة محددة بأرقام يمكن التحقق منها داخل الفيديو."
    lines = ["🎯 الصفقات المستخرجة من الفيديو — مرتبة من الأقوى إلى الأضعف", ""]
    for i, s in enumerate(setups[:limit], 1):
        entry = format_price(s.entry_price) if s.entry_price > 0 else "غير محدد"
        sl = format_price(s.sl) if s.sl > 0 else "غير محدد"
        tp1 = format_price(s.tps[0]) if s.tps else "غير محدد"
        tp10 = format_price(s.tps[-1]) if s.tps else "غير محدد"
        lines.append(
            f"{i}. {s.asset_name} | {s.side} | Rank {s.rank_score}/100\n"
            f"   Entry {entry} | SL {sl} | TP1 {tp1} | Last TP {tp10}\n"
            f"   Confidence {s.confidence:.0f}% | Completeness {s.completeness:.0f}% | RR1 {s.rr_tp1:.2f} | RR10 {s.rr_tp10:.2f}\n"
            f"   Timestamp {s.source_timestamp or '—'} | {s.status} | ID {s.id}"
        )
    return "\n\n".join(lines)


async def process_strategy_video(message: types.Message, url: str, original_text: str):
    await safe_send(message, "🎥 تم اكتشاف رابط فيديو. أبدأ أولًا باستخراج الترجمة إن وجدت، وإذا لم توجد سأحلل الفيديو نفسه بصريًا وصوتيًا عبر Gemini...")

    transcript = await extract_video_text(url)
    source = transcript
    source_kind = "الترجمة/النص"

    if not transcript:
        await safe_send(message, "👁️ لا توجد ترجمة متاحة. جاري تنزيل الفيديو وتحليل الصور، الرسوم، الشموع، المؤشرات والصوت مباشرةً...")
        source = await analyze_video_visually(url)
        source_kind = "تحليل فيديو بصري/صوتي"

    if not source:
        await safe_send(
            message,
            "❌ تعذر الوصول إلى محتوى الفيديو أو تحليله.\n"
            "تمت تجربة Gemini مباشرةً لروابط YouTube ثم مسار yt-dlp + Gemini Files API.\n"
            "لن أختلق محتوى الفيديو أو قواعد غير موجودة.\n"
            + (f"\n🔎 آخر خطأ تقني: {VIDEO_LAST_ERROR[:700]}" if VIDEO_LAST_ERROR else "\n🔎 السبب التقني غير متاح؛ تحقق من Gemini API وRender Logs.")
        )
        return

    # IMPORTANT: extract concrete trades BEFORE converting the video into an
    # abstract strategy. A video may contain valid BUY/SELL setups even when its
    # rules cannot be represented by our StrategyDefinition. The old order
    # returned early here and silently discarded those setups.
    setups: List[VideoTradeSetup] = []
    try:
        setups = await extract_video_trade_setups(url, source)
    except Exception as exc:
        logger.exception("Video trade extraction failed: %s", exc)
        _record_video_error(exc)

    strategy = await convert_content_to_strategy(url, source)
    if strategy:
        strategies_db["strategies"][strategy.id] = asdict(strategy)
        save_strategies()

    # Persist and report concrete trade setups independently of strategy conversion.
    try:
        async with video_trade_lock:
            for setup in setups:
                video_trade_setups[setup.id] = setup
            if len(video_trade_setups) > VIDEO_TRADE_MAX * 10:
                ranked = sorted(video_trade_setups.values(), key=lambda x: (x.rank_score, x.created_at), reverse=True)
                video_trade_setups.clear()
                video_trade_setups.update({x.id: x for x in ranked[:VIDEO_TRADE_MAX * 10]})
            save_video_trade_setups()
        if setups:
            await safe_send(message, video_trade_summary(setups))
        else:
            await safe_send(message, "🎯 لم يستخرج الفيديو صفقة محددة بأرقام موثوقة؛ لن أخترع Entry/SL/TP.")

        # A submitted video must also trigger an immediate reanalysis of the bot's
        # own open trades for the assets explicitly found in the video.
        affected_assets = {x.asset_key for x in setups}
        if not affected_assets:
            affected_assets = set(detect_assets_in_text(source))
        affected_trades = [t for t in list(open_trades.values()) if t.status == "OPEN" and t.asset_key in affected_assets]
        if affected_trades:
            await safe_send(message, f"🔄 الفيديو مرتبط بـ {len(affected_trades)} صفقة مفتوحة. سأعيد تحليلها الآن وأفرزها حسب الأولوية.")
            for t in affected_trades:
                try:
                    await reanalyze_trade(t, reason="video")
                except Exception:
                    logger.exception("Video-triggered reanalysis failed for %s", t.id)
            affected_trades.sort(key=trade_priority_key, reverse=True)
            lines = ["📋 ترتيب الصفقات بعد تحليل الفيديو:"]
            for i, t in enumerate(affected_trades, 1):
                lines.append(f"{i}. {t.asset_name} {t.side} | {t.id} | {t.management} | confidence {t.confidence:.0f}% | R {t.realized_r:.2f}")
            await safe_send(message, "\n".join(lines))
    except Exception as exc:
        logger.exception("Video trade extraction failed")
        await safe_send(message, f"⚠️ تم حفظ الاستراتيجية، لكن تعذر استخراج الصفقات منها: {str(exc)[:500]}")

    if not strategy:
        await safe_send(
            message,
            "⚠️ لم أستطع تحويل محتوى الفيديو إلى StrategyDefinition قابلة للتدريب، "
            "لكن هذا لا يلغي تحليل الصفقات. تم الاحتفاظ بالصفقات التي أمكن قراءتها "
            "وسأعتمد فقط على الأرقام التي ظهرت فعليًا في الفيديو."
        )
        return

    await safe_send(
        message,
        f"🧠 تم استخراج الاستراتيجية: {strategy.name}\n"
        f"المصدر: {source_kind}\n"
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
            result = result_from_db(sid)
            if result is None: continue
            strategy = strategy_from_db(sid)
            if strategy:
                rows.append((strategy, result))
        except Exception:
            continue
    rows.sort(key=lambda pair: (pair[1].score, pair[1].profit_factor, pair[1].net_r), reverse=True)
    return rows[:limit]


# ============================================================
# NEWS LEARNING
# ============================================================
news_learning: Dict[str,dict] = {"items": []}

def load_news_learning():
    global news_learning
    try:
        data = _persistent_get("news_learning")
        if data is None and os.path.exists(NEWS_DB_FILE):
            with open(NEWS_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        news_learning = data if isinstance(data, dict) else {"items": []}
        news_learning.setdefault("items", [])
        logger.info("Loaded %s news-learning records from %s", len(news_learning["items"]), persistence_status())
    except Exception:
        logger.exception("Could not load news learning")


def save_news_learning():
    try:
        if _persistent_set("news_learning", news_learning):
            return
        tmp = NEWS_DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(news_learning, f, ensure_ascii=False, indent=2)
        os.replace(tmp, NEWS_DB_FILE)
    except Exception:
        logger.exception("Could not save news learning")


def parse_news_prediction(ai_text:str, source_text:str) -> dict:
    obj=extract_json_from_ai(ai_text) or {}
    if not isinstance(obj,dict): obj={}
    assets=obj.get("assets") if isinstance(obj.get("assets"),list) else detect_assets_in_text(source_text)
    direction=str(obj.get("direction",obj.get("impact","unknown"))).upper()
    if direction in ("BULLISH", "BULL"): direction="BUY"
    elif direction in ("BEARISH", "BEAR"): direction="SELL"
    if direction not in ("BUY","SELL","MIXED","UNKNOWN"): direction="UNKNOWN"
    try: strength=float(obj.get("strength",obj.get("confidence",0)))
    except Exception: strength=0.0
    try: horizon=int(obj.get("horizon_minutes",60))
    except Exception: horizon=60
    return {"assets":assets,"direction":direction,"strength":max(0,min(100,strength)),"horizon_minutes":max(1,horizon)}

async def evaluate_news_learning():
    changed=False; now=now_local()
    for item in news_learning.get("items",[]):
        if item.get("evaluated") or not item.get("start_price") or now < dt_from_string(item.get("evaluate_at",now.isoformat())): continue
        for asset in item.get("assets",[]):
            try:
                price=await get_price(ASSETS[asset]["symbol"],use_cache=False)
                start=float(item["start_price"].get(asset,price)); actual="BUY" if price>start else "SELL" if price<start else "NEUTRAL"
                item.setdefault("results",{})[asset]={"end_price":price,"actual":actual,"correct": actual==item.get("direction")}
            except Exception: pass
        item["evaluated"]=True; changed=True
    if changed: save_news_learning()

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
    prompt += "\nأعد أيضًا JSON صالحًا فقط في كتلة منفصلة بالمفاتيح: assets(list), direction(BUY/SELL/BULLISH/BEARISH/MIXED/UNKNOWN), strength(0-100), horizon_minutes(integer)."
    ai = await safe_ai_generate(prompt) or "تعذر الوصول إلى Gemini حاليًا. تم استلام النص ويمكن إعادة المحاولة."
    prediction=parse_news_prediction(ai,text)
    item={"id":uuid.uuid4().hex[:10].upper(),"created_at":now_local().isoformat(),"source_text":text,"assets":prediction["assets"],"direction":prediction["direction"],"strength":prediction["strength"],"horizon_minutes":prediction["horizon_minutes"],"start_price":{},"evaluate_at":(now_local()+timedelta(minutes=prediction["horizon_minutes"])).isoformat(),"evaluated":False,"results":{}}
    for a in prediction["assets"]:
        try: item["start_price"][a]=await get_price(ASSETS[a]["symbol"],use_cache=False)
        except Exception: pass
    news_learning.setdefault("items",[]).append(item); news_learning["items"]=news_learning["items"][-500:]; save_news_learning()
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
            mark_trade_closed(trade, trade.last_price, trade.last_action or "EXPIRED - TIME LIMIT", status="EXPIRED")
            save_state()
            await safe_reply(trade.chat_id,
                f"⏰ إغلاق تلقائي بسبب إغلاق السوق\nالأصل: {trade.asset_name}\n"
                f"Trade ID: {trade.id}\nالحالة: {trade.status}\n"
                f"الإجراء: {trade.last_action}")
            return
        # Price-only polling every 30 seconds: protects SL/TP between scheduled reviews.
        price = await get_price(trade.symbol, use_cache=False)
        events = evaluate_trade_price(trade, price, None)
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
            await evaluate_news_learning()
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

async def self_ping_loop():
    """Keep a Render free web service warm while it is running.

    This is only a best-effort mitigation. For guaranteed 24/7 availability,
    use an external uptime monitor or a paid/always-on Render service.
    """
    if not SELF_PING_ENABLED or not PUBLIC_BASE_URL:
        logger.info("Self-ping disabled or RENDER_EXTERNAL_URL/PUBLIC_BASE_URL not set")
        return
    url = PUBLIC_BASE_URL + "/health"
    await asyncio.sleep(30)
    timeout = aiohttp.ClientTimeout(total=15)
    while True:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "TradingBot-keepalive/1.0"}) as resp:
                    logger.info("Self-ping %s -> HTTP %s", url, resp.status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Self-ping failed: %s", exc)
        await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)

# ============================================================
# HEALTH SERVER
# ============================================================
async def health(request: web.Request):
    return web.json_response({
        "status":"ok", "bot":"Trading Bot",
        "open_trades":len([t for t in open_trades.values() if t.status=="OPEN"]),
        "market_data_configured":bool(get_twelve_data_api_key()), "gemini_keys":len(GEMINI_KEYS),
        "time":now_local().isoformat(), "monitor_seconds":MONITOR_SECONDS,
        "account_balance":ACCOUNT_BALANCE, "risk_per_trade_pct":RISK_PER_TRADE_PCT,
        "open_risk":current_open_risk(), "daily_risk_exposure":daily_risk_exposure(),
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
    init_persistence()
    migrate_legacy_json_to_persistence()
    load_state()
    load_strategies()
    load_video_trade_setups()
    load_news_learning()
    if not get_twelve_data_api_key(): logger.warning("TWELVE_DATA_API_KEY is missing.")
    if not GEMINI_KEYS: logger.warning("No Gemini API keys configured.")
    runner=await start_web_server()
    monitor_task=asyncio.create_task(trade_monitor())
    keepalive_task=asyncio.create_task(self_ping_loop())
    try:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            logger.warning("delete_webhook warning: %s",exc)
        logger.info("Starting Telegram polling")
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        monitor_task.cancel()
        keepalive_task.cancel()
        try: await monitor_task
        except asyncio.CancelledError: pass
        try: await keepalive_task
        except asyncio.CancelledError: pass
        save_state()
        try: await runner.cleanup()
        except Exception: pass
        await bot.session.close()
        logger.info("Trading Bot stopped")

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
