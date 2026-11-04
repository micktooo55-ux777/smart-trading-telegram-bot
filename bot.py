from __future__ import annotations
import asyncio
import json
import logging
import os
import sqlite3
from urllib.request import Request, urlopen
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TZ = os.getenv("TIMEZONE", "Asia/Riyadh")
INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MINUTES", "1"))
YF_PERIOD = os.getenv("YFINANCE_PERIOD", "5d")
YF_INTERVAL = os.getenv("YFINANCE_INTERVAL", "15m")
MAX_AGE_MIN = int(os.getenv("MAX_DATA_AGE_MINUTES", "45"))
RISK_PCT = float(os.getenv("DEFAULT_RISK_PERCENT", "1.0"))
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))
CONTRACT_UNITS_PER_LOT = float(os.getenv("CONTRACT_UNITS_PER_LOT", "100000"))
PRICE_VALUE_PER_UNIT = float(os.getenv("PRICE_VALUE_PER_UNIT", "1.0"))
NEWS_FILTER_REQUIRED = os.getenv("NEWS_FILTER_REQUIRED", "1").lower() in {"1", "true", "yes"}
NEWS_EVENTS_FILE = ROOT / os.getenv("NEWS_EVENTS_FILE", "data/news_events.json")
NEWS_EVENTS_URL = os.getenv("NEWS_EVENTS_URL", "").strip()
NEWS_BLACKOUT_MIN = int(os.getenv("NEWS_BLACKOUT_MINUTES", "30"))
NEWS_ADAPTIVE = os.getenv("NEWS_ADAPTIVE", "1").lower() in {"1", "true", "yes"}
DXY_SYMBOL = os.getenv("DXY_SYMBOL", "DX-Y.NYB")
PRICE_PROVIDER = os.getenv("PRICE_PROVIDER", "yahoo").lower()
BROKER_FEED_URL = os.getenv("BROKER_FEED_URL", "").strip()
BROKER_FEED_TOKEN = os.getenv("BROKER_FEED_TOKEN", "").strip()
BROKER_FEED_TIMEOUT = int(os.getenv("BROKER_FEED_TIMEOUT_SECONDS", "8"))
ALLOW_PUBLIC_FALLBACK = os.getenv("ALLOW_PUBLIC_FALLBACK", "1").lower() in {"1", "true", "yes"}
DB_PATH = ROOT / "data" / "bot.db"
SIGNATURE = "> Built by Eng. Nafea Abdulrahman Al-Karandi"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smart-bot")

# Canonical MT5 symbols: base currency first, quote currency second.
# Yahoo tickers are only the read-only data adapter; execution/quotes are not MT5.
MT5_SYMBOLS = [
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
    "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF", "EURJPY",
    "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF", "GBPJPY",
    "AUDNZD", "AUDCAD", "AUDCHF", "AUDJPY", "NZDCAD", "NZDCHF", "NZDJPY",
    "CADCHF", "CADJPY", "CHFJPY", "XAUUSD", "XAGUSD", "BTCUSD",
]
YAHOO_SYMBOLS = {
    symbol: (
        "BTC-USD" if symbol == "BTCUSD" else
        "GC=F" if symbol == "XAUUSD" else
        "SI=F" if symbol == "XAGUSD" else
        f"{symbol}=X"
    )
    for symbol in MT5_SYMBOLS
}

@dataclass
class Signal:
    asset: str
    side: str
    price: float
    rsi: float
    pattern: str
    stop_distance: float
    confidence: str
    observed_at: str
    source: str = "broker_live_feed"
    dxy_bias: str = "unknown"
    correlation_note: str = ""
    news_note: str = ""


TRADE_STYLES = {
    "scalp": {"label": "⚡ سكالبينج", "sl": 1.0, "tp": (0.8, 1.2, 1.8)},
    "medium": {"label": "📊 متوسط المدى", "sl": 1.5, "tp": (1.5, 2.5, 3.5)},
    "long": {"label": "🌐 بعيد المدى", "sl": 2.5, "tp": (2.5, 4.0, 6.0)},
}


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(chat_id INTEGER PRIMARY KEY, balance REAL, paused INTEGER DEFAULT 0, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY, chat_id INTEGER, asset TEXT, side TEXT, price REAL, rsi REAL, pattern TEXT, stop_distance REAL, risk_pct REAL, observed_at TEXT, status TEXT DEFAULT 'open', outcome REAL, checked_at TEXT);
    CREATE INDEX IF NOT EXISTS idx_signals_user ON signals(chat_id, observed_at);
    """)
    _add_columns(conn, "users", {"alerts_enabled": "INTEGER DEFAULT 1"})
    _add_columns(conn, "signals", {
        "selected_style": "TEXT",
        "rating": "INTEGER",
        "profit_reserved": "INTEGER DEFAULT 0",
        "last_refresh": "TEXT",
        "dxy_bias": "TEXT",
        "correlation_note": "TEXT",
        "news_note": "TEXT",
    })
    return conn


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_user(chat_id: int):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(chat_id, balance, created_at) VALUES (?, NULL, ?)", (chat_id, now_iso()))


def _load_news_events() -> list[dict]:
    """Load a normalized economic-calendar JSON list; never invent missing events."""
    try:
        if NEWS_EVENTS_URL:
            request = Request(NEWS_EVENTS_URL, headers={"User-Agent": "smart-trading-telegram-bot/1.0"})
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        elif NEWS_EVENTS_FILE.exists():
            payload = json.loads(NEWS_EVENTS_FILE.read_text(encoding="utf-8"))
        else:
            return []
        events = payload.get("events", payload) if isinstance(payload, dict) else payload
        return [event for event in events if isinstance(event, dict)]
    except Exception as exc:
        log.warning("news calendar unavailable: %s", exc)
        return []


def news_guard(asset: str, at: Optional[datetime] = None) -> tuple[bool, str]:
    """Return (allowed, note). Missing data blocks when NEWS_FILTER_REQUIRED is enabled."""
    events = _load_news_events()
    if not events:
        return (not NEWS_FILTER_REQUIRED, "لا يوجد تقويم أخبار موثوق متاح")
    at = at or datetime.now(timezone.utc)
    for event in events:
        try:
            impact = str(event.get("impact", "")).lower()
            event_time = datetime.fromisoformat(str(event["time"]).replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            currencies = {str(x).upper() for x in event.get("currencies", [])}
            asset_currencies = {asset[:3], asset[3:6]} if len(asset) >= 6 else set()
            relevant = not currencies or bool(currencies & asset_currencies) or "USD" in currencies
            within = abs((event_time - at).total_seconds()) <= NEWS_BLACKOUT_MIN * 60
            if impact in {"high", "critical", "3"} and relevant and within:
                if NEWS_ADAPTIVE:
                    return True, f"ADAPTIVE:{event.get('title', 'عالي التأثير')} حتى {event_time.isoformat()}"
                return False, f"حظر أخبار {event.get('title', 'عالي التأثير')} حتى {event_time.isoformat()}"
        except (KeyError, TypeError, ValueError):
            continue
    return True, "لا يوجد إصدار عالي التأثير ضمن نافذة الحظر"


def dxy_guard(asset: str, side: str, dxy_data: Optional[pd.DataFrame]) -> tuple[bool, str, str]:
    """Confirm USD-correlated assets against DXY EMA direction."""
    if dxy_data is None or dxy_data.empty:
        return False, "unknown", "بيانات DXY غير متاحة؛ تم حظر الإشارة"
    close = dxy_data["Close"].astype(float)
    fast = float(close.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1])
    slow = float(close.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1])
    dxy_bias = "down" if fast < slow else "up" if fast > slow else "flat"
    if asset in {"XAUUSD", "XAGUSD"}:
        allowed = (side == "BUY" and dxy_bias == "down") or (side == "SELL" and dxy_bias == "up")
        return allowed, dxy_bias, f"DXY={dxy_bias}; {asset} يتطلب ارتباطاً عكسياً مؤكداً"
    if asset.startswith("USD"):
        expected = "up" if side == "BUY" else "down"
    elif asset.endswith("USD"):
        expected = "down" if side == "BUY" else "up"
    else:
        expected = None
    allowed = expected is None or dxy_bias == expected
    return allowed, dxy_bias, f"DXY={dxy_bias}; التوقع={expected or 'غير مباشر'}"


def save_signal(chat_id: int, sig: Signal) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO signals(chat_id,asset,side,price,rsi,pattern,stop_distance,risk_pct,observed_at,dxy_bias,correlation_note,news_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, sig.asset, sig.side, sig.price, sig.rsi, sig.pattern, sig.stop_distance, RISK_PCT, sig.observed_at, sig.dxy_bias, sig.correlation_note, sig.news_note),
        )
        return int(cur.lastrowid)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    # A continuously rising series has zero losses; its RSI is 100, not NaN.
    rs = gain / loss.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


def _normalize_live_payload(symbol: str, payload: dict) -> tuple[pd.DataFrame, str]:
    """Validate the broker bridge contract before indicators use the candles."""
    if str(payload.get("symbol", symbol)).upper() != symbol.upper():
        raise ValueError("live_symbol_mismatch")
    candles = payload.get("candles")
    if not isinstance(candles, list) or len(candles) < 30:
        raise ValueError("live_insufficient_candles")
    frame = pd.DataFrame(candles)
    required = {"time", "open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        raise ValueError("live_schema_missing")
    frame = frame.rename(columns={"time": "Time", "open": "Open", "high": "High", "low": "Low", "close": "Close"})
    for column in ["Open", "High", "Low", "Close"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    if len(frame) < 30:
        raise ValueError("live_insufficient_valid_candles")
    observed = pd.Timestamp(frame["Time"].iloc[-1]).to_pydatetime()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
    if age > timedelta(minutes=MAX_AGE_MIN):
        raise ValueError(f"live_stale_data:{int(age.total_seconds()/60)}m")
    frame = frame.set_index("Time")
    frame.attrs["bid"] = payload.get("bid")
    frame.attrs["ask"] = payload.get("ask")
    frame.attrs["source"] = payload.get("source", "broker_live_feed")
    return frame, observed.isoformat()


def fetch_live(symbol: str) -> tuple[pd.DataFrame, str]:
    if not BROKER_FEED_URL:
        raise RuntimeError("BROKER_FEED_URL is not configured; refusing public-price fallback")
    separator = "&" if "?" in BROKER_FEED_URL else "?"
    url = f"{BROKER_FEED_URL}{separator}symbol={symbol}&interval={YF_INTERVAL}&period={YF_PERIOD}"
    headers = {"User-Agent": "smart-trading-telegram-bot/1.0", "Accept": "application/json"}
    if BROKER_FEED_TOKEN:
        headers["Authorization"] = f"Bearer {BROKER_FEED_TOKEN}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=BROKER_FEED_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return _normalize_live_payload(symbol, payload)


def fetch(symbol: str) -> tuple[pd.DataFrame, str]:
    if PRICE_PROVIDER in {"broker_http", "mt5_bridge", "broker"} and BROKER_FEED_URL:
        return fetch_live(symbol)
    if PRICE_PROVIDER != "yahoo" and not ALLOW_PUBLIC_FALLBACK:
        raise RuntimeError("no approved live broker feed configured")
    data = yf.download(symbol, period=YF_PERIOD, interval=YF_INTERVAL, auto_adjust=False, progress=False, threads=False)
    if data is None or data.empty:
        raise ValueError("no_data")
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    if len(data) < 30:
        raise ValueError("insufficient_data")
    idx = data.index[-1]
    observed = pd.Timestamp(idx).to_pydatetime()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
    if age > timedelta(minutes=MAX_AGE_MIN):
        raise ValueError(f"stale_data:{int(age.total_seconds()/60)}m")
    data.attrs["source"] = "Yahoo Finance public/reference feed"
    return data, observed.isoformat()


def adaptive_features(data: pd.DataFrame) -> dict[str, bool | float]:
    close = data["Close"].astype(float)
    middle = close.rolling(20).mean()
    deviation = close.rolling(20).std(ddof=0)
    upper = middle + 2 * deviation
    lower = middle - 2 * deviation
    current_rsi = rsi(close)
    prev, last = data.iloc[-2], data.iloc[-1]
    rsi_prev, rsi_last = float(current_rsi.iloc[-2]), float(current_rsi.iloc[-1])
    bullish_sweep = float(last.Low) < float(prev.Low) and float(last.Close) > float(prev.Low)
    bearish_sweep = float(last.High) > float(prev.High) and float(last.Close) < float(prev.High)
    bullish_divergence = float(last.Low) < float(prev.Low) and rsi_last > rsi_prev
    bearish_divergence = float(last.High) > float(prev.High) and rsi_last < rsi_prev
    return {
        "ema9": float(close.ewm(span=9, adjust=False).mean().iloc[-1]),
        "ema21": float(close.ewm(span=21, adjust=False).mean().iloc[-1]),
        "ema50": float(close.ewm(span=50, adjust=False).mean().iloc[-1]),
        "upper": float(upper.iloc[-1]),
        "lower": float(lower.iloc[-1]),
        "bullish_sweep": bullish_sweep,
        "bearish_sweep": bearish_sweep,
        "bullish_divergence": bullish_divergence,
        "bearish_divergence": bearish_divergence,
        "bullish_overextension": float(last.Close) <= float(lower.iloc[-1]),
        "bearish_overextension": float(last.Close) >= float(upper.iloc[-1]),
    }


def analyze(asset: str, symbol: str, dxy_data: Optional[pd.DataFrame] = None) -> Signal | None:
    data, observed = fetch(symbol)
    close = data["Close"].astype(float)
    data["rsi"] = rsi(close)
    data["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    data["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False).mean()
    features = adaptive_features(data)
    prev, last = data.iloc[-2], data.iloc[-1]
    body_prev = abs(float(prev.Close - prev.Open))
    body_last = abs(float(last.Close - last.Open))
    bullish_engulf = prev.Close < prev.Open and last.Close > last.Open and last.Open <= prev.Close and last.Close >= prev.Open and body_last >= body_prev
    bearish_engulf = prev.Close > prev.Open and last.Close < last.Open and last.Open >= prev.Close and last.Close <= prev.Open and body_last >= body_prev
    current_rsi = float(data["rsi"].iloc[-1])
    side, pattern = None, None
    bullish_trend = float(last.Close) > features["ema21"] > features["ema50"]
    bearish_trend = float(last.Close) < features["ema21"] < features["ema50"]
    # The RSI bands are deliberately asymmetric to avoid treating every
    # engulfing candle as a signal in the middle of a range.
    if bullish_engulf and bullish_trend and 30 <= current_rsi <= 48:
        side, pattern = "BUY", "Bullish Engulfing"
    elif bearish_engulf and bearish_trend and 52 <= current_rsi <= 70:
        side, pattern = "SELL", "Bearish Engulfing"
    if not side:
        return None
    news_allowed, news_note = news_guard(asset)
    if not news_allowed:
        log.info("%s blocked by news guard: %s", asset, news_note)
        return None
    if news_note.startswith("ADAPTIVE:"):
        adaptive_buy = bool(features["bullish_sweep"] and features["bullish_overextension"] and features["bullish_divergence"] and bullish_trend)
        adaptive_sell = bool(features["bearish_sweep"] and features["bearish_overextension"] and features["bearish_divergence"] and bearish_trend)
        if (side == "BUY" and not adaptive_buy) or (side == "SELL" and not adaptive_sell):
            log.info("%s rejected: news-adaptive confluence incomplete", asset)
            return None
        pattern += " + News Liquidity Sweep"
    correlation_allowed, dxy_bias, correlation_note = dxy_guard(asset, side, dxy_data)
    if not correlation_allowed:
        log.info("%s blocked by DXY guard: %s", asset, correlation_note)
        return None
    live_bid = pd.to_numeric(data.attrs.get("bid"), errors="coerce")
    live_ask = pd.to_numeric(data.attrs.get("ask"), errors="coerce")
    if side == "BUY" and pd.notna(live_ask):
        price = float(live_ask)
    elif side == "SELL" and pd.notna(live_bid):
        price = float(live_bid)
    else:
        price = float(last.Close)
    candle_range = max(float(last.High - last.Low), price * 0.001)
    confidence = "high" if (current_rsi <= 40 or current_rsi >= 60) else "medium"
    source = str(data.attrs.get("source", "Yahoo Finance delayed/read-only"))
    return Signal(asset, side, price, current_rsi, pattern, candle_range, confidence, observed,
                  source=source,
                  dxy_bias=dxy_bias, correlation_note=correlation_note, news_note=news_note)


def signal_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(TRADE_STYLES["scalp"]["label"], callback_data=f"targets:{signal_id}:scalp")],
        [InlineKeyboardButton(TRADE_STYLES["medium"]["label"], callback_data=f"targets:{signal_id}:medium")],
        [InlineKeyboardButton(TRADE_STYLES["long"]["label"], callback_data=f"targets:{signal_id}:long")],
        [InlineKeyboardButton("🔄 تحديث التحليل", callback_data=f"refresh:{signal_id}"), InlineKeyboardButton("🔔 التنبيهات", callback_data=f"alerts:{signal_id}")],
        [InlineKeyboardButton("💰 حجز الأرباح (خطة)", callback_data=f"reserve:{signal_id}")],
        [InlineKeyboardButton("🛡️ الارتباط والأخبار", callback_data=f"context:{signal_id}")],
        [InlineKeyboardButton("⭐ قيّم الإشارة", callback_data=f"rate:{signal_id}")],
    ])


def rating_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(str(value), callback_data=f"rating:{signal_id}:{value}") for value in range(1, 6)
    ]])


def management_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تحديث التحليل", callback_data=f"refresh:{signal_id}"), InlineKeyboardButton("🔔 التنبيهات", callback_data=f"alerts:{signal_id}")],
        [InlineKeyboardButton("💰 حجز الأرباح (خطة)", callback_data=f"reserve:{signal_id}"), InlineKeyboardButton("⭐ تقييم", callback_data=f"rate:{signal_id}")],
        [InlineKeyboardButton("🛡️ الارتباط والأخبار", callback_data=f"context:{signal_id}")],
    ])


def format_signal(sig: Signal, balance: Optional[float]) -> str:
    risk_amount = (balance or 0) * RISK_PCT / 100
    units = (risk_amount / (sig.stop_distance * PRICE_VALUE_PER_UNIT)) if risk_amount > 0 else None
    lots = (units / CONTRACT_UNITS_PER_LOT) if units else None
    sizing = (f"حجم نظري: {units:.2f} وحدة ≈ {lots:.4f} لوت" if units else "الحجم النظري: أدخل الرصيد عبر /setbalance")
    return (f"فرصة تحليلية جديدة — {sig.asset}\n الاتجاه: {sig.side}\n السعر المرجعي: {sig.price:.6g}\n"
            f"RSI: {sig.rsi:.2f} | النموذج: {sig.pattern}\nوقف نموذجي: {sig.stop_distance:.6g}\n"
            f"مخاطرة النموذج: {RISK_PCT:.2f}% | {sizing}\n"
            f"الثقة: {sig.confidence}\nDXY: {sig.dxy_bias}\nوقت القراءة UTC: {sig.observed_at}\nالمصدر: {sig.source}\n"
            "اختر نوع الصفقة لحساب SL وTP1 وTP2 وTP3 ديناميكياً:\n"
            "تنبيه: الأسعار مرجعية وليست MT5 تنفيذية؛ تحقق من السعر والسبريد بنفسك.\n\n" + SIGNATURE)


def format_targets(asset: str, side: str, entry: float, candle_range: float, style_key: str) -> str:
    style = TRADE_STYLES[style_key]
    sl_distance = candle_range * style["sl"]
    tp_distances = [candle_range * factor for factor in style["tp"]]
    if side == "BUY":
        stop = entry - sl_distance
        targets = [entry + distance for distance in tp_distances]
    else:
        stop = entry + sl_distance
        targets = [entry - distance for distance in tp_distances]
    return (f"خطة {style['label']} — {asset}\nالاتجاه: {side}\n"
            f"سعر الدخول المرجعي: {entry:.6g}\nوقف الخسارة SL: {stop:.6g}\n"
            f"TP1: {targets[0]:.6g}\nTP2: {targets[1]:.6g}\nTP3: {targets[2]:.6g}\n"
            f"نطاق الشمعة: {candle_range:.6g}\nمضاعف الوقف: {style['sl']:.2f}x\n"
            "المستويات حسابية مرجعية وليست أوامر مرسلة أو توصية مالية. تحقق من السعر والسبريد وإدارة رأس المال قبل أي قرار.")


def scan_assets() -> list[Signal]:
    out = []
    try:
        dxy_data, _ = fetch(DXY_SYMBOL)
    except Exception as exc:
        log.warning("DXY shield unavailable; scan blocked: %s", exc)
        return out
    for asset, symbol in YAHOO_SYMBOLS.items():
        try:
            sig = analyze(asset, symbol, dxy_data)
            if sig:
                out.append(sig)
        except Exception as e:
            log.debug("%s unavailable: %s", asset, e)
    return out

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    await update.message.reply_text("مرحباً. هذا بوت تحليل وتنبيهات فقط. استخدم /help ثم /setbalance 1000 لحساب المخاطرة النظرية.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("الأوامر: /setbalance <USD>، /scan، /watchlist، /analytics، /report، /performance، /pause، /resume. البيانات من Yahoo Finance وقد تكون مرجعية.")

async def setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    try: value = float(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("الاستخدام: /setbalance 1000"); return
    if value <= 0: await update.message.reply_text("يجب أن يكون الرصيد موجباً."); return
    with db() as c: c.execute("UPDATE users SET balance=? WHERE chat_id=?", (value, update.effective_chat.id))
    await update.message.reply_text(f"تم حفظ رصيد الجلسة: {value:.2f}. لن يُستخدم لتنفيذ أي صفقة.")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    await update.message.reply_text("جاري مسح الأصول المتاحة والتحقق من حداثة البيانات...")
    signals = await asyncio.to_thread(scan_assets)
    with db() as c: balance = c.execute("SELECT balance FROM users WHERE chat_id=?", (update.effective_chat.id,)).fetchone()[0]
    if not signals: await update.message.reply_text("لم تظهر إشارة مكتملة الآن، أو أن بعض البيانات غير متاحة/قديمة."); return
    for sig in signals[:10]:
        signal_id = save_signal(update.effective_chat.id, sig)
        await update.message.reply_text(format_signal(sig, balance), reply_markup=signal_keyboard(signal_id))


async def targets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, signal_id_text, style_key = query.data.split(":", 2)
        signal_id = int(signal_id_text)
        if style_key not in TRADE_STYLES:
            raise ValueError("unknown_style")
    except (AttributeError, ValueError):
        await query.message.reply_text("تعذر قراءة اختيار الصفقة.")
        return
    with db() as c:
        row = c.execute(
            "SELECT chat_id,asset,side,price,stop_distance FROM signals WHERE id=?",
            (signal_id,),
        ).fetchone()
    if not row or row[0] != query.message.chat_id:
        await query.message.reply_text("هذه الإشارة غير متاحة لهذه المحادثة.")
        return
    _, asset, side, entry, candle_range = row
    with db() as c:
        c.execute("UPDATE signals SET selected_style=? WHERE id=?", (style_key, signal_id))
    await query.message.reply_text(format_targets(asset, side, float(entry), float(candle_range), style_key), reply_markup=management_keyboard(signal_id))


async def management_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        action, signal_id_text = query.data.split(":", 1)
        signal_id = int(signal_id_text)
    except (AttributeError, ValueError):
        await query.message.reply_text("تعذر قراءة الإجراء.")
        return
    with db() as c:
        row = c.execute("SELECT chat_id,asset,side,price,stop_distance FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not row or row[0] != query.message.chat_id:
        await query.message.reply_text("هذه الإشارة غير متاحة لهذه المحادثة.")
        return
    if action == "context":
        with db() as c:
            context_row = c.execute("SELECT dxy_bias,correlation_note,news_note FROM signals WHERE id=?", (signal_id,)).fetchone()
        dxy_bias, correlation_note, news_note = context_row or ("unknown", "غير متاح", "غير متاح")
        await query.message.reply_text(
            f"درع السوق — {row[1]}\nDXY: {dxy_bias or 'unknown'}\nالارتباط: {correlation_note or 'غير متاح'}\nالأخبار: {news_note or 'غير متاح'}\n\nهذه قراءة مرجعية وليست ضماناً ضد الانعكاس.",
            reply_markup=management_keyboard(signal_id),
        )
    elif action == "reserve":
        with db() as c:
            c.execute("UPDATE signals SET profit_reserved=1,status='profit_plan' WHERE id=?", (signal_id,))
        await query.message.reply_text("تم تسجيل خطة حجز الأرباح لهذه الإشارة. هذا إجراء تذكيري فقط، ولم تُرسل أي صفقة أو أمر.", reply_markup=management_keyboard(signal_id))
    elif action == "alerts":
        with db() as c:
            current = c.execute("SELECT alerts_enabled FROM users WHERE chat_id=?", (query.message.chat_id,)).fetchone()
            enabled = 0 if current and current[0] else 1
            c.execute("UPDATE users SET alerts_enabled=? WHERE chat_id=?", (enabled, query.message.chat_id))
        await query.message.reply_text("تم تفعيل التنبيهات الدورية." if enabled else "تم إيقاف التنبيهات الدورية.", reply_markup=management_keyboard(signal_id))
    elif action == "rate":
        await query.message.reply_text("اختر تقييم الإشارة من 1 إلى 5:", reply_markup=rating_keyboard(signal_id))
    elif action == "refresh":
        asset = row[1]
        try:
            dxy_data, _ = await asyncio.to_thread(fetch, DXY_SYMBOL)
            fresh = await asyncio.to_thread(analyze, asset, YAHOO_SYMBOLS[asset], dxy_data)
        except Exception as exc:
            log.info("refresh failed for %s: %s", asset, exc)
            fresh = None
        if not fresh:
            await query.message.reply_text(f"لا توجد قراءة مكتملة جديدة لـ {asset} أو أن البيانات غير متاحة/قديمة.")
            return
        new_id = save_signal(query.message.chat_id, fresh)
        await query.message.reply_text(format_signal(fresh, None), reply_markup=signal_keyboard(new_id))


async def rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم حفظ التقييم")
    try:
        _, signal_id_text, value_text = query.data.split(":", 2)
        signal_id, value = int(signal_id_text), int(value_text)
        if value not in range(1, 6):
            raise ValueError
    except (AttributeError, ValueError):
        await query.message.reply_text("تقييم غير صالح.")
        return
    with db() as c:
        row = c.execute("SELECT chat_id FROM signals WHERE id=?", (signal_id,)).fetchone()
        if not row or row[0] != query.message.chat_id:
            await query.message.reply_text("هذه الإشارة غير متاحة لهذه المحادثة.")
            return
        c.execute("UPDATE signals SET rating=? WHERE id=?", (value, signal_id))
    await query.message.reply_text(f"تم حفظ تقييمك: {value}/5. سيظهر ضمن الذاكرة التراكمية.")

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"النطاق: {len(YAHOO_SYMBOLS)} رمز MT5 قياسي (أزواج رئيسية وتقاطعّية، XAUUSD، XAGUSD، BTCUSD). مزود Yahoo للقراءة فقط وقد تختلف الأسعار عن وسيط MT5.")

async def performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    with db() as c:
        total, open_count = c.execute("SELECT COUNT(*), SUM(status='open') FROM signals WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
        resolved = c.execute("SELECT COUNT(*), AVG(outcome) FROM signals WHERE chat_id=? AND status='resolved'", (update.effective_chat.id,)).fetchone()
        rated, avg_rating = c.execute("SELECT COUNT(rating), AVG(rating) FROM signals WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
        plans = c.execute("SELECT COUNT(*) FROM signals WHERE chat_id=? AND profit_reserved=1", (update.effective_chat.id,)).fetchone()[0]
    await update.message.reply_text(f"الذاكرة الحية\nإشارات مسجلة: {total}\nمفتوحة: {open_count or 0}\nخطط حجز أرباح: {plans}\nإشارات مقيمة: {rated}\nمتوسط تقييم المستخدم: {avg_rating:.2f}/5\nمتوسط النتيجة المسجلة: {resolved[1] if resolved[1] is not None else 'لا توجد بيانات'}\nالتقييم التعلمي لا يضمن أداءً مستقبلياً.")


async def analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    await update.message.reply_text(
        f"المحرك التكيفي\nالأصول: {len(YAHOO_SYMBOLS)}\nالمؤشرات: EMA9/21/50، RSI، Bollinger Bands، Engulfing\n"
        f"درع DXY: مفعّل\nالأخبار: {'تكيفي' if NEWS_ADAPTIVE else 'حظر تقليدي'}\nالفحص الدوري: كل {INTERVAL_MIN * 60} ثانية\n"
        "قرب الخبر العالي يتطلب Sweep + Bollinger overextension + RSI divergence + اتجاه EMA.\n\n" + SIGNATURE
    )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    with db() as c:
        rows = c.execute("SELECT asset,side,rsi,pattern,dxy_bias,rating,status,observed_at FROM signals WHERE chat_id=? ORDER BY id DESC LIMIT 10", (update.effective_chat.id,)).fetchall()
    if not rows:
        await update.message.reply_text("لا توجد إشارات مسجلة بعد.\n\n" + SIGNATURE)
        return
    lines = ["تقرير آخر الإشارات:"]
    for asset, side, rsi_value, pattern, dxy_bias, rating, status, observed_at in rows:
        lines.append(f"{asset} {side} | RSI {rsi_value:.2f} | DXY {dxy_bias or 'n/a'} | تقييم {rating or '-'} | {status} | {observed_at}")
    await update.message.reply_text("\n".join(lines) + "\n\n" + SIGNATURE)

async def set_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    paused = 1 if update.message.text.startswith('/pause') else 0
    with db() as c: c.execute("UPDATE users SET paused=? WHERE chat_id=?", (paused, update.effective_chat.id))
    await update.message.reply_text("تم إيقاف التنبيهات الدورية." if paused else "تم استئناف التنبيهات الدورية.")

async def periodic_scan(context: ContextTypes.DEFAULT_TYPE):
    signals = await asyncio.to_thread(scan_assets)
    if not signals: return
    with db() as c: users = c.execute("SELECT chat_id,balance FROM users WHERE paused=0 AND alerts_enabled=1").fetchall()
    for chat_id, balance in users:
        for sig in signals[:5]:
            signal_id = save_signal(chat_id, sig)
            try: await context.bot.send_message(chat_id, format_signal(sig, balance), reply_markup=signal_keyboard(signal_id))
            except Exception: log.exception("telegram send failed for %s", chat_id)


def main():
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    db().close()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("help", help_cmd)); app.add_handler(CommandHandler("setbalance", setbalance)); app.add_handler(CommandHandler("scan", scan_cmd)); app.add_handler(CommandHandler("watchlist", watchlist)); app.add_handler(CommandHandler("analytics", analytics_cmd)); app.add_handler(CommandHandler("report", report_cmd)); app.add_handler(CommandHandler("performance", performance)); app.add_handler(CommandHandler("pause", set_pause)); app.add_handler(CommandHandler("resume", set_pause))
    app.add_handler(CallbackQueryHandler(targets_callback, pattern=r"^targets:\d+:(scalp|medium|long)$"))
    app.add_handler(CallbackQueryHandler(rating_callback, pattern=r"^rating:\d+:[1-5]$"))
    app.add_handler(CallbackQueryHandler(management_callback, pattern=r"^(reserve|alerts|rate|refresh|context):\d+$"))
    app.job_queue.run_repeating(periodic_scan, interval=INTERVAL_MIN*60, first=30)
    log.info("bot started; assets=%s interval=%sm timezone=%s", len(YAHOO_SYMBOLS), INTERVAL_MIN, TZ)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
