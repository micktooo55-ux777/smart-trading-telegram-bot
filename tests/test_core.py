import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
import bot
import pandas as pd

def test_asset_coverage():
    assert 'EURUSD' in bot.YAHOO_SYMBOLS
    assert 'EURJPY' in bot.YAHOO_SYMBOLS
    assert bot.YAHOO_SYMBOLS['XAUUSD'] == 'XAUUSD=X'
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
    assert callback_data == ['targets:42:scalp', 'targets:42:medium', 'targets:42:long']
