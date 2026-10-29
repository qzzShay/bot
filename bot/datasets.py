"""Historische Binance-Futures-Daten von data.binance.vision (offizielle Archive, kein API-Key, keine Limits)."""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE = "https://data.binance.vision/data/futures/um"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
        "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def to_binance_symbol(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTCUSDT'"""
    return symbol.split(":")[0].replace("/", "")


def _months(start: str, end: str | None) -> list[pd.Timestamp]:
    s = pd.Timestamp(start).to_period("M")
    # end ist exklusiv: end="2026-10-01" lädt bis einschließlich September
    e = ((pd.Timestamp(end) - pd.Timedelta(seconds=1)) if end else pd.Timestamp.now()).to_period("M")
    return [p.to_timestamp() for p in pd.period_range(s, e, freq="M")]


def _get_zip_csv(url: str) -> pd.DataFrame | None:
    r = requests.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    header = 0 if not first[:1].isdigit() else None
    return pd.read_csv(io.StringIO(raw), header=header)


def download_klines(symbol: str, timeframe: str, start: str, end: str | None = None,
                    cache_dir: str = "data/vision") -> pd.DataFrame:
    """Lädt Monatsarchive (und für den laufenden Monat Tagesarchive), mit lokalem Cache."""
    sym = to_binance_symbol(symbol)
    cache = Path(cache_dir) / sym / timeframe
    cache.mkdir(parents=True, exist_ok=True)
    frames = []
    now = pd.Timestamp.now()
    for m in _months(start, end):
        tag = m.strftime("%Y-%m")
        f = cache / f"{tag}.csv"
        if f.exists():
            frames.append(pd.read_csv(f))
            continue
        df = _get_zip_csv(f"{BASE}/monthly/klines/{sym}/{timeframe}/{sym}-{timeframe}-{tag}.zip")
        if df is None:
            # Monat noch nicht archiviert → Tagesdateien
            parts = []
            for d in pd.date_range(m, min(m + pd.offsets.MonthEnd(0), now - pd.Timedelta(days=1)), freq="D"):
                day_file = cache / "daily" / f"{d:%Y-%m-%d}.csv"
                if day_file.exists():
                    parts.append(pd.read_csv(day_file))
                    continue
                part = _get_zip_csv(f"{BASE}/daily/klines/{sym}/{timeframe}/{sym}-{timeframe}-{d:%Y-%m-%d}.zip")
                if part is not None:
                    part = part.iloc[:, :12]
                    part.columns = COLS
                    day_file.parent.mkdir(exist_ok=True)
                    part.to_csv(day_file, index=False)
                    parts.append(part)
            if not parts:
                continue
            df = pd.concat(parts)
            is_complete = False
        else:
            is_complete = True
        df = df.iloc[:, :12]
        df.columns = COLS
        if is_complete:
            df.to_csv(f, index=False)
        frames.append(df)
        log.info("%s %s %s: %d Kerzen", sym, timeframe, tag, len(df))
    if not frames:
        raise RuntimeError(f"Keine Daten für {sym} {timeframe}")
    df = pd.concat(frames).drop_duplicates("open_time").sort_values("open_time")
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    out = df[["open", "high", "low", "close", "volume", "taker_buy_volume"]].astype(float)
    if start:
        out = out[out.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        out = out[out.index < pd.Timestamp(end, tz="UTC")]
    return out


def download_funding(symbol: str, start: str, end: str | None = None, cache_dir: str = "data/vision") -> pd.Series:
    """Historische Funding-Raten (alle 8h bzw. 4h)."""
    sym = to_binance_symbol(symbol)
    cache = Path(cache_dir) / sym / "funding"
    cache.mkdir(parents=True, exist_ok=True)
    frames = []
    for m in _months(start, end):
        tag = m.strftime("%Y-%m")
        f = cache / f"{tag}.csv"
        if f.exists():
            frames.append(pd.read_csv(f))
            continue
        df = _get_zip_csv(f"{BASE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{tag}.zip")
        if df is None:
            continue
        df = df.iloc[:, [0, 2]]
        df.columns = ["calc_time", "funding_rate"]
        df.to_csv(f, index=False)
        frames.append(df)
    if not frames:
        return pd.Series(dtype=float)
    df = pd.concat(frames).drop_duplicates("calc_time").sort_values("calc_time")
    return pd.Series(df["funding_rate"].astype(float).values,
                     index=pd.to_datetime(df["calc_time"].astype("int64"), unit="ms", utc=True))
