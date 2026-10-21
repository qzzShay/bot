"""Technische Indikatoren (reines pandas/numpy, keine TA-Lib nötig).

Alle Funktionen erwarten ein DataFrame mit den Spalten open, high, low, close, volume.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's Moving Average (wie in RSI/ATR/ADX verwendet)."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = rma(delta.clip(lower=0), period)
    loss = rma(-delta.clip(upper=0), period)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # Kein Verlust im Fenster → RSI 100
    return out.where(loss != 0, 100.0).where(gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return rma(true_range(df), period)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    dev = close.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + std * dev, mid - std * dev
    width = (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width, "pct_b": pct_b})


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = rma(true_range(df), period)
    plus_di = 100 * rma(plus_dm, period) / tr
    minus_di = 100 * rma(minus_dm, period) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame({"adx": rma(dx, period), "plus_di": plus_di, "minus_di": minus_di})


def stoch_rsi(close: pd.Series, period: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    r = rsi(close, period)
    lo = r.rolling(period).min()
    hi = r.rolling(period).max()
    stoch = 100 * (r - lo) / (hi - lo).replace(0, np.nan)
    k_line = stoch.rolling(k).mean()
    return pd.DataFrame({"k": k_line, "d": k_line.rolling(d).mean()})


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Supertrend. direction: +1 = Aufwärtstrend, -1 = Abwärtstrend."""
    a = atr(df, period).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    final_up = np.full(n, np.nan)
    final_lo = np.full(n, np.nan)
    direction = np.zeros(n)
    line = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(a[i]):
            continue
        if i == 0 or np.isnan(final_up[i - 1]):
            final_up[i], final_lo[i], direction[i] = upper[i], lower[i], 1
        else:
            final_up[i] = upper[i] if (upper[i] < final_up[i - 1] or close[i - 1] > final_up[i - 1]) else final_up[i - 1]
            final_lo[i] = lower[i] if (lower[i] > final_lo[i - 1] or close[i - 1] < final_lo[i - 1]) else final_lo[i - 1]
            if direction[i - 1] == 1:
                direction[i] = -1 if close[i] < final_lo[i] else 1
            else:
                direction[i] = 1 if close[i] > final_up[i] else -1
        line[i] = final_lo[i] if direction[i] == 1 else final_up[i]
    return pd.DataFrame({"line": line, "direction": direction}, index=df.index)


def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["close"].diff()).fillna(0)
    return (sign * df["volume"]).cumsum()


def vwap(df: pd.DataFrame, period: int = 96) -> pd.Series:
    """Rollierender VWAP über `period` Kerzen."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).rolling(period, min_periods=1).sum()
    vol = df["volume"].rolling(period, min_periods=1).sum()
    return pv / vol.replace(0, np.nan)


def swing_levels(df: pd.DataFrame, lookback: int = 5, count: int = 3) -> tuple[list[float], list[float]]:
    """Einfache Support-/Resistance-Erkennung über lokale Hochs/Tiefs (Pivots)."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    res, sup = [], []
    for i in range(lookback, len(df) - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        window_l = lows[i - lookback : i + lookback + 1]
        if highs[i] == window_h.max():
            res.append(float(highs[i]))
        if lows[i] == window_l.min():
            sup.append(float(lows[i]))
    return sup[-count:], res[-count:]
