from __future__ import annotations
import asyncio
import logging
import os
import sqlite3
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
INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
YF_PERIOD = os.getenv("YFINANCE_PERIOD", "5d")
YF_INTERVAL = os.getenv("YFINANCE_INTERVAL", "15m")
MAX_AGE_MIN = int(os.getenv("MAX_DATA_AGE_MINUTES", "45"))
RISK_PCT = float(os.getenv("DEFAULT_RISK_PERCENT", "1.0"))
DB_PATH = ROOT / "data" / "bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smart-bot")

CURRENCIES = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
YAHOO_SYMBOLS = {f"{a}{b}": f"{a}{b}=X" for a in CURRENCIES for b in CURRENCIES if a != b}
YAHOO_SYMBOLS.update({"XAUUSD": "XAUUSD=X", "XAGUSD": "XAGUSD=X", "BTCUSD": "BTC-USD"})

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
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_user(chat_id: int):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(chat_id, balance, created_at) VALUES (?, NULL, ?)", (chat_id, now_iso()))


def save_signal(chat_id: int, sig: Signal) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO signals(chat_id,asset,side,price,rsi,pattern,stop_distance,risk_pct,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (chat_id, sig.asset, sig.side, sig.price, sig.rsi, sig.pattern, sig.stop_distance, RISK_PCT, sig.observed_at),
        )
        return int(cur.lastrowid)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    # A continuously rising series has zero losses; its RSI is 100, not NaN.
    rs = gain / loss.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


def fetch(symbol: str) -> tuple[pd.DataFrame, str]:
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
    return data, observed.isoformat()


def analyze(asset: str, symbol: str) -> Signal | None:
    data, observed = fetch(symbol)
    close = data["Close"].astype(float)
    data["rsi"] = rsi(close)
    prev, last = data.iloc[-2], data.iloc[-1]
    body_prev = abs(float(prev.Close - prev.Open))
    body_last = abs(float(last.Close - last.Open))
    bullish_engulf = prev.Close < prev.Open and last.Close > last.Open and last.Open <= prev.Close and last.Close >= prev.Open and body_last >= body_prev
    bearish_engulf = prev.Close > prev.Open and last.Close < last.Open and last.Open >= prev.Close and last.Close <= prev.Open and body_last >= body_prev
    current_rsi = float(data["rsi"].iloc[-1])
    side, pattern = None, None
    if bullish_engulf and current_rsi <= 45:
        side, pattern = "BUY", "Bullish Engulfing"
    elif bearish_engulf and current_rsi >= 55:
        side, pattern = "SELL", "Bearish Engulfing"
    if not side:
        return None
    price = float(last.Close)
    candle_range = max(float(last.High - last.Low), price * 0.001)
    return Signal(asset, side, price, current_rsi, pattern, candle_range, "medium", observed)


def signal_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(TRADE_STYLES["scalp"]["label"], callback_data=f"targets:{signal_id}:scalp")],
        [InlineKeyboardButton(TRADE_STYLES["medium"]["label"], callback_data=f"targets:{signal_id}:medium")],
        [InlineKeyboardButton(TRADE_STYLES["long"]["label"], callback_data=f"targets:{signal_id}:long")],
    ])


def format_signal(sig: Signal, balance: Optional[float]) -> str:
    risk_amount = (balance or 0) * RISK_PCT / 100
    size = (risk_amount / sig.stop_distance) if risk_amount > 0 else None
    sizing = f"حجم نظري: {size:.4f}" if size else "الحجم النظري: أدخل الرصيد عبر /setbalance"
    return (f"فرصة تحليلية جديدة — {sig.asset}\n الاتجاه: {sig.side}\n السعر المرجعي: {sig.price:.6g}\n"
            f"RSI: {sig.rsi:.2f} | النموذج: {sig.pattern}\nوقف نموذجي: {sig.stop_distance:.6g}\n"
            f"مخاطرة النموذج: {RISK_PCT:.2f}% | {sizing}\n"
            f"الثقة: {sig.confidence}\nوقت القراءة UTC: {sig.observed_at}\n"
            "اختر نوع الصفقة لحساب SL وTP1 وTP2 وTP3 ديناميكياً:\n"
            "تنبيه: هذه إشارة غير تنفيذية وليست ضماناً أو توصية مالية. تحقق من السعر والسبريد بنفسك.")


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
    for asset, symbol in YAHOO_SYMBOLS.items():
        try:
            sig = analyze(asset, symbol)
            if sig:
                out.append(sig)
        except Exception as e:
            log.debug("%s unavailable: %s", asset, e)
    return out

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    await update.message.reply_text("مرحباً. هذا بوت تحليل وتنبيهات فقط. استخدم /help ثم /setbalance 1000 لحساب المخاطرة النظرية.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("الأوامر: /setbalance <USD>، /scan، /watchlist، /performance، /pause، /resume. البيانات من Yahoo Finance وقد تكون متأخرة.")

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
    await query.message.reply_text(format_targets(asset, side, float(entry), float(candle_range), style_key))

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"النطاق: {len(YAHOO_SYMBOLS)} رمزاً ديناميكياً (28 زوجاً رئيسياً/تقاطعيّاً، XAUUSD، XAGUSD، BTCUSD). قد يفشل بعض الرموز حسب تغطية Yahoo.")

async def performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    with db() as c:
        total, open_count = c.execute("SELECT COUNT(*), SUM(status='open') FROM signals WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
        resolved = c.execute("SELECT COUNT(*), AVG(outcome) FROM signals WHERE chat_id=? AND status='resolved'", (update.effective_chat.id,)).fetchone()
    await update.message.reply_text(f"الذاكرة الحية\nإشارات مسجلة: {total}\nمفتوحة: {open_count or 0}\nمغلقة بتقييم: {resolved[0]}\nمتوسط النتيجة المسجلة: {resolved[1] if resolved[1] is not None else 'لا توجد بيانات'}\nالتقييم التعلمي لا يضمن أداءً مستقبلياً.")

async def set_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    paused = 1 if update.message.text.startswith('/pause') else 0
    with db() as c: c.execute("UPDATE users SET paused=? WHERE chat_id=?", (paused, update.effective_chat.id))
    await update.message.reply_text("تم إيقاف التنبيهات الدورية." if paused else "تم استئناف التنبيهات الدورية.")

async def periodic_scan(context: ContextTypes.DEFAULT_TYPE):
    signals = await asyncio.to_thread(scan_assets)
    if not signals: return
    with db() as c: users = c.execute("SELECT chat_id,balance FROM users WHERE paused=0").fetchall()
    for chat_id, balance in users:
        for sig in signals[:5]:
            signal_id = save_signal(chat_id, sig)
            try: await context.bot.send_message(chat_id, format_signal(sig, balance), reply_markup=signal_keyboard(signal_id))
            except Exception: log.exception("telegram send failed for %s", chat_id)


def main():
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    db().close()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("help", help_cmd)); app.add_handler(CommandHandler("setbalance", setbalance)); app.add_handler(CommandHandler("scan", scan_cmd)); app.add_handler(CommandHandler("watchlist", watchlist)); app.add_handler(CommandHandler("performance", performance)); app.add_handler(CommandHandler("pause", set_pause)); app.add_handler(CommandHandler("resume", set_pause))
    app.add_handler(CallbackQueryHandler(targets_callback, pattern=r"^targets:\d+:(scalp|medium|long)$"))
    app.job_queue.run_repeating(periodic_scan, interval=INTERVAL_MIN*60, first=30)
    log.info("bot started; assets=%s interval=%sm timezone=%s", len(YAHOO_SYMBOLS), INTERVAL_MIN, TZ)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
