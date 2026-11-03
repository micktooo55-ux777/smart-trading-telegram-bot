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
