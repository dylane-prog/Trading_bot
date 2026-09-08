import os
import asyncio
import logging
import json
import math
import uuid
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
STATE_FILE = os.getenv("STATE_FILE", "trading_state.json")
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
        except (TelegramForbiddenError, Exception) as exc:
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
            open_trades[tid] = Trade(**raw)
        logger.info("Loaded %s trades from state", len(open_trades))
    except Exception:
        logger.exception("Could not load state")


def create_trade(chat_id: int, analysis: Analysis) -> Trade:
    now = now_local()
    mins = max(1, round(analysis.duration_minutes * REANALYSIS_PERCENT))
    return Trade(
        id=uuid.uuid4().hex[:8].upper(), chat_id=chat_id,
        asset_key=analysis.asset_key, asset_name=analysis.asset_name, symbol=analysis.symbol,
        side=analysis.signal, entry_low=analysis.entry_low, entry_high=analysis.entry_high,
        entry_price=analysis.price, sl=analysis.sl, tps=analysis.tps,
        opened_at=now.isoformat(), estimated_duration_minutes=analysis.duration_minutes,
        next_reanalysis_at=(now + timedelta(minutes=mins)).isoformat(),
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
        f"Last action: {trade.last_action}"
    )

def trade_expired(trade: Trade, now: Optional[datetime] = None) -> bool:
    if trade.status != "OPEN":
        return False
    now = now or now_local()
    expiry = dt_from_string(trade.opened_at) + timedelta(minutes=trade.estimated_duration_minutes)
    return now >= expiry


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
        "🧪 الاختبار:\n/auto_backtest\n/weekly_table\n\n"
        "📰 أرسل أو أعد توجيه خبر إلى البوت لتحليل تأثيره.\n\n"
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
    forward_origin = getattr(message,"forward_origin",None)
    is_forward = forward_origin is not None
    is_url = "http://" in text or "https://" in text
    is_strategy = is_url or "استراتيجية" in text.lower() or "strategy" in text.lower()
    kind = "خبر مُعاد توجيهه" if is_forward else ("رابط/استراتيجية" if is_strategy else "خبر")
    await safe_send(message,f"🧠 جاري تحليل {kind}...")
    prompt=f"""
حلل النص التالي كمحلل مخاطر للأسواق. لا تخترع تفاصيل.
النص:
{text}
حدد: نوع المحتوى، الأصول المتأثرة، اتجاه التأثير bullish/bearish/mixed/unknown، قوة 1-5، المدة دقائق/ساعات/أيام، وما يجب مراقبته.
لا تضمن الربح. إذا كان رابط فيديو فلا تدّع أنك شاهدت الفيديو ما لم يتوفر محتواه فعليًا.
"""
    ai=await safe_ai_generate(prompt) or "تعذر الوصول إلى Gemini حاليًا. تم استلام النص ويمكن إعادة المحاولة."
    os.makedirs("memory",exist_ok=True)
    filename="memory/strategies_memory.txt" if is_strategy else "memory/news_memory.txt"
    try:
        with open(filename,"a",encoding="utf-8") as f:
            f.write(f"\n[{now_local().isoformat()}]\nTYPE: {kind}\nTEXT:\n{text}\nANALYSIS:\n{ai}\n"+"="*70+"\n")
    except Exception:
        logger.exception("Could not save memory")
    await safe_send(message,f"📰 تحليل {kind}\n\n{ai}")
    affected=detect_assets_in_text(text)
    for trade in [t for t in list(open_trades.values()) if t.status=="OPEN" and t.asset_key in affected]:
        try:
            old=trade.status
            trade,analysis,events=await reanalyze_trade(trade,reason="news")
            await safe_reply(trade.chat_id,
                f"⚡ إعادة تحليل فورية بسبب خبر\nالأصل: {trade.asset_name}\nTrade ID: {trade.id}\n"
                f"السعر: {format_price(analysis.price)}\nالإشارة: {analysis.signal}\nالحالة: {trade.status}\n"
                f"الإجراء: {trade.last_action}\nTP events: {', '.join(events) if events else 'none'}")
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
