import numpy as np
import pandas as pd

from bot import indicators as ind


def test_rsi_bounds_and_extremes(ohlcv):
    r = ind.rsi(ohlcv["close"]).dropna()
    assert r.between(0, 100).all()
    up = pd.Series(np.arange(1, 100, dtype=float))
    assert ind.rsi(up).iloc[-1] == 100


def test_ema_matches_pandas(ohlcv):
    e = ind.ema(ohlcv["close"], 20)
    ref = ohlcv["close"].ewm(span=20, adjust=False).mean()
    assert np.allclose(e.dropna(), ref[e.notna()])


def test_atr_positive(ohlcv):
    a = ind.atr(ohlcv).dropna()
    assert (a > 0).all()


def test_supertrend_direction_values(ohlcv):
    st = ind.supertrend(ohlcv)
    assert set(np.unique(st["direction"].iloc[20:])) <= {-1.0, 1.0}


def test_adx_range(ohlcv):
    a = ind.adx(ohlcv).dropna()
    assert a["adx"].between(0, 100).all()


def test_bollinger_order(ohlcv):
    b = ind.bollinger(ohlcv["close"]).dropna()
    assert (b["upper"] >= b["mid"]).all() and (b["mid"] >= b["lower"]).all()
