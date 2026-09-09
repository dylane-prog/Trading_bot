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
from typing import Optional, Dict, List, Tuple, Any
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

def get_twelve_data_api_key() -> str:
    """Read the key at request time so Render environment changes are visible."""
    return os.getenv("TWELVE_DATA_API_KEY", "").strip()

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

# Advanced management / validation switches. These are paper-trading controls;
# no broker order is sent by this file.
USE_LEARNED_STRATEGY = os.getenv("USE_LEARNED_STRATEGY", "true").lower() in ("1", "true", "yes", "on")
LEARNED_STRATEGY_MIN_SCORE = float(os.getenv("LEARNED_STRATEGY_MIN_SCORE", "65"))
LEARNED_STRATEGY_MIN_ROBUSTNESS = float(os.getenv("LEARNED_STRATEGY_MIN_ROBUSTNESS", "60"))
TP_ALLOCATION = [float(x) for x in os.getenv("TP_ALLOCATION", "0.10,0.10,0.10,0.10,0.10,0.10,0.10,0.10,0.10,0.10").split(",") if x.strip()]
if len(TP_ALLOCATION) != 10 or any(x <= 0 for x in TP_ALLOCATION) or abs(sum(TP_ALLOCATION) - 1.0) > 1e-6:
    TP_ALLOCATION = [0.10] * 10
BREAK_EVEN_AFTER_TP = max(0, int(os.getenv("BREAK_EVEN_AFTER_TP", "1")))
TRAILING_AFTER_TP = max(0, int(os.getenv("TRAILING_AFTER_TP", "3")))
TRAILING_ATR_MULT = max(0.1, float(os.getenv("TRAILING_ATR_MULT", "0.8")))
MAX_SPREAD_ATR_RATIO = max(0.0, float(os.getenv("MAX_SPREAD_ATR_RATIO", "0.20")))
STALE_PRICE_SECONDS = max(1, int(os.getenv("STALE_PRICE_SECONDS", "45")))
NEWS_DB_FILE = os.getenv("NEWS_DB_FILE", "news_learning.json")
NEWS_EVAL_MINUTES_DEFAULT = max(1, int(os.getenv("NEWS_EVAL_MINUTES_DEFAULT", "60")))
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
    api_key = get_twelve_data_api_key()
    if not api_key:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured in Render Environment Variables.")
    params = dict(params)
    params["apikey"] = api_key
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

async def get_time_series_interval(symbol: str, interval: str, outputsize: int = TD_OUTPUTSIZE) -> List[dict]:
    """Fetch candles for an explicit interval without changing global TD_INTERVAL."""
    cache_key = f"{symbol}:{interval}:{outputsize}"
    now = datetime.now(timezone.utc)
    cached = market_cache.get(cache_key)
    if cached and (now - cached[0]).total_seconds() < MARKET_CACHE_SECONDS:
        return cached[1]
    async with market_lock:
        cached = market_cache.get(cache_key)
        now = datetime.now(timezone.utc)
        if cached and (now - cached[0]).total_seconds() < MARKET_CACHE_SECONDS:
            return cached[1]
        data = await td_get("time_series", {
            "symbol": symbol, "interval": interval,
            "outputsize": min(max(outputsize, 60), 5000),
            "format": "JSON", "timezone": "UTC",
        })
        values=[]
        for row in reversed(data.get("values") or []):
            try:
                values.append({
                    "datetime": row.get("datetime", ""),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row.get("volume", 0) or 0),
                })
            except (ValueError, TypeError, KeyError):
                continue
        if len(values) < 60:
            raise RuntimeError(f"Not enough candles for {symbol} at {interval}. Received {len(values)}.")
        market_cache[cache_key]=(datetime.now(timezone.utc), values)
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

def analyze_market(asset_key: str, candles: List[dict], live_price: Optional[float] = None, interval: Optional[str] = None) -> Analysis:
    cfg = ASSETS[asset_key]
    if len(candles) < 60:
        raise RuntimeError("Insufficient candle data")
    closes=[x["close"] for x in candles]; highs=[x["high"] for x in candles]; lows=[x["low"] for x in candles]
    price=float(live_price if live_price is not None else closes[-1])
    e20,e50=ema(closes,20),ema(closes,50); r=rsi(closes,14); a=atr(highs,lows,closes,14)
    m_line,m_signal,m_hist=macd(closes); support,resistance=support_resistance(highs,lows,60)
    if None in (e20,e50,r,a,m_line,m_signal,m_hist) or a<=0:
        raise RuntimeError("Insufficient indicator data")

    score=0; reasons=[]
    # Correct trend logic: EMA20 > EMA50 is bullish, not bearish.
    if price > e20: score += 1; reasons.append("السعر فوق EMA20")
    else: score -= 1; reasons.append("السعر تحت EMA20")
    if e20 > e50: score += 2; reasons.append("EMA20 فوق EMA50: اتجاه صاعد")
    elif e20 < e50: score -= 2; reasons.append("EMA20 تحت EMA50: اتجاه هابط")
    else: reasons.append("EMA20 قريب جدًا من EMA50: اتجاه محايد")
    if m_hist > 0: score += 2; reasons.append("MACD histogram إيجابي")
    elif m_hist < 0: score -= 2; reasons.append("MACD histogram سلبي")
    if 55 < r < 75: score += 2; reasons.append(f"RSI صاعد ومتوازن: {r:.1f}")
    elif 25 < r < 45: score -= 2; reasons.append(f"RSI هابط ومتوازن: {r:.1f}")
    elif r >= 75: score -= 1; reasons.append(f"RSI مرتفع جدًا: {r:.1f} — خطر تصحيح")
    elif r <= 25: score += 1; reasons.append(f"RSI منخفض جدًا: {r:.1f} — ارتداد محتمل")
    momentum=price-closes[-7] if len(closes)>=7 else 0.0
    if momentum > 0: score += 1; reasons.append("الزخم القصير إيجابي")
    elif momentum < 0: score -= 1; reasons.append("الزخم القصير سلبي")

    # Avoid forcing a trade when the evidence is internally contradictory.
    aligned_buy = price > e20 > e50 and m_hist > 0 and momentum > 0
    aligned_sell = price < e20 < e50 and m_hist < 0 and momentum < 0
    if score >= 5 and aligned_buy: signal="BUY"
    elif score <= -5 and aligned_sell: signal="SELL"
    else: signal="NO TRADE"

    # Confidence is based on agreement, not merely abs(score).
    directional_points = sum([
        price > e20 if signal=="BUY" else price < e20 if signal=="SELL" else False,
        e20 > e50 if signal=="BUY" else e20 < e50 if signal=="SELL" else False,
        m_hist > 0 if signal=="BUY" else m_hist < 0 if signal=="SELL" else False,
        55 < r < 75 if signal=="BUY" else 25 < r < 45 if signal=="SELL" else False,
        momentum > 0 if signal=="BUY" else momentum < 0 if signal=="SELL" else False,
    ]) if signal != "NO TRADE" else 0
    confidence = 45.0 + directional_points * 9.0 if signal != "NO TRADE" else 35.0 + min(20.0, abs(score)*3.0)
    confidence = min(92.0, max(35.0, confidence))

    half=a*0.20; entry_low,entry_high=price-half,price+half
    if signal=="BUY":
        sl=price-a*1.20; tps=[price+a*x for x in (0.8,1.2,1.6,2.0,2.4,2.8,3.2,3.6,4.0,4.5)]
    elif signal=="SELL":
        sl=price+a*1.20; tps=[price-a*x for x in (0.8,1.2,1.6,2.0,2.4,2.8,3.2,3.6,4.0,4.5)]
    else: sl=price; tps=[]
    dur=estimate_trade_duration_minutes(signal,confidence,score,price,a,e20,e50,momentum,candle_interval_minutes(interval or TD_INTERVAL))
    return Analysis(asset_key=asset_key,asset_name=cfg["name"],symbol=cfg["symbol"],price=price,signal=signal,confidence=confidence,
        entry_low=entry_low,entry_high=entry_high,sl=sl,tps=tps,atr_value=a,rsi_value=r,ema20=e20,ema50=e50,
        macd_value=m_line,macd_signal=m_signal,macd_hist=m_hist,support=support,resistance=resistance,duration_minutes=dur,
        score=score,reasons=reasons,generated_at=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"))

async def get_market_snapshot(asset_key: str) -> Tuple[Analysis,List[dict]]:
    cfg=ASSETS[asset_key]
    live_price, base_candles = await asyncio.gather(get_price(cfg["symbol"],False), get_time_series_interval(cfg["symbol"],TD_INTERVAL,max(TD_OUTPUTSIZE,300)))
    preliminary=analyze_market(asset_key,base_candles,live_price,TD_INTERVAL)
    # Choose the analytical timeframe from the preliminary horizon.
    d=preliminary.duration_minutes
    if d and d <= 15: interval="1min"; size=300
    elif d and d <= 180: interval="5min"; size=500
    elif d and d <= 720: interval="15min"; size=500
    else: interval="1h"; size=300
    if interval == TD_INTERVAL:
        candles=base_candles
    else:
        try: candles=await get_time_series_interval(cfg["symbol"],interval,size)
        except Exception: candles=base_candles; interval=TD_INTERVAL
    analysis=analyze_market(asset_key,candles,live_price,interval)

    # Use only sufficiently validated learned strategies as a confluence layer.
    if USE_LEARNED_STRATEGY and strategies_db.get("strategies"):
        best=top_strategies(1)
        if best:
            strategy,result=best[0]
            if result.score >= LEARNED_STRATEGY_MIN_SCORE and result.robustness >= LEARNED_STRATEGY_MIN_ROBUSTNESS and result.verdict in ("STRONG","PROMISING"):
                learned=strategy_signal(strategy,candles,len(candles)-1)
                if learned:
                    if analysis.signal==learned:
                        analysis.score += 1; analysis.confidence=min(94.0,analysis.confidence+4); analysis.reasons.append(f"استراتيجية متعلمة متوافقة: {strategy.name}")
                    elif analysis.signal=="NO TRADE":
                        analysis.reasons.append(f"استراتيجية متعلمة أعطت {learned} لكن التأكيد الفني غير كافٍ")
                    else:
                        analysis.confidence=max(35.0,analysis.confidence-5); analysis.reasons.append(f"تعارض مع الاستراتيجية المتعلمة: {strategy.name}")
    return analysis,candles

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
    remaining_position_size: float = 0.0
    closed_quantity: float = 0.0
    tp_allocations: List[float] = field(default_factory=lambda: [0.10] * 10)
    tp_realized_pnl: List[float] = field(default_factory=lambda: [0.0] * 10)
    break_even_price: float = 0.0
    trailing_stop: float = 0.0
    trailing_active: bool = False
    peak_price: float = 0.0
    expiry_at: str = ""
    expiry_reason: str = "TIME LIMIT"
    closed_at: str = ""

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
            raw.setdefault("risk_percent", 0.0)
            raw.setdefault("risk_amount", 0.0)
            raw.setdefault("position_size", 0.0)
            raw.setdefault("rr_tp1", 0.0)
            raw.setdefault("rr_tp10", 0.0)
            raw.setdefault("realized_pnl", 0.0)
            raw.setdefault("realized_r", 0.0)
            raw.setdefault("close_price", 0.0)
            raw.setdefault("remaining_position_size", raw.get("position_size", 0.0))
            raw.setdefault("closed_quantity", 0.0)
            raw.setdefault("tp_allocations", [0.10] * 10)
            raw.setdefault("tp_realized_pnl", [0.0] * 10)
            raw.setdefault("break_even_price", 0.0)
            raw.setdefault("trailing_stop", 0.0)
            raw.setdefault("trailing_active", False)
            raw.setdefault("peak_price", raw.get("entry_price", 0.0))
            raw.setdefault("expiry_at", "")
            raw.setdefault("expiry_reason", "TIME LIMIT")
            if not raw.get("expiry_at") and raw.get("opened_at"):
                try:
                    exp, why = calculate_trade_expiry(dt_from_string(raw["opened_at"]), max(1, int(raw.get("estimated_duration_minutes", 1))), raw.get("asset_key", ""))
                    raw["expiry_at"], raw["expiry_reason"] = exp.isoformat(), why
                except Exception:
                    pass
            raw.setdefault("closed_at", "")
            # Backward compatibility: give older open trades risk metadata
            # without changing their entry/SL.
            if raw.get("status") == "OPEN" and float(raw.get("risk_amount", 0.0) or 0.0) <= 0:
                stop_distance = abs(float(raw.get("entry_price", 0.0)) - float(raw.get("sl", 0.0)))
                if ACCOUNT_BALANCE > 0 and stop_distance > 0:
                    raw["risk_percent"] = RISK_PER_TRADE_PCT
                    raw["risk_amount"] = ACCOUNT_BALANCE * RISK_PER_TRADE_PCT / 100.0
                    raw["position_size"] = raw["risk_amount"] / stop_distance
                    raw["rr_tp1"] = abs(float(raw.get("tps", [0.0])[0]) - float(raw.get("entry_price", 0.0))) / stop_distance if raw.get("tps") else 0.0
                    raw["rr_tp10"] = abs(float(raw.get("tps", [0.0])[-1]) - float(raw.get("entry_price", 0.0))) / stop_distance if raw.get("tps") else 0.0
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


def create_trade(chat_id: int, analysis: Analysis, risk: Optional[dict] = None) -> Trade:
    now=now_local(); duration=max(MIN_TRADE_DURATION_MINUTES,min(MAX_TRADE_DURATION_MINUTES,int(analysis.duration_minutes or 1)))
    expiry,reason=calculate_trade_expiry(now,duration,analysis.asset_key); market_close=next_market_close(analysis.asset_key,now)
    size=float((risk or {}).get("position_size",0.0))
    return Trade(id=uuid.uuid4().hex[:8].upper(),chat_id=chat_id,asset_key=analysis.asset_key,asset_name=analysis.asset_name,symbol=analysis.symbol,
        side=analysis.signal,entry_low=analysis.entry_low,entry_high=analysis.entry_high,entry_price=analysis.price,sl=analysis.sl,tps=list(analysis.tps),
        opened_at=now.isoformat(),estimated_duration_minutes=duration,next_reanalysis_at=(now+timedelta(minutes=max(1,round(duration*REANALYSIS_PERCENT)))).isoformat(),
        market_close_at=market_close.isoformat() if market_close else "",last_price=analysis.price,score=analysis.score,confidence=analysis.confidence,
        risk_percent=RISK_PER_TRADE_PCT,risk_amount=float((risk or {}).get("risk_amount",0.0)),position_size=size,remaining_position_size=size,
        rr_tp1=float((risk or {}).get("rr_tp1",0.0)),rr_tp10=float((risk or {}).get("rr_tp10",0.0)),tp_allocations=list(TP_ALLOCATION),
        tp_realized_pnl=[0.0]*10,peak_price=analysis.price,expiry_at=expiry.isoformat(),expiry_reason=reason)

def trade_status_text(trade: Trade) -> str:
    next_time=dt_from_string(trade.next_reanalysis_at).strftime("%Y-%m-%d %H:%M:%S") if trade.next_reanalysis_at else "-"
    expiry=dt_from_string(trade.expiry_at).strftime("%Y-%m-%d %H:%M:%S") if trade.expiry_at else "-"
    reached=", ".join(f"TP{x}" for x in trade.hit_tps) if trade.hit_tps else "none"
    return (f"{trade.asset_name} | {trade.side} | {trade.status}\nID: {trade.id}\nEntry: {format_price(trade.entry_price)}\n"
        f"SL: {format_price(trade.sl)}\nLast price: {format_price(trade.last_price)}\nRisk: {trade.risk_amount:.2f}$ ({trade.risk_percent:.2f}%)\n"
        f"Position: {trade.remaining_position_size:.6f}/{trade.position_size:.6f} units\nEstimated duration: {format_duration_minutes(trade.estimated_duration_minutes)}\nRealized PnL: {trade.realized_pnl:.2f}$ ({trade.realized_r:.2f}R)\n"
        f"R:R TP1 / TP10: {trade.rr_tp1:.2f} / {trade.rr_tp10:.2f}\nManagement: {trade.management}\nReached TP: {reached}\n"
        f"Break-even: {format_price(trade.break_even_price) if trade.break_even_price else 'not active'}\n"
        f"Trailing: {format_price(trade.trailing_stop) if trade.trailing_active else 'not active'}\nNext reanalysis: {next_time}\n"
        f"Expiry: {expiry} ({trade.expiry_reason})\nMarket close: {(dt_from_string(trade.market_close_at).strftime('%Y-%m-%d %H:%M:%S') if trade.market_close_at else '24/7 / no close')}\n"
        f"Last action: {trade.last_action}")

def trade_expired(trade: Trade, now: Optional[datetime] = None) -> bool:
    if trade.status!="OPEN": return False
    now=now or now_local()
    if trade.expiry_at:
        expiry=dt_from_string(trade.expiry_at); reason=trade.expiry_reason or "TIME LIMIT"
    else:
        expiry,reason=calculate_trade_expiry(dt_from_string(trade.opened_at),max(1,trade.estimated_duration_minutes),trade.asset_key)
    if now>=expiry:
        trade.last_action=f"EXPIRED - {reason}"; return True
    return False

def _pnl_for_qty(trade: Trade, entry: float, price: float, qty: float) -> float:
    if trade.side=="BUY": return (price-entry)*qty
    return (entry-price)*qty

def _activate_management(trade: Trade, price: float, atr_value: float):
    if BREAK_EVEN_AFTER_TP and len(trade.hit_tps) >= BREAK_EVEN_AFTER_TP and trade.remaining_position_size > 0:
        trade.break_even_price=trade.entry_price
        if (trade.side=="BUY" and trade.sl < trade.entry_price) or (trade.side=="SELL" and trade.sl > trade.entry_price):
            trade.sl=trade.entry_price
        trade.management="BREAK-EVEN"
    if TRAILING_AFTER_TP and len(trade.hit_tps) >= TRAILING_AFTER_TP and trade.remaining_position_size > 0 and atr_value>0:
        trail=price-atr_value*TRAILING_ATR_MULT if trade.side=="BUY" else price+atr_value*TRAILING_ATR_MULT
        if not trade.trailing_active:
            trade.trailing_stop=trail; trade.trailing_active=True
        elif trade.side=="BUY": trade.trailing_stop=max(trade.trailing_stop,trail)
        else: trade.trailing_stop=min(trade.trailing_stop,trail)
        if trade.side=="BUY": trade.sl=max(trade.sl,trade.trailing_stop)
        else: trade.sl=min(trade.sl,trade.trailing_stop)
        trade.management="TRAILING"

def mark_trade_closed(trade: Trade, price: float, action: str, status: str="CLOSED"):
    if trade.status != "OPEN": return
    if trade.remaining_position_size > 0:
        pnl=_pnl_for_qty(trade,trade.entry_price,price,trade.remaining_position_size)
        trade.realized_pnl += pnl; trade.closed_quantity += trade.remaining_position_size
        trade.remaining_position_size=0.0
    trade.status=status; trade.management="CLOSE"; trade.last_action=action; trade.close_price=float(price); trade.closed_at=now_local().isoformat()
    trade.realized_r=(trade.realized_pnl/trade.risk_amount) if trade.risk_amount>0 else 0.0

def evaluate_trade_price(trade: Trade, price: float, atr_value: float=0.0) -> List[str]:
    events=[]; trade.last_price=float(price); trade.peak_price=max(trade.peak_price,price) if trade.side=="BUY" else min(trade.peak_price or price,price)
    if trade.status!="OPEN": return events
    if trade.side=="BUY" and price<=trade.sl: mark_trade_closed(trade,price,"CLOSE - SL"); return ["SL"]
    if trade.side=="SELL" and price>=trade.sl: mark_trade_closed(trade,price,"CLOSE - SL"); return ["SL"]
    for i,tp in enumerate(trade.tps,1):
        if i in trade.hit_tps: continue
        hit=(price>=tp) if trade.side=="BUY" else (price<=tp)
        if not hit: continue
        trade.hit_tps.append(i); events.append(f"TP{i}")
        alloc=trade.tp_allocations[i-1] if i-1<len(trade.tp_allocations) else 0.10
        qty=min(trade.remaining_position_size,trade.position_size*alloc)
        if qty>0:
            pnl=_pnl_for_qty(trade,trade.entry_price,tp,qty); trade.realized_pnl += pnl; trade.tp_realized_pnl[i-1]=pnl; trade.closed_quantity += qty; trade.remaining_position_size=max(0.0,trade.remaining_position_size-qty)
        if trade.remaining_position_size <= max(1e-12,trade.position_size*0.0001):
            mark_trade_closed(trade,price,"CLOSE - FINAL TP"); events.append("FINAL TP"); return events
    _activate_management(trade,price,atr_value)
    if events: trade.last_action=events[-1]+" HIT"
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
        remaining_fraction = (trade.remaining_position_size / trade.position_size) if trade.position_size > 0 else 1.0
        trade.side = analysis.signal
        trade.entry_low = analysis.entry_low
        trade.entry_high = analysis.entry_high
        trade.entry_price = analysis.price
        trade.sl = analysis.sl
        trade.tps = analysis.tps
        trade.hit_tps = []
        trade.notified_tps = []
        trade.closed_quantity = 0.0
        trade.tp_realized_pnl = [0.0] * 10
        trade.estimated_duration_minutes = max(1, analysis.duration_minutes)
        refresh_trade_risk(trade, analysis)
        trade.remaining_position_size = trade.position_size * max(0.0, min(1.0, remaining_fraction))
        trade.closed_quantity = trade.position_size - trade.remaining_position_size
        trade.break_even_price = 0.0
        trade.trailing_stop = 0.0
        trade.trailing_active = False
        close_at = next_market_close(trade.asset_key, now)
        trade.market_close_at = close_at.isoformat() if close_at else ""
        trade.expiry_at, trade.expiry_reason = (lambda x: (x[0].isoformat(), x[1]))(calculate_trade_expiry(now, trade.estimated_duration_minutes, trade.asset_key))

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
async def market_quality_gate(analysis: Analysis) -> Tuple[bool, str]:
    """Optional spread/quality protection. If quote fields are unavailable, do not invent a spread."""
    if MAX_SPREAD_ATR_RATIO <= 0 or analysis.atr_value <= 0:
        return True, "quality checks disabled"
    try:
        data = await td_get("quote", {"symbol": analysis.symbol, "format": "JSON"})
        bid = float(data.get("bid")); ask = float(data.get("ask"))
        spread = max(0.0, ask-bid)
        if spread > analysis.atr_value * MAX_SPREAD_ATR_RATIO:
            return False, f"السبريد {spread:.6f} أكبر من {MAX_SPREAD_ATR_RATIO:.2f}×ATR ({analysis.atr_value:.6f})."
    except Exception:
        # Twelve Data does not expose bid/ask for every instrument/data plan.
        return True, "spread unavailable; not used"
    return True, "OK"

async def perform_asset_analysis(message: types.Message, asset_key: str, create_new_trade: bool = True):
    if not get_twelve_data_api_key():
        await safe_send(message, "❌ TWELVE_DATA_API_KEY غير متاح داخل عملية البوت.")
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
            quality_ok, quality_reason = await market_quality_gate(analysis)
            if not quality_ok:
                await safe_send(message, analysis_message(analysis, ai_note) + f"\n\n🛡️ لم تُفتح الصفقة بسبب جودة السوق: {quality_reason}")
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
# COMMANDS
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await safe_send(message,
        "👑 مرحبًا بك في Trading Bot\n\n"
        "📈 التحليل الحي:\n/gold\n/btc\n/eurusd\n/silver\n/oil\n/eth\n\n"
        "📌 إدارة الصفقات:\n/trades\n/close ALL\n/close TRADE_ID\n/reanalyze TRADE_ID\n/status\n/risk\n\n"
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
    """Baseline backtest using the same directional logic and 10-TP management."""
    if len(candles)<140: return {"trades":0,"wins":0,"losses":0,"win_rate":0,"profit_factor":0,"net_r":0,"max_drawdown_r":0,"expectancy_r":0,"sharpe_like":0,"note":"Not enough historical candles."}
    results=[]; durations=[]; i=60
    while i < len(candles)-12:
        c=[x["close"] for x in candles[:i+1]]; h=[x["high"] for x in candles[:i+1]]; l=[x["low"] for x in candles[:i+1]]
        e20,e50,rr,aa=ema(c,20),ema(c,50),rsi(c,14),atr(h,l,c,14)
        if None in (e20,e50,rr,aa) or aa<=0: i+=1; continue
        price=c[-1]
        side="BUY" if price>e20>e50 and rr>55 else "SELL" if price<e20<e50 and rr<45 else None
        if not side: i+=1; continue
        sl=price-aa*1.2 if side=="BUY" else price+aa*1.2
        tps=[price+aa*x for x in (0.8,1.2,1.6,2.0,2.4,2.8,3.2,3.6,4.0,4.5)] if side=="BUY" else [price-aa*x for x in (0.8,1.2,1.6,2.0,2.4,2.8,3.2,3.6,4.0,4.5)]
        remaining=1.0; realized=0.0; hit=[]; trail=sl; be=False; exit_j=None
        for j in range(i+1,min(i+1+max(12,72),len(candles))):
            bar=candles[j]
            # Conservative OHLC assumption: SL first when SL and TP are both touched.
            if side=="BUY" and bar["low"]<=trail:
                realized += remaining*((trail-price)/aa); exit_j=j; break
            if side=="SELL" and bar["high"]>=trail:
                realized += remaining*((price-trail)/aa); exit_j=j; break
            for n,tp in enumerate(tps,1):
                if n in hit: continue
                touched=(bar["high"]>=tp) if side=="BUY" else (bar["low"]<=tp)
                if touched:
                    alloc=0.10; realized += alloc*((tp-price)/aa if side=="BUY" else (price-tp)/aa); remaining-=alloc; hit.append(n)
                    if len(hit)>=BREAK_EVEN_AFTER_TP and not be:
                        trail=price; be=True
                    if len(hit)>=TRAILING_AFTER_TP:
                        candidate=bar["close"]-aa*TRAILING_ATR_MULT if side=="BUY" else bar["close"]+aa*TRAILING_ATR_MULT
                        trail=max(trail,candidate) if side=="BUY" else min(trail,candidate)
                    if remaining<=1e-9: exit_j=j; break
            if exit_j is not None: break
        if exit_j is None:
            exit_j=min(len(candles)-1,i+72); exit_price=candles[exit_j]["close"]; realized += remaining*((exit_price-price)/aa if side=="BUY" else (price-exit_price)/aa)
        results.append(realized); durations.append(max(1,(exit_j or i)-i)); i += max(1,(exit_j or i)-i)
    trades=[r for r in results if r!=0]; wins=[r for r in trades if r>0]; losses=[r for r in trades if r<0]; gp=sum(wins); gl=abs(sum(losses)); pf=gp/gl if gl else (99.0 if gp else 0.0); net=sum(trades); wr=len(wins)/len(trades)*100 if trades else 0; exp=net/len(trades) if trades else 0
    eq=peak=dd=0.0
    for r in results: eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq)
    _,_,_,_,sh=_series_metrics(results)
    return {"trades":len(trades),"wins":len(wins),"losses":len(losses),"win_rate":round(wr,2),"profit_factor":round(pf,3),"net_r":round(net,3),"max_drawdown_r":round(dd,3),"expectancy_r":round(exp,4),"sharpe_like":round(sh,4),"avg_duration_bars":round(sum(durations)/len(durations),2) if durations else 0,"note":"Walk-forward-style historical simulation with 10 TP allocations, break-even and trailing management. Conservative SL-first OHLC assumption."}

@dp.message(Command("auto_backtest"))
async def cmd_auto_backtest(message: types.Message):
    if not get_twelve_data_api_key():
        await safe_send(message, "❌ TWELVE_DATA_API_KEY غير متاح للعملية الحالية.")
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
        text += f"{name}\nTrades: {r['trades']}\nWins: {r['wins']}\nLosses: {r['losses']}\nWin rate: {r['win_rate']}%\nPF: {r.get('profit_factor',0)} | Net R: {r.get('net_r',0)}\nMax DD: {r.get('max_drawdown_r',0)}R | Expectancy: {r.get('expectancy_r',0)}R\nSharpe-like: {r.get('sharpe_like',0)}\nAvg duration: {r.get('avg_duration_bars',0)} bars\nNote: {r['note']}\n\n"
    await safe_send(message, text + "⚠️ الاختبار تاريخي وليس ضمانًا للنتائج المستقبلية.")

@dp.message(Command("weekly_table"))
async def cmd_weekly_table(message: types.Message):
    if not get_twelve_data_api_key():
        await safe_send(message, "❌ TWELVE_DATA_API_KEY غير متاح للعملية الحالية."); return
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
    oos_trades: int = 0
    oos_win_rate: float = 0.0
    oos_profit_factor: float = 0.0
    oos_net_r: float = 0.0
    sharpe_like: float = 0.0


strategies_db: Dict[str, dict] = {"strategies": {}, "results": {}}
strategy_db_lock = asyncio.Lock()

news_learning: Dict[str, dict] = {"records": {}}
news_lock = asyncio.Lock()

def load_news_learning():
    global news_learning
    if not os.path.exists(NEWS_DB_FILE): return
    try:
        with open(NEWS_DB_FILE,"r",encoding="utf-8") as f: data=json.load(f)
        if isinstance(data,dict): news_learning={"records":data.get("records",{})}
    except Exception: logger.exception("Could not load news learning")

def save_news_learning():
    try:
        tmp=NEWS_DB_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f: json.dump(news_learning,f,ensure_ascii=False,indent=2)
        os.replace(tmp,NEWS_DB_FILE)
    except Exception: logger.exception("Could not save news learning")

def _parse_news_json(text: str) -> Optional[dict]:
    data=extract_json_from_ai(text)
    if not data: return None
    assets=[a for a in data.get("assets",[]) if a in ASSETS]
    direction=str(data.get("direction","unknown")).lower()
    if direction not in ("bullish","bearish","mixed","unknown"): direction="unknown"
    try: strength=max(1,min(5,int(data.get("strength",0))))
    except Exception: strength=0
    try: horizon=max(1,min(4320,int(data.get("horizon_minutes",NEWS_EVAL_MINUTES_DEFAULT))))
    except Exception: horizon=NEWS_EVAL_MINUTES_DEFAULT
    return {"assets":assets,"direction":direction,"strength":strength,"horizon_minutes":horizon,"summary":str(data.get("summary", ""))[:1000]}

async def evaluate_news_learning():
    due=[]; now=now_local()
    for rid,rec in list(news_learning.get("records",{}).items()):
        if rec.get("evaluated") or not rec.get("due_at"): continue
        try:
            if dt_from_string(rec["due_at"])<=now: due.append((rid,rec))
        except Exception: continue
    changed=False
    for rid,rec in due:
        assets=rec.get("assets",[])
        outcomes=[]
        for asset in assets:
            try:
                current=await get_price(ASSETS[asset]["symbol"],use_cache=False)
                start=float(rec.get("prices",{}).get(asset,0));
                if start<=0: continue
                move=current-start
                actual="bullish" if move>0 else "bearish" if move<0 else "flat"
                predicted=rec.get("direction","unknown")
                correct=(predicted==actual) if predicted in ("bullish","bearish") else None
                outcomes.append({"asset":asset,"start":start,"end":current,"move":move,"actual":actual,"correct":correct})
            except Exception as exc: logger.warning("News outcome failed %s/%s: %s",rid,asset,exc)
        rec["outcomes"]=outcomes; rec["evaluated"]=True; rec["evaluated_at"]=now.isoformat(); changed=True
    if changed: save_news_learning()



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


def _series_metrics(values: List[float]) -> Tuple[int,float,float,float,float]:
    trades=[r for r in values if r!=0.0]; wins=[r for r in trades if r>0]; losses=[r for r in trades if r<0]
    gp=sum(wins); gl=abs(sum(losses)); pf=gp/gl if gl>0 else (99.0 if gp>0 else 0.0)
    wr=(len(wins)/len(trades)*100.0) if trades else 0.0
    net=sum(trades);
    if len(trades)>1:
        mean=sum(trades)/len(trades); var=sum((x-mean)**2 for x in trades)/(len(trades)-1); sharpe=mean/math.sqrt(var)*math.sqrt(len(trades)) if var>0 else (99.0 if mean>0 else 0.0)
    else: sharpe=0.0
    return len(trades),wr,pf,net,sharpe

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

    _, _, _, _, sharpe_like = _series_metrics(r_values)
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
        sharpe_like=round(sharpe_like, 4),
    )


async def train_strategy(strategy: StrategyDefinition) -> StrategyResult:
    """Train on the first 70% of sampled history and validate on the last 30%."""
    async with STRATEGY_TRAINING_LOCK:
        training_status.update({"running":True,"strategy_id":strategy.id,"strategy_name":strategy.name,"tests":0,"trades":0,"message":"loading historical candles"})
        all_r=[]; oos_r=[]; assets_tested=0
        try:
            for asset_key,cfg in ASSETS.items():
                try:
                    candles=await get_time_series(cfg["symbol"],500); max_index=len(candles)-strategy.max_hold_bars-1; first_index=60
                    if max_index<=first_index: continue
                    available=max_index-first_index+1; count=min(TRAINING_TESTS,available)
                    if count==1: indices=[first_index]
                    else: indices=sorted(set(first_index+round(i*(available-1)/(count-1)) for i in range(count)))
                    split=max(1,int(len(indices)*0.70)); train_indices=indices[:split]; test_indices=indices[split:]
                    for idx in train_indices: all_r.append(simulate_strategy_test(strategy,candles,idx))
                    for idx in test_indices:
                        r=simulate_strategy_test(strategy,candles,idx); all_r.append(r); oos_r.append(r)
                    assets_tested+=1; training_status["tests"]=len(all_r); training_status["trades"]=sum(1 for x in all_r if x!=0.0); training_status["message"]=f"testing {asset_key}"
                except Exception as exc: logger.warning("Strategy training failed on %s: %s",asset_key,exc)
            result=calculate_strategy_metrics(strategy.id,all_r,len(all_r),assets_tested)
            oos_trades,oos_wr,oos_pf,oos_net,_=_series_metrics(oos_r)
            result.oos_trades=oos_trades; result.oos_win_rate=round(oos_wr,2); result.oos_profit_factor=round(oos_pf,3); result.oos_net_r=round(oos_net,3)
            strategies_db["results"][strategy.id]=asdict(result); strategies_db["strategies"][strategy.id]=asdict(strategy); save_strategies(); return result
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
            f"OOS: {result.oos_trades} trades | WR {result.oos_win_rate}% | PF {result.oos_profit_factor} | Net R {result.oos_net_r}",
            f"Sharpe-like: {result.sharpe_like}",
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
    forward_origin=getattr(message,"forward_origin",None); is_forward=forward_origin is not None
    url=extract_url(text)
    if url and is_video_url(url):
        await process_strategy_video(message,url,text); return
    is_strategy_text="استراتيجية" in text.lower() or "strategy" in text.lower()
    kind="خبر مُعاد توجيهه" if is_forward else ("استراتيجية/نص" if is_strategy_text else "خبر")
    await safe_send(message,f"🧠 جاري تحليل {kind}...")
    prompt=f"""
حلل النص التالي كمحلل مخاطر للأسواق. لا تخترع تفاصيل.
أخرج JSON فقط بالشكل:
{{"assets":[],"direction":"bullish|bearish|mixed|unknown","strength":1,"horizon_minutes":60,"summary":"..."}}
assets يجب أن تكون فقط من: {list(ASSETS.keys())}.
إذا لم يذكر النص أصلًا بوضوح، اجعل assets فارغة. horizon_minutes بين 1 و4320.
النص:
{text[:12000]}
"""
    ai=await safe_ai_generate(prompt)
    parsed=_parse_news_json(ai)
    if parsed:
        display=(f"النوع: {kind}\nالأصول: {', '.join(parsed['assets']) or 'غير محددة'}\n"
                 f"التأثير: {parsed['direction']}\nالقوة: {parsed['strength']}/5\n"
                 f"الأفق: {format_duration_minutes(parsed['horizon_minutes'])}\n{parsed['summary']}")
    else:
        display=ai or "تعذر الوصول إلى Gemini حاليًا. تم استلام النص ويمكن إعادة المحاولة."

    os.makedirs("memory",exist_ok=True)
    filename="memory/strategies_memory.txt" if is_strategy_text else "memory/news_memory.txt"
    try:
        with open(filename,"a",encoding="utf-8") as f: f.write(f"\n[{now_local().isoformat()}]\nTYPE: {kind}\nTEXT:\n{text}\nANALYSIS:\n{display}\n"+"="*70+"\n")
    except Exception: logger.exception("Could not save memory")

    if parsed and not is_strategy_text and parsed["assets"] and parsed["direction"] in ("bullish","bearish"):
        prices={}
        for asset in parsed["assets"]:
            try: prices[asset]=await get_price(ASSETS[asset]["symbol"],False)
            except Exception: pass
        rid="NEWS-"+uuid.uuid4().hex[:10].upper(); now=now_local()
        news_learning["records"][rid]={"id":rid,"created_at":now.isoformat(),"due_at":(now+timedelta(minutes=parsed["horizon_minutes"])).isoformat(),
            "assets":parsed["assets"],"direction":parsed["direction"],"strength":parsed["strength"],"horizon_minutes":parsed["horizon_minutes"],"prices":prices,"evaluated":False,"outcomes":[]}
        save_news_learning()
        display += f"\n\n🧠 News Learning ID: {rid}\nسيتم قياس النتيجة بعد {format_duration_minutes(parsed['horizon_minutes'])}."

    await safe_send(message,f"📰 تحليل {kind}\n\n{display}")
    affected=detect_assets_in_text(text)
    for trade in [t for t in list(open_trades.values()) if t.status=="OPEN" and t.asset_key in affected]:
        try:
            trade,analysis,events=await reanalyze_trade(trade,reason="news")
            await safe_reply(trade.chat_id,f"⚡ إعادة تحليل فورية بسبب خبر\nالأصل: {trade.asset_name}\nTrade ID: {trade.id}\nالسعر: {format_price(analysis.price)}\nالإشارة: {analysis.signal}\nالحالة: {trade.status}\nالإجراء: {trade.last_action}\nTP events: {', '.join(events) if events else 'none'}")
        except Exception: logger.exception("News reanalysis failed")


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
            await evaluate_news_learning()
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
    load_state()
    load_strategies()
    load_news_learning()
    if not get_twelve_data_api_key(): logger.warning("TWELVE_DATA_API_KEY is missing.")
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
