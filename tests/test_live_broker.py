"""LiveBroker gegen eine gemockte ccxt-Börse: Absicherung, Notschließung, externe Schließung."""
import pytest

from bot.broker import LiveBroker
from bot.config import ExchangeConfig
from bot.exchange import MarketLimits
from bot.risk import SizedOrder
from bot.strategy import LONG, Signal


class MockCcxt:
    id = "mock"

    def __init__(self, fail_stop=False):
        self.orders, self.position, self.fail_stop, self.cancelled = [], 0.0, fail_stop, []

    def set_margin_mode(self, *a):
        pass

    def set_leverage(self, *a):
        pass

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        if "stopLossPrice" in params and self.fail_stop:
            raise RuntimeError("SL rejected")
        o = {"id": str(len(self.orders) + 1), "side": side, "amount": amount, "params": params,
             "average": 100.0, "filled": amount}
        self.orders.append(o)
        if not ("stopLossPrice" in params or "takeProfitPrice" in params):
            self.position += amount if side == "buy" else -amount
        return o

    def fetch_positions(self, symbols=None):
        if self.position == 0:
            return []
        return [{"symbol": "BTC/USDT:USDT", "side": "long" if self.position > 0 else "short",
                 "contracts": abs(self.position)}]

    def cancel_order(self, oid, symbol, params=None):
        self.cancelled.append(oid)

    def cancel_all_orders(self, symbol, params=None):
        self.cancelled.append("all")

    def fetch_my_trades(self, symbol, since):
        return [{"side": "sell", "amount": 1.0, "price": 95.0}]


class MockExchange:
    def __init__(self, ccxt_mock):
        self.ex = ccxt_mock

    def limits(self, s):
        return MarketLimits(0.001, 0.001, 5, 100, 1.0)

    def to_exchange_amount(self, s, q):
        return round(q, 3)

    def price_to_precision(self, s, p):
        return round(p, 2)

    def last_price(self, s):
        return 100.0


def signal():
    return Signal(LONG, 100.0, 95.0, 110.0, 0.7, "trend", 2.5, "test")


def test_open_places_stop_and_take_profit():
    m = MockCcxt()
    b = LiveBroker(MockExchange(m), ExchangeConfig())
    pos = b.open("BTC/USDT:USDT", signal(), SizedOrder(1.0, 100, 1, 100, 5, 0))
    kinds = [list(o["params"]) for o in m.orders]
    assert kinds[0] == []                                # Market-Einstieg
    assert "stopLossPrice" in kinds[1] and m.orders[1]["params"]["reduceOnly"]
    assert "takeProfitPrice" in kinds[2] and m.orders[2]["params"]["reduceOnly"]
    assert pos.sl_order_id == "2" and pos.tp_order_id == "3"


def test_position_closed_immediately_if_stop_fails():
    m = MockCcxt(fail_stop=True)
    b = LiveBroker(MockExchange(m), ExchangeConfig())
    with pytest.raises(RuntimeError):
        b.open("BTC/USDT:USDT", signal(), SizedOrder(1.0, 100, 1, 100, 5, 0))
    assert m.position == 0                               # Notschließung erfolgt
    assert "BTC/USDT:USDT" not in b.positions


def test_trailing_stop_replaces_order_new_first():
    m = MockCcxt()
    b = LiveBroker(MockExchange(m), ExchangeConfig())
    b.open("BTC/USDT:USDT", signal(), SizedOrder(1.0, 100, 1, 100, 5, 0))
    b.set_stop("BTC/USDT:USDT", 101.0)
    assert m.orders[-1]["params"]["stopLossPrice"] == 101.0
    assert m.cancelled == ["2"]                          # alter SL erst danach storniert
    assert b.positions["BTC/USDT:USDT"].sl_order_id == "4"


def test_detects_exchange_side_stop_out():
    m = MockCcxt()
    b = LiveBroker(MockExchange(m), ExchangeConfig(taker_fee=0))
    b.open("BTC/USDT:USDT", signal(), SizedOrder(1.0, 100, 1, 100, 5, 0))
    assert b.check_exits("BTC/USDT:USDT") is None        # Position noch offen
    m.position = 0                                       # SL hat auf der Börse ausgelöst
    c = b.check_exits("BTC/USDT:USDT")
    assert c.reason == "Stop-Loss" and c.exit == 95.0 and c.pnl == -5.0
    assert "all" in m.cancelled                          # Rest-TP storniert
