import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
import bot
import pandas as pd
from datetime import datetime, timedelta, timezone

def test_asset_coverage():
    assert 'EURUSD' in bot.YAHOO_SYMBOLS
    assert 'EURJPY' in bot.YAHOO_SYMBOLS
    assert 'USDCAD' in bot.YAHOO_SYMBOLS
    assert 'CADUSD' not in bot.YAHOO_SYMBOLS
    assert bot.YAHOO_SYMBOLS['XAUUSD'] == 'GC=F'
    assert bot.YAHOO_SYMBOLS['XAGUSD'] == 'SI=F'
    assert bot.YAHOO_SYMBOLS['BTCUSD'] == 'BTC-USD'

def test_rsi_range():
    s = pd.Series(range(1, 40), dtype=float)
    x = bot.rsi(s).dropna()
    assert float(x.iloc[-1]) > 50
    assert float(x.iloc[-1]) <= 100

def test_dynamic_targets_scale_by_style_and_direction():
    scalp = bot.format_targets('EURUSD', 'BUY', 1.1000, 0.0100, 'scalp')
    long = bot.format_targets('EURUSD', 'BUY', 1.1000, 0.0100, 'long')
    assert 'SL: 1.09' in scalp
    assert 'TP1: 1.108' in scalp
    assert 'SL: 1.075' in long
    assert 'TP3: 1.16' in long

    sell = bot.format_targets('XAUUSD', 'SELL', 2000.0, 10.0, 'medium')
    assert 'SL: 2015' in sell
    assert 'TP3: 1965' in sell

def test_keyboard_contains_all_trade_styles():
    keyboard = bot.signal_keyboard(42).inline_keyboard
    callback_data = [button.callback_data for row in keyboard for button in row]
    assert callback_data[:3] == ['targets:42:scalp', 'targets:42:medium', 'targets:42:long']
    assert 'refresh:42' in callback_data
    assert 'alerts:42' in callback_data
    assert 'reserve:42' in callback_data
    assert 'rate:42' in callback_data
    assert 'context:42' in callback_data

def test_dxy_guard_blocks_gold_when_bias_conflicts():
    dxy = pd.DataFrame({'Close': [100.0] * 40 + [101.0] * 10})
    allowed, bias, note = bot.dxy_guard('XAUUSD', 'BUY', dxy)
    assert bias == 'up'
    assert allowed is False
    assert 'DXY=up' in note

def test_news_guard_fails_closed_without_calendar(monkeypatch):
    monkeypatch.setattr(bot, 'NEWS_FILTER_REQUIRED', True)
    monkeypatch.setattr(bot, '_load_news_events', lambda: [])
    allowed, note = bot.news_guard('EURUSD', datetime.now(timezone.utc))
    assert allowed is False
    assert 'تقويم أخبار' in note

def test_live_feed_contract_validates_candles_and_quotes():
    candles = [
        {'time': (datetime.now(timezone.utc) - timedelta(minutes=29-i)).isoformat(), 'open': 1.0, 'high': 1.01, 'low': 0.99, 'close': 1.0 + i / 10000}
        for i in range(30)
    ]
    frame, observed = bot._normalize_live_payload('EURUSD', {
        'symbol': 'EURUSD', 'source': 'test-broker', 'bid': 1.1010, 'ask': 1.1012, 'candles': candles
    })
    assert len(frame) == 30
    assert frame.attrs['bid'] == 1.1010
    assert frame.attrs['ask'] == 1.1012
    assert observed.endswith('+00:00')

def test_learning_settles_tp3_and_updates_weight(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'learning.db')
    with bot.db() as c:
        c.execute("INSERT INTO signals(chat_id,asset,side,price,stop_distance,risk_pct,observed_at,feature_vector,status) VALUES(?,?,?,?,?,?,?,?,?)", (1, 'EURUSD', 'BUY', 1.0, 0.1, 1.0, bot.now_iso(), '{"pattern": 1.0}', 'open'))
    assert bot.settle_open_signals({'EURUSD': 1.46}) == 1
    with bot.db() as c:
        row = c.execute("SELECT status,outcome_stage,outcome_label,outcome_r FROM signals WHERE chat_id=1").fetchone()
        weight = c.execute("SELECT weight,samples FROM filter_weights WHERE name='pattern'").fetchone()
    assert row == ('resolved', 'TP3', 'win', 4.5)
    assert weight[1] == 1
    assert weight[0] > 1.0
