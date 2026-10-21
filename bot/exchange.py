"""Dünne Hülle um ccxt: Verbindung, Marktdaten, Präzision/Limits."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import ccxt
import pandas as pd

from .analysis import tf_delta
from .config import ExchangeConfig

log = logging.getLogger(__name__)


@dataclass
class MarketLimits:
    min_qty: float
    qty_step: float
    min_notional: float
    max_leverage: int | None
    contract_size: float


def retry(fn, *args, attempts: int = 4, **kwargs):
    """Wiederholt Netzwerkfehler mit exponentiellem Backoff."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.DDoSProtection) as e:
            if i == attempts - 1:
                raise
            wait = 2 ** i
            log.warning("Netzwerkfehler (%s), neuer Versuch in %ss", type(e).__name__, wait)
            time.sleep(wait)


class Exchange:
    def __init__(self, cfg: ExchangeConfig, authenticated: bool = True):
        self.cfg = cfg
        if not hasattr(ccxt, cfg.name):
            raise ValueError(f"Unbekannte Börse für ccxt: {cfg.name}")
        params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        if authenticated and cfg.api_key:
            params.update(apiKey=cfg.api_key, secret=cfg.api_secret)
            if cfg.api_password:
                params["password"] = cfg.api_password
        self.ex: ccxt.Exchange = getattr(ccxt, cfg.name)(params)
        # Sandbox nur für authentifizierten Handel. Paper-Trading, Analyse und Backtests
        # nutzen immer die echten Marktdaten der Hauptbörse.
        self.sandbox = cfg.sandbox if authenticated else "none"
        if self.sandbox == "testnet":
            self.ex.set_sandbox_mode(True)
        elif self.sandbox == "demo":
            if hasattr(self.ex, "enable_demo_trading"):
                self.ex.enable_demo_trading(True)
            else:
                self.ex.set_sandbox_mode(True)
        self._markets_loaded = False

    @property
    def id(self) -> str:
        return self.ex.id

    def load_markets(self) -> dict:
        if not self._markets_loaded:
            retry(self.ex.load_markets)
            self._markets_loaded = True
        return self.ex.markets

    def market(self, symbol: str) -> dict:
        self.load_markets()
        if symbol not in self.ex.markets:
            raise ValueError(f"Symbol {symbol} existiert nicht auf {self.id}")
        return self.ex.markets[symbol]

    def limits(self, symbol: str) -> MarketLimits:
        m = self.market(symbol)
        prec = m.get("precision", {}).get("amount")
        if prec is None:
            step = 0.0
        elif self.ex.precisionMode == ccxt.TICK_SIZE:
            step = float(prec)
        else:
            step = 10 ** -int(prec)
        lim = m.get("limits", {})
        max_lev = (lim.get("leverage") or {}).get("max")
        contract = float(m.get("contractSize") or 1.0)
        return MarketLimits(
            # Mengen im Bot sind immer in Basiswährung; Börsenmengen ggf. in Kontrakten
            min_qty=float((lim.get("amount") or {}).get("min") or 0) * contract,
            qty_step=step * contract,
            min_notional=float((lim.get("cost") or {}).get("min") or 0),
            max_leverage=int(max_lev) if max_lev else None,
            contract_size=contract,
        )

    # ---------- Marktdaten ----------
    def fetch_ohlcv(self, symbol: str, timeframe: str, bars: int, since_ms: int | None = None,
                    only_closed: bool = True) -> pd.DataFrame:
        """Lädt Kerzen (mit Paginierung) als DataFrame. Die laufende Kerze wird entfernt."""
        self.load_markets()
        step_ms = int(tf_delta(timeframe).total_seconds() * 1000)
        if since_ms is None:
            since_ms = self.ex.milliseconds() - (bars + 2) * step_ms
        rows: list[list] = []
        cursor = since_ms
        while True:
            batch = retry(self.ex.fetch_ohlcv, symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            last = batch[-1][0]
            if last + step_ms > self.ex.milliseconds() or len(batch) < 2 or len(rows) >= bars + 2:
                break
            cursor = last + step_ms
        if not rows:
            raise RuntimeError(f"Keine Kerzen für {symbol} {timeframe}")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates("ts").sort_values("ts")
        df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        df = df.astype(float)
        if only_closed:
            now = pd.Timestamp.now(tz="UTC")
            df = df[df.index + tf_delta(timeframe) <= now]
        return df.tail(bars)

    def fetch_history(self, symbol: str, timeframe: str, start: str, end: str | None = None) -> pd.DataFrame:
        """Historische Daten für Backtests (start/end als 'YYYY-MM-DD')."""
        self.load_markets()
        since = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        end_ts = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.now(tz="UTC")
        bars = int((end_ts - pd.Timestamp(start, tz="UTC")) / tf_delta(timeframe)) + 1
        df = self.fetch_ohlcv(symbol, timeframe, bars, since_ms=since)
        return df[df.index < end_ts]

    def last_price(self, symbol: str) -> float:
        t = retry(self.ex.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close"))

    def funding_rate(self, symbol: str) -> float | None:
        try:
            fr = retry(self.ex.fetch_funding_rate, symbol)
            v = fr.get("fundingRate")
            return float(v) if v is not None else None
        except Exception as e:  # nicht kritisch
            log.debug("Funding-Rate für %s nicht verfügbar: %s", symbol, e)
            return None

    # ---------- Präzision ----------
    def to_exchange_amount(self, symbol: str, qty: float) -> float:
        """Basiswährungs-Menge → Börsenmenge (Kontrakte) mit korrekter Präzision."""
        contract = self.limits(symbol).contract_size
        return float(self.ex.amount_to_precision(symbol, qty / contract))

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.ex.price_to_precision(symbol, price))
