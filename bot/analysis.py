"""Marktanalyse: Indikatoren, Marktregime, Multi-Timeframe-Trend und Signal-Scores.

Die Scores werden vektorisiert über die komplette Historie berechnet. Live-Betrieb und
Backtest nutzen dadurch exakt denselben Code – der Live-Bot schaut nur auf die letzte
abgeschlossene Kerze.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ind
from .config import StrategyConfig

TF_DELTA = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h", "1d": "1D",
}


def tf_delta(tf: str) -> pd.Timedelta:
    return pd.Timedelta(TF_DELTA[tf])


def _clip01(x):
    return np.clip(x, 0.0, 1.0)


def trend_bias(htf: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Übergeordneter Trend: +1 bullish, -1 bearish, 0 neutral."""
    close = htf["close"]
    fast = ind.ema(close, cfg.ema_slow)
    slow = ind.ema(close, cfg.ema_trend)
    st = ind.supertrend(htf)["direction"]
    bull = (close > slow) & (fast > slow) & (st > 0)
    bear = (close < slow) & (fast < slow) & (st < 0)
    bias = pd.Series(np.where(bull, 1, np.where(bear, -1, 0)), index=htf.index)
    bias[slow.isna()] = 0
    strength = ((fast - slow) / slow).abs().fillna(0)
    return pd.DataFrame({"htf_bias": bias, "htf_strength": strength})


def align_htf(entry: pd.DataFrame, htf_feat: pd.DataFrame, entry_tf: str, trend_tf: str) -> pd.DataFrame:
    """HTF-Werte auf die Entry-Kerzen legen – nur bereits ABGESCHLOSSENE HTF-Kerzen (kein Lookahead)."""
    left = pd.DataFrame({"close_time": entry.index + tf_delta(entry_tf)})
    right = htf_feat.copy()
    right["close_time"] = right.index + tf_delta(trend_tf)
    merged = pd.merge_asof(left, right.sort_values("close_time"), on="close_time", direction="backward")
    merged.index = entry.index
    return merged.drop(columns="close_time").fillna({"htf_bias": 0, "htf_strength": 0})


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = TF_DELTA[tf]
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna()


def compute_features(entry: pd.DataFrame, htf: pd.DataFrame | None, cfg: StrategyConfig,
                     entry_tf: str, trend_tf: str) -> pd.DataFrame:
    """Berechnet alle Indikatoren + Long-/Short-Scores pro Kerze."""
    df = entry.copy()
    c = df["close"]
    df["ema_fast"] = ind.ema(c, cfg.ema_fast)
    df["ema_slow"] = ind.ema(c, cfg.ema_slow)
    df["ema_trend"] = ind.ema(c, cfg.ema_trend)
    df["rsi"] = ind.rsi(c, cfg.rsi_period)
    df["atr"] = ind.atr(df, cfg.atr_period)
    df["atr_pct"] = df["atr"] / c
    m = ind.macd(c)
    df["macd_hist"] = m["hist"]
    bb = ind.bollinger(c)
    df["bb_pct_b"], df["bb_width"], df["bb_mid"] = bb["pct_b"], bb["width"], bb["mid"]
    a = ind.adx(df, 14)
    df["adx"], df["plus_di"], df["minus_di"] = a["adx"], a["plus_di"], a["minus_di"]
    st = ind.supertrend(df)
    df["st_dir"] = st["direction"]
    sr = ind.stoch_rsi(c)
    df["stoch_k"], df["stoch_d"] = sr["k"], sr["d"]
    df["vol_ratio"] = df["volume"] / ind.sma(df["volume"], 20)
    df["vwap"] = ind.vwap(df)
    # Volatilitäts-Perzentil: extreme Volatilität (News, Crash) → nicht handeln
    df["atr_rank"] = df["atr_pct"].rolling(300, min_periods=50).rank(pct=True)

    if htf is not None and len(htf) > 0:
        df = df.join(align_htf(df, trend_bias(htf, cfg), entry_tf, trend_tf))
    else:
        df["htf_bias"], df["htf_strength"] = 0, 0.0

    trending = df["adx"] >= cfg.adx_trend_min
    df["regime"] = np.where(df["atr_rank"] > 0.98, "volatile", np.where(trending, "trend", "range"))

    # ---------- Trendfolge-Score ----------
    dist_atr = (c - df["ema_fast"]) / df["atr"]           # wie weit vom EMA entfernt (in ATR)
    hist_rising = df["macd_hist"].diff() > 0
    vol_score = _clip01((df["vol_ratio"] - 0.8) / 0.7)

    long_t = (
        0.20 * ((df["ema_fast"] > df["ema_slow"]).astype(float) * (0.5 + 0.5 * (c > df["ema_fast"])))
        + 0.15 * (df["st_dir"] > 0)
        + 0.15 * ((df["macd_hist"] > 0) * (0.6 + 0.4 * hist_rising))
        + 0.15 * np.select([df["rsi"].between(50, 68), df["rsi"].between(45, 50), df["rsi"].between(68, 75)], [1.0, 0.5, 0.4], 0.0)
        + 0.10 * vol_score
        + 0.10 * _clip01(1 - (dist_atr.clip(lower=0) - 0.5) / 2.5)
        + 0.15 * (df["htf_bias"] > 0)
    )
    short_t = (
        0.20 * ((df["ema_fast"] < df["ema_slow"]).astype(float) * (0.5 + 0.5 * (c < df["ema_fast"])))
        + 0.15 * (df["st_dir"] < 0)
        + 0.15 * ((df["macd_hist"] < 0) * (0.6 + 0.4 * ~hist_rising))
        + 0.15 * np.select([df["rsi"].between(32, 50), df["rsi"].between(50, 55), df["rsi"].between(25, 32)], [1.0, 0.5, 0.4], 0.0)
        + 0.10 * vol_score
        + 0.10 * _clip01(1 - ((-dist_atr).clip(lower=0) - 0.5) / 2.5)
        + 0.15 * (df["htf_bias"] < 0)
    )
    if cfg.require_trend_alignment:
        long_t = long_t.where(df["htf_bias"] > 0, 0.0)
        short_t = short_t.where(df["htf_bias"] < 0, 0.0)

    # ---------- Mean-Reversion-Score (Seitwärtsmarkt) ----------
    stoch_up = (df["stoch_k"] > df["stoch_d"]) & (df["stoch_k"] < 25)
    stoch_dn = (df["stoch_k"] < df["stoch_d"]) & (df["stoch_k"] > 75)
    long_mr = (
        0.35 * np.select([df["bb_pct_b"] <= 0, df["bb_pct_b"] <= 0.1], [1.0, 0.6], 0.0)
        + 0.30 * np.select([df["rsi"] < 30, df["rsi"] < 36], [1.0, 0.5], 0.0)
        + 0.20 * stoch_up
        + 0.15 * (df["htf_bias"] >= 0)
    )
    short_mr = (
        0.35 * np.select([df["bb_pct_b"] >= 1, df["bb_pct_b"] >= 0.9], [1.0, 0.6], 0.0)
        + 0.30 * np.select([df["rsi"] > 70, df["rsi"] > 64], [1.0, 0.5], 0.0)
        + 0.20 * stoch_dn
        + 0.15 * (df["htf_bias"] <= 0)
    )
    if not cfg.allow_mean_reversion:
        long_mr = long_mr * 0
        short_mr = short_mr * 0

    is_trend = df["regime"] == "trend"
    is_range = df["regime"] == "range"
    df["long_score"] = np.where(is_trend, long_t, np.where(is_range, long_mr, 0.0))
    df["short_score"] = np.where(is_trend, short_t, np.where(is_range, short_mr, 0.0))
    df["setup"] = np.where(is_trend, "trend", np.where(is_range, "mean_reversion", "none"))

    warm = df[["ema_trend", "adx", "atr", "rsi", "stoch_d", "vol_ratio"]].isna().any(axis=1)
    df.loc[warm, ["long_score", "short_score"]] = 0.0
    df[["long_score", "short_score"]] = df[["long_score", "short_score"]].astype(float).round(4)
    return df


@dataclass
class MarketSnapshot:
    """Lesbare Zusammenfassung der letzten abgeschlossenen Kerze."""
    symbol: str
    time: pd.Timestamp
    price: float
    regime: str
    htf_bias: int
    adx: float
    rsi: float
    atr_pct: float
    macd_hist: float
    bb_pct_b: float
    vol_ratio: float
    long_score: float
    short_score: float
    setup: str
    support: list[float] = field(default_factory=list)
    resistance: list[float] = field(default_factory=list)
    funding_rate: float | None = None

    def summary(self) -> str:
        bias = {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[int(self.htf_bias)]
        fr = f" | Funding {self.funding_rate * 100:+.4f}%" if self.funding_rate is not None else ""
        sup = ", ".join(f"{x:.6g}" for x in self.support) or "-"
        res = ", ".join(f"{x:.6g}" for x in self.resistance) or "-"
        return (
            f"{self.symbol} @ {self.price:.6g} ({self.time:%Y-%m-%d %H:%M} UTC)\n"
            f"  Trend (HTF): {bias} | Regime: {self.regime} | ADX {self.adx:.1f} | RSI {self.rsi:.1f}\n"
            f"  Volatilität (ATR): {self.atr_pct * 100:.2f}% | MACD-Hist {self.macd_hist:+.4g} | "
            f"BB%B {self.bb_pct_b:.2f} | Volumen x{self.vol_ratio:.2f}{fr}\n"
            f"  Support: {sup} | Resistance: {res}\n"
            f"  Score LONG {self.long_score:.2f} | SHORT {self.short_score:.2f} | Setup: {self.setup}"
        )


def snapshot(symbol: str, feat: pd.DataFrame, funding_rate: float | None = None) -> MarketSnapshot:
    row = feat.iloc[-1]
    sup, res = ind.swing_levels(feat.tail(200))
    return MarketSnapshot(
        symbol=symbol, time=feat.index[-1], price=float(row["close"]), regime=str(row["regime"]),
        htf_bias=int(row["htf_bias"]), adx=float(row["adx"]), rsi=float(row["rsi"]),
        atr_pct=float(row["atr_pct"]), macd_hist=float(row["macd_hist"]), bb_pct_b=float(row["bb_pct_b"]),
        vol_ratio=float(row["vol_ratio"]), long_score=float(row["long_score"]),
        short_score=float(row["short_score"]), setup=str(row["setup"]),
        support=sup, resistance=res, funding_rate=funding_rate,
    )
