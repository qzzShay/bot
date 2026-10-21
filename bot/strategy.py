"""Strategie: aus den Analyse-Features Ein- und Ausstiegssignale ableiten und Stops managen."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import StrategyConfig

LONG, SHORT = "long", "short"


@dataclass
class Signal:
    side: str
    price: float
    stop: float
    take_profit: float
    score: float
    setup: str
    atr: float
    reason: str

    @property
    def risk_per_unit(self) -> float:
        return abs(self.price - self.stop)


@dataclass
class PositionState:
    """Minimaler Zustand einer offenen Position, den das Stop-Management braucht."""
    side: str
    entry: float
    stop: float
    initial_stop: float
    take_profit: float
    best_price: float  # höchster Kurs (long) bzw. tiefster Kurs (short) seit Einstieg

    @property
    def initial_risk(self) -> float:
        return abs(self.entry - self.initial_stop)

    def r_multiple(self, price: float) -> float:
        if self.initial_risk == 0:
            return 0.0
        move = price - self.entry if self.side == LONG else self.entry - price
        return move / self.initial_risk


class Strategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def entry_signal(self, feat: pd.DataFrame, i: int = -1, funding_rate: float | None = None) -> Signal | None:
        """Prüft die Kerze i (Standard: letzte abgeschlossene Kerze) auf ein frisches Einstiegssignal.

        "Frisch" heißt: der Score überschreitet die Schwelle auf DIESER Kerze. Damit steigt der
        Bot nach einem Stop-out nicht sofort wieder in dieselbe Bewegung ein.
        """
        if len(feat) < 2:
            return None
        row, prev = feat.iloc[i], feat.iloc[i - 1]
        th = self.cfg.entry_threshold
        atr = float(row["atr"])
        price = float(row["close"])
        if not atr or atr != atr or row["regime"] == "volatile":
            return None

        candidates = []
        if row["long_score"] >= th and prev["long_score"] < th and row["long_score"] > row["short_score"]:
            candidates.append((LONG, float(row["long_score"])))
        if row["short_score"] >= th and prev["short_score"] < th and row["short_score"] > row["long_score"]:
            candidates.append((SHORT, float(row["short_score"])))
        if not candidates:
            return None
        side, score = max(candidates, key=lambda x: x[1])

        # Funding-Filter: nicht long gehen, wenn Longs teuer Funding zahlen (und umgekehrt)
        if funding_rate is not None:
            if side == LONG and funding_rate > self.cfg.max_adverse_funding:
                return None
            if side == SHORT and funding_rate < -self.cfg.max_adverse_funding:
                return None

        setup = str(row["setup"])
        stop_dist = atr * self.cfg.atr_stop_mult
        tp_r = self.cfg.take_profit_r if setup == "trend" else min(self.cfg.take_profit_r, 1.5)
        if side == LONG:
            stop, tp = price - stop_dist, price + stop_dist * tp_r
        else:
            stop, tp = price + stop_dist, price - stop_dist * tp_r
        reason = (
            f"{setup} {side} score={score:.2f} adx={row['adx']:.1f} rsi={row['rsi']:.1f} "
            f"htf={int(row['htf_bias'])} regime={row['regime']}"
        )
        return Signal(side, price, stop, tp, score, setup, atr, reason)

    def exit_signal(self, feat: pd.DataFrame, side: str, i: int = -1) -> str | None:
        """Vorzeitiger Ausstieg bei starkem Gegensignal."""
        if not self.cfg.exit_on_opposite_signal:
            return None
        row = feat.iloc[i]
        th = self.cfg.entry_threshold
        if side == LONG and row["short_score"] >= th and row["setup"] == "trend":
            return f"Gegensignal SHORT (score {row['short_score']:.2f})"
        if side == SHORT and row["long_score"] >= th and row["setup"] == "trend":
            return f"Gegensignal LONG (score {row['long_score']:.2f})"
        return None

    def update_stop(self, pos: PositionState, high: float, low: float, atr: float) -> float:
        """Break-Even und ATR-Trailing-Stop. Stops werden nur in Gewinnrichtung verschoben."""
        if pos.side == LONG:
            pos.best_price = max(pos.best_price, high)
        else:
            pos.best_price = min(pos.best_price, low)
        best_r = pos.r_multiple(pos.best_price)
        new_stop = pos.stop

        if best_r >= self.cfg.breakeven_at_r:
            # Break-Even + kleiner Puffer für Gebühren
            be = pos.entry + (0.1 * pos.initial_risk if pos.side == LONG else -0.1 * pos.initial_risk)
            new_stop = max(new_stop, be) if pos.side == LONG else min(new_stop, be)

        if best_r >= self.cfg.trailing_start_r and atr == atr and atr > 0:
            if pos.side == LONG:
                new_stop = max(new_stop, pos.best_price - atr * self.cfg.trailing_atr_mult)
            else:
                new_stop = min(new_stop, pos.best_price + atr * self.cfg.trailing_atr_mult)

        pos.stop = new_stop
        return new_stop
