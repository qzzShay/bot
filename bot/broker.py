"""Order-Ausführung: PaperBroker (Simulation mit echten Kursen) und LiveBroker (echte Orders via ccxt)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt

from .config import ExchangeConfig
from .exchange import Exchange, retry
from .risk import SizedOrder
from .strategy import LONG, PositionState, Signal

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Position(PositionState):
    symbol: str = ""
    qty: float = 0.0
    leverage: int = 1
    opened_at: str = ""
    fees: float = 0.0
    reason_open: str = ""
    trade_id: int | None = None
    sl_order_id: str | None = None
    tp_order_id: str | None = None

    def unrealized(self, price: float) -> float:
        d = 1 if self.side == LONG else -1
        return (price - self.entry) * self.qty * d

    @property
    def margin(self) -> float:
        return self.entry * self.qty / max(self.leverage, 1)

    @property
    def open_risk(self) -> float:
        """Verlust bis zum aktuellen Stop (0, wenn der Stop bereits im Gewinn liegt)."""
        d = 1 if self.side == LONG else -1
        return max(0.0, (self.entry - self.stop) * d * self.qty)


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    qty: float
    entry: float
    exit: float
    pnl: float        # netto, nach Gebühren
    fees: float
    reason: str
    opened_at: str
    closed_at: str
    trade_id: int | None = None


class PaperBroker:
    """Simuliert Fills mit Gebühren und Slippage. Wird für Paper-Trading UND den Backtest genutzt."""

    is_live = False

    def __init__(self, cfg: ExchangeConfig, start_equity: float):
        self.fee = cfg.taker_fee
        self.slip = cfg.slippage
        self.cash = float(start_equity)
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, float] = {}

    def mark(self, symbol: str, price: float) -> None:
        self.marks[symbol] = price

    def equity(self) -> float:
        return self.cash + sum(p.unrealized(self.marks.get(s, p.entry)) for s, p in self.positions.items())

    def free_balance(self) -> float:
        return self.equity() - sum(p.margin for p in self.positions.values())

    def _fill(self, side: str, price: float, opening: bool) -> float:
        buying = (side == LONG) == opening
        return price * (1 + self.slip) if buying else price * (1 - self.slip)

    def open(self, symbol: str, signal: Signal, sized: SizedOrder, ts: str | None = None) -> Position:
        fill = self._fill(signal.side, signal.price, opening=True)
        fee = fill * sized.qty * self.fee
        self.cash -= fee
        pos = Position(
            side=signal.side, entry=fill, stop=signal.stop, initial_stop=signal.stop,
            take_profit=signal.take_profit, best_price=fill, symbol=symbol, qty=sized.qty,
            leverage=sized.leverage, opened_at=ts or now_iso(), fees=fee, reason_open=signal.reason,
        )
        self.positions[symbol] = pos
        self.marks[symbol] = fill
        return pos

    def close(self, symbol: str, price: float, reason: str, ts: str | None = None,
              apply_slippage: bool = True) -> ClosedTrade | None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        fill = self._fill(pos.side, price, opening=False) if apply_slippage else price
        fee = fill * pos.qty * self.fee
        gross = pos.unrealized(fill)
        self.cash += gross - fee
        self.marks[symbol] = fill
        total_fees = pos.fees + fee
        return ClosedTrade(pos.symbol, pos.side, pos.qty, pos.entry, fill, gross - total_fees, total_fees,
                           reason, pos.opened_at, ts or now_iso(), pos.trade_id)

    def set_stop(self, symbol: str, stop: float) -> None:
        if symbol in self.positions:
            self.positions[symbol].stop = stop

    def apply_funding(self, symbol: str, rate: float) -> float:
        """Funding-Zahlung buchen: Longs zahlen bei positiver Rate, Shorts erhalten sie (und umgekehrt)."""
        pos = self.positions.get(symbol)
        if pos is None or not rate:
            return 0.0
        d = 1 if pos.side == LONG else -1
        payment = self.marks.get(symbol, pos.entry) * pos.qty * rate * d
        self.cash -= payment
        pos.fees += payment
        return payment

    def check_exits(self, symbol: str, high: float, low: float, ts: str | None = None) -> ClosedTrade | None:
        """Prüft, ob Stop-Loss oder Take-Profit innerhalb [low, high] getroffen wurden.
        Werden beide in derselben Kerze berührt, wird konservativ der Stop angenommen."""
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        if pos.side == LONG:
            if low <= pos.stop:
                return self.close(symbol, pos.stop, "Stop-Loss" if pos.stop < pos.entry else "Trailing-Stop", ts)
            if pos.take_profit is not None and high >= pos.take_profit:
                return self.close(symbol, pos.take_profit, "Take-Profit", ts)
        else:
            if high >= pos.stop:
                return self.close(symbol, pos.stop, "Stop-Loss" if pos.stop > pos.entry else "Trailing-Stop", ts)
            if pos.take_profit is not None and low <= pos.take_profit:
                return self.close(symbol, pos.take_profit, "Take-Profit", ts)
        return None


class LiveBroker:
    """Echte Orders. Jede Position wird SOFORT nach dem Einstieg mit Stop-Loss und
    Take-Profit (reduceOnly) auf der Börse abgesichert – auch wenn der Bot danach abstürzt."""

    is_live = True

    def __init__(self, exchange: Exchange, cfg: ExchangeConfig):
        self.x = exchange
        self.ex = exchange.ex
        self.cfg = cfg
        self.positions: dict[str, Position] = {}
        self._configured: set[tuple[str, int]] = set()

    # ---------- Konto ----------
    def equity(self) -> float:
        bal = retry(self.ex.fetch_balance)
        info = bal.get("info") or {}
        try:
            if self.ex.id.startswith("binance") and "totalMarginBalance" in info:
                return float(info["totalMarginBalance"])
            if self.ex.id == "bybit":
                return float(info["result"]["list"][0]["totalEquity"])
            if self.ex.id == "okx":
                return float(info["data"][0]["totalEq"])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        total = bal.get("total", {}).get(self.cfg.quote_currency, 0) or 0
        unreal = sum(float(p.get("unrealizedPnl") or 0) for p in self.exchange_positions())
        return float(total) + unreal

    def free_balance(self) -> float:
        bal = retry(self.ex.fetch_balance)
        return float(bal.get("free", {}).get(self.cfg.quote_currency, 0) or 0)

    def mark(self, symbol: str, price: float) -> None:
        pass

    def exchange_positions(self, symbols: list[str] | None = None) -> list[dict]:
        pos = retry(self.ex.fetch_positions, symbols) if symbols else retry(self.ex.fetch_positions)
        return [p for p in pos if float(p.get("contracts") or 0) != 0]

    # ---------- Hilfsfunktionen ----------
    def _configure(self, symbol: str, leverage: int) -> None:
        if (symbol, leverage) in self._configured:
            return
        try:
            self.ex.set_margin_mode(self.cfg.margin_mode, symbol)
        except ccxt.BaseError as e:  # "No need to change margin type" etc.
            log.debug("set_margin_mode %s: %s", symbol, e)
        try:
            self.ex.set_leverage(leverage, symbol)
        except ccxt.BaseError as e:
            if "not modified" not in str(e).lower() and "no need" not in str(e).lower():
                raise
        self._configured.add((symbol, leverage))

    def _protect(self, symbol: str, side: str, amount: float, stop: float | None, tp: float | None) -> tuple[str | None, str | None]:
        close_side = "sell" if side == LONG else "buy"
        sl_id = tp_id = None
        if stop is not None:
            o = retry(self.ex.create_order, symbol, "market", close_side, amount, None,
                      {"stopLossPrice": self.x.price_to_precision(symbol, stop), "reduceOnly": True})
            sl_id = o.get("id")
        if tp is not None:
            o = retry(self.ex.create_order, symbol, "market", close_side, amount, None,
                      {"takeProfitPrice": self.x.price_to_precision(symbol, tp), "reduceOnly": True})
            tp_id = o.get("id")
        return sl_id, tp_id

    def _cancel(self, symbol: str, order_id: str | None) -> None:
        if not order_id:
            return
        try:
            self.ex.cancel_order(order_id, symbol, {"stop": True, "trigger": True})
        except ccxt.OrderNotFound:
            pass
        except ccxt.BaseError:
            try:
                self.ex.cancel_order(order_id, symbol)
            except ccxt.BaseError as e:
                log.warning("Order %s (%s) konnte nicht storniert werden: %s", order_id, symbol, e)

    def _cancel_all(self, symbol: str) -> None:
        for params in ({}, {"stop": True, "trigger": True}):
            try:
                self.ex.cancel_all_orders(symbol, params)
            except ccxt.BaseError as e:
                log.debug("cancel_all_orders %s %s: %s", symbol, params, e)

    # ---------- Trading ----------
    def open(self, symbol: str, signal: Signal, sized: SizedOrder, ts: str | None = None) -> Position:
        self._configure(symbol, sized.leverage)
        amount = self.x.to_exchange_amount(symbol, sized.qty)
        if amount <= 0:
            raise ValueError(f"Menge {sized.qty} für {symbol} zu klein")
        side = "buy" if signal.side == LONG else "sell"
        # Bewusst KEIN automatischer Retry: bei einem Timeout könnte die Order doppelt ausgeführt werden
        order = self.ex.create_order(symbol, "market", side, amount)
        try:
            order = self.ex.fetch_order(order["id"], symbol) if not order.get("average") else order
        except ccxt.BaseError:
            pass
        contract = self.x.limits(symbol).contract_size
        fill = float(order.get("average") or order.get("price") or self.x.last_price(symbol))
        filled = float(order.get("filled") or amount)
        qty = filled * contract

        # Stops relativ zum tatsächlichen Fill verschieben (Slippage), Abstände bleiben gleich
        shift = fill - signal.price
        stop = signal.stop + shift
        tp = signal.take_profit + shift if signal.take_profit is not None else None
        try:
            sl_id, tp_id = self._protect(symbol, signal.side, filled, stop, tp)
        except Exception:
            log.exception("Stop-Loss konnte nicht gesetzt werden – Position wird SOFORT geschlossen!")
            retry(self.ex.create_order, symbol, "market", "sell" if signal.side == LONG else "buy",
                  filled, None, {"reduceOnly": True})
            raise

        pos = Position(
            side=signal.side, entry=fill, stop=stop, initial_stop=stop, take_profit=tp, best_price=fill,
            symbol=symbol, qty=qty, leverage=sized.leverage, opened_at=ts or now_iso(),
            fees=fill * qty * self.cfg.taker_fee, reason_open=signal.reason, sl_order_id=sl_id, tp_order_id=tp_id,
        )
        self.positions[symbol] = pos
        return pos

    def set_stop(self, symbol: str, stop: float) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        amount = self.x.to_exchange_amount(symbol, pos.qty)
        # Erst neuen Stop setzen, dann alten löschen → Position ist nie ungeschützt
        new_id, _ = self._protect(symbol, pos.side, amount, stop, None)
        self._cancel(symbol, pos.sl_order_id)
        pos.stop, pos.sl_order_id = stop, new_id

    def close(self, symbol: str, price: float | None, reason: str, ts: str | None = None,
              apply_slippage: bool = True) -> ClosedTrade | None:
        pos = self.positions.get(symbol)
        live = self.exchange_positions([symbol])
        self._cancel_all(symbol)
        exit_price = price
        if live:
            p = live[0]
            close_side = "sell" if p["side"] == "long" else "buy"
            order = retry(self.ex.create_order, symbol, "market", close_side, float(p["contracts"]), None,
                          {"reduceOnly": True})
            exit_price = float(order.get("average") or order.get("price") or self.x.last_price(symbol))
        if pos is None:
            return None
        self.positions.pop(symbol, None)
        exit_price = exit_price or self.x.last_price(symbol)
        return self._closed(pos, exit_price, reason, ts)

    def _closed(self, pos: Position, exit_price: float, reason: str, ts: str | None) -> ClosedTrade:
        fee = exit_price * pos.qty * self.cfg.taker_fee
        gross = pos.unrealized(exit_price)
        fees = pos.fees + fee
        return ClosedTrade(pos.symbol, pos.side, pos.qty, pos.entry, exit_price, gross - fees, fees, reason,
                           pos.opened_at, ts or now_iso(), pos.trade_id)

    def check_exits(self, symbol: str, high: float = 0, low: float = 0, ts: str | None = None) -> ClosedTrade | None:
        """Erkennt Positionen, die auf der Börse per Stop-Loss/Take-Profit geschlossen wurden."""
        pos = self.positions.get(symbol)
        if pos is None or self.exchange_positions([symbol]):
            return None
        exit_price, reason = self._find_exit(pos)
        self._cancel_all(symbol)  # übrig gebliebene SL/TP-Order entfernen
        self.positions.pop(symbol, None)
        return self._closed(pos, exit_price, reason, ts)

    def _find_exit(self, pos: Position) -> tuple[float, str]:
        since = int(datetime.fromisoformat(pos.opened_at).timestamp() * 1000)
        close_side = "sell" if pos.side == LONG else "buy"
        try:
            trades = [t for t in retry(self.ex.fetch_my_trades, pos.symbol, since) if t["side"] == close_side]
        except ccxt.BaseError:
            trades = []
        if trades:
            amt = sum(float(t["amount"]) for t in trades)
            price = sum(float(t["price"]) * float(t["amount"]) for t in trades) / amt
        else:
            price = self.x.last_price(pos.symbol)
        if pos.take_profit is not None and abs(price - pos.take_profit) < abs(price - pos.stop):
            return price, "Take-Profit"
        return price, "Stop-Loss" if (price - pos.entry) * (1 if pos.side == LONG else -1) < 0 else "Trailing-Stop"

    def adopt(self, pos: Position) -> None:
        """Position (z.B. nach Neustart aus der DB) übernehmen und Absicherung prüfen."""
        self.positions[pos.symbol] = pos
        try:
            open_orders = self.ex.fetch_open_orders(pos.symbol, None, None, {"stop": True, "trigger": True})
        except ccxt.BaseError:
            open_orders = []
        try:
            open_orders += self.ex.fetch_open_orders(pos.symbol)
        except ccxt.BaseError:
            pass
        ids = {o.get("id") for o in open_orders}
        if pos.sl_order_id not in ids:
            log.warning("%s: kein aktiver Stop-Loss gefunden – setze neu bei %.6g", pos.symbol, pos.stop)
            amount = self.x.to_exchange_amount(pos.symbol, pos.qty)
            pos.sl_order_id, _ = self._protect(pos.symbol, pos.side, amount, pos.stop, None)

