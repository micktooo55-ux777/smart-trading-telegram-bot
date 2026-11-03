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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
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


def format_signal(sig: Signal, balance: Optional[float]) -> str:
    risk_amount = (balance or 0) * RISK_PCT / 100
    size = (risk_amount / sig.stop_distance) if risk_amount > 0 else None
    sizing = f"حجم نظري: {size:.4f}" if size else "الحجم النظري: أدخل الرصيد عبر /setbalance"
    return (f"إشارة تحليلية — {sig.asset}\n الاتجاه: {sig.side}\n السعر المرجعي: {sig.price:.6g}\n"
            f"RSI: {sig.rsi:.2f} | النموذج: {sig.pattern}\nوقف نموذجي: {sig.stop_distance:.6g}\n"
            f"مخاطرة النموذج: {RISK_PCT:.2f}% | {sizing}\n"
            f"الثقة: {sig.confidence}\nوقت القراءة UTC: {sig.observed_at}\n"
            "تنبيه: هذه إشارة غير تنفيذية وليست ضماناً أو توصية مالية. تحقق من السعر والسبريد بنفسك.")


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
        with db() as c: c.execute("INSERT INTO signals(chat_id,asset,side,price,rsi,pattern,stop_distance,risk_pct,observed_at) VALUES(?,?,?,?,?,?,?,?,?)", (update.effective_chat.id,sig.asset,sig.side,sig.price,sig.rsi,sig.pattern,sig.stop_distance,RISK_PCT,sig.observed_at))
        await update.message.reply_text(format_signal(sig, balance))

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
            with db() as c: c.execute("INSERT INTO signals(chat_id,asset,side,price,rsi,pattern,stop_distance,risk_pct,observed_at) VALUES(?,?,?,?,?,?,?,?,?)", (chat_id,sig.asset,sig.side,sig.price,sig.rsi,sig.pattern,sig.stop_distance,RISK_PCT,sig.observed_at))
            try: await context.bot.send_message(chat_id, format_signal(sig, balance))
            except Exception: log.exception("telegram send failed for %s", chat_id)


def main():
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    db().close()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("help", help_cmd)); app.add_handler(CommandHandler("setbalance", setbalance)); app.add_handler(CommandHandler("scan", scan_cmd)); app.add_handler(CommandHandler("watchlist", watchlist)); app.add_handler(CommandHandler("performance", performance)); app.add_handler(CommandHandler("pause", set_pause)); app.add_handler(CommandHandler("resume", set_pause))
    app.job_queue.run_repeating(periodic_scan, interval=INTERVAL_MIN*60, first=30)
    log.info("bot started; assets=%s interval=%sm timezone=%s", len(YAHOO_SYMBOLS), INTERVAL_MIN, TZ)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
