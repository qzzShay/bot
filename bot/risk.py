"""Risikomanagement: Positionsgröße, Hebel, Liquidations-Check, Verlustlimits und Kill-Switch."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import RiskConfig
from .strategy import LONG, Signal

# Vereinfachte Maintenance-Margin-Rate (Binance/Bybit liegen für kleine Positionen bei 0.4–0.5%)
MAINTENANCE_MARGIN_RATE = 0.005


@dataclass
class SizedOrder:
    qty: float
    notional: float
    leverage: int
    margin: float
    risk_amount: float
    liquidation_price: float


def liquidation_price(side: str, entry: float, leverage: float, mmr: float = MAINTENANCE_MARGIN_RATE) -> float:
    """Näherung für den Liquidationspreis einer ISOLATED-Position."""
    if side == LONG:
        return entry * (1 - 1 / leverage + mmr)
    return entry * (1 + 1 / leverage - mmr)


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.peak_equity: float | None = None
        self.day: str | None = None
        self.day_start_equity: float | None = None
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.halted = False
        self.halt_reason = ""

    # ---------- Zustand (für Persistenz) ----------
    def to_state(self) -> dict:
        return {
            "peak_equity": self.peak_equity, "day": self.day, "day_start_equity": self.day_start_equity,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "halted": self.halted, "halt_reason": self.halt_reason,
        }

    def load_state(self, s: dict) -> None:
        self.peak_equity = s.get("peak_equity")
        self.day = s.get("day")
        self.day_start_equity = s.get("day_start_equity")
        self.consecutive_losses = s.get("consecutive_losses", 0)
        cu = s.get("cooldown_until")
        self.cooldown_until = datetime.fromisoformat(cu) if cu else None
        self.halted = s.get("halted", False)
        self.halt_reason = s.get("halt_reason", "")

    # ---------- Laufende Überwachung ----------
    def update_equity(self, equity: float, now: datetime | None = None) -> str | None:
        """Aktualisiert Höchststand/Tagesstart. Gibt einen Grund zurück, falls alle Positionen
        sofort geschlossen werden müssen (Kill-Switch)."""
        now = now or datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.day_start_equity = today, equity
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

        dd = (self.peak_equity - equity) / self.peak_equity * 100 if self.peak_equity else 0
        if dd >= self.cfg.max_drawdown_pct and not self.halted:
            self.halted = True
            self.halt_reason = f"Max. Drawdown {dd:.1f}% erreicht (Limit {self.cfg.max_drawdown_pct}%)"
            return self.halt_reason
        return None

    def daily_loss_pct(self, equity: float) -> float:
        if not self.day_start_equity:
            return 0.0
        return (self.day_start_equity - equity) / self.day_start_equity * 100

    def record_trade(self, pnl: float, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.cooldown_until = now + timedelta(minutes=self.cfg.cooldown_minutes)
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

    def can_open(self, equity: float, open_positions: int, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        if self.halted:
            return False, f"Bot gestoppt: {self.halt_reason}"
        if equity <= 0:
            return False, "Kein Kapital"
        if open_positions >= self.cfg.max_open_positions:
            return False, "Max. offene Positionen erreicht"
        if self.daily_loss_pct(equity) >= self.cfg.max_daily_loss_pct:
            return False, f"Tagesverlust-Limit erreicht ({self.daily_loss_pct(equity):.1f}%)"
        if self.cooldown_until and now < self.cooldown_until:
            return False, f"Cooldown nach Verlustserie bis {self.cooldown_until:%H:%M} UTC"
        return True, ""

    # ---------- Positionsgröße ----------
    def size(self, signal: Signal, equity: float, min_qty: float = 0.0, qty_step: float = 0.0,
             min_notional: float = 0.0, max_leverage_exchange: int | None = None,
             free_balance: float | None = None, open_risk: float = 0.0) -> SizedOrder | None:
        """Risikobasierte Positionsgröße:  qty = (Equity * Risiko%) / Abstand Entry→Stop.

        Der Hebel ergibt sich aus Notional / Equity und wird so gewählt, dass der
        Liquidationspreis sicher HINTER dem Stop-Loss liegt.
        """
        if signal.risk_per_unit <= 0 or equity <= 0:
            return None
        risk_amount = equity * self.cfg.risk_per_trade_pct / 100 * signal.risk_factor
        # Gesamtrisiko aller offenen Positionen begrenzen (Kryptos sind stark korreliert)
        budget = equity * self.cfg.max_portfolio_risk_pct / 100 - max(open_risk, 0.0)
        if budget < risk_amount * 0.25:
            return None
        risk_amount = min(risk_amount, budget)
        qty = risk_amount / signal.risk_per_unit

        max_lev = self.cfg.max_leverage
        if max_leverage_exchange:
            max_lev = min(max_lev, max_leverage_exchange)
        max_notional = min(equity * self.cfg.max_position_notional_x, equity * max_lev)
        if qty * signal.price > max_notional:
            qty = max_notional / signal.price

        if qty_step > 0:
            qty = math.floor(qty / qty_step) * qty_step
            qty = float(f"{qty:.12g}")
        if qty <= 0 or qty < min_qty:
            return None
        notional = qty * signal.price
        if notional < min_notional:
            return None

        # Kleinster Hebel, bei dem die Margin in das freie Guthaben passt (mit 10% Reserve)
        free = equity if free_balance is None else min(free_balance, equity)
        if free <= 0:
            return None
        leverage = max(1, math.ceil(notional / (free * 0.9)))
        if leverage > max_lev:
            return None

        # Liquidation muss mit Puffer hinter dem Stop liegen; sonst ist der Hebel bereits zu hoch
        buf = self.cfg.liquidation_buffer_pct / 100
        liq = liquidation_price(signal.side, signal.price, leverage)
        safe = liq < signal.stop * (1 - buf) if signal.side == LONG else liq > signal.stop * (1 + buf)
        if not safe:
            return None

        return SizedOrder(qty=qty, notional=notional, leverage=leverage, margin=notional / leverage,
                          risk_amount=qty * signal.risk_per_unit, liquidation_price=liq)
