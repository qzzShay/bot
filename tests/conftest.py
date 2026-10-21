import numpy as np
import pandas as pd
import pytest

from bot.config import Config


def make_ohlcv(n: int = 6000, seed: int = 1, start_price: float = 30000.0, freq: str = "15min") -> pd.DataFrame:
    """Synthetische Kurse mit wechselnden Trend- und Seitwärtsphasen."""
    rng = np.random.default_rng(seed)
    drift = np.zeros(n)
    i = 0
    while i < n:
        length = rng.integers(200, 800)
        drift[i : i + length] = rng.choice([0.0006, -0.0006, 0.0, 0.0])
        i += length
    rets = drift + rng.normal(0, 0.004, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, 0.003, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = rng.lognormal(10, 0.5, n)
    idx = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


@pytest.fixture
def ohlcv():
    return make_ohlcv()


@pytest.fixture
def cfg():
    c = Config()
    c.validate()
    return c
