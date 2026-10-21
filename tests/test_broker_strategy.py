from bot.broker import PaperBroker
from bot.config import ExchangeConfig, StrategyConfig
from bot.risk import SizedOrder
from bot.strategy import LONG, SHORT, PositionState, Signal, Strategy


def broker():
    return PaperBroker(ExchangeConfig(taker_fee=0.001, slippage=0.0), 1000)


def open_long(b, price=100.0, stop=95.0, tp=110.0, qty=2.0):
    s = Signal(LONG, price, stop, tp, 0.7, "trend", 2.5, "t")
    return b.open("X", s, SizedOrder(qty, qty * price, 1, qty * price, qty * (price - stop), 0))


def test_paper_take_profit_pnl_after_fees():
    b = broker()
    open_long(b)
    c = b.check_exits("X", high=111, low=99)
    assert c.reason == "Take-Profit"
    assert abs(c.pnl - (20 - 0.2 - 0.22)) < 1e-9
    assert abs(b.equity() - (1000 + c.pnl)) < 1e-9


def test_paper_stop_wins_if_both_hit():
    b = broker()
    open_long(b)
    c = b.check_exits("X", high=120, low=90)
    assert c.reason == "Stop-Loss" and c.pnl < 0


def test_paper_short():
    b = broker()
    s = Signal(SHORT, 100, 105, 90, 0.7, "trend", 2.5, "t")
    b.open("X", s, SizedOrder(1, 100, 1, 100, 5, 0))
    b.mark("X", 95)
    assert b.equity() > 1000 - 1
    c = b.check_exits("X", high=96, low=89)
    assert c.reason == "Take-Profit" and c.pnl > 9


def test_breakeven_and_trailing():
    st = Strategy(StrategyConfig(breakeven_at_r=1.0, trailing_start_r=1.5, trailing_atr_mult=2.0))
    p = PositionState(LONG, entry=100, stop=95, initial_stop=95, take_profit=120, best_price=100)
    assert st.update_stop(p, high=103, low=99, atr=1) == 95           # noch < 1R
    assert st.update_stop(p, high=105.5, low=101, atr=1) == 100.5     # Break-Even + 0.1R
    assert st.update_stop(p, high=110, low=106, atr=1) == 108         # Trailing: 110 - 2*1
    assert st.update_stop(p, high=109, low=104, atr=1) == 108         # Stop wird nie zurückgesetzt


def test_trailing_short():
    st = Strategy(StrategyConfig(breakeven_at_r=1.0, trailing_start_r=1.5, trailing_atr_mult=2.0))
    p = PositionState(SHORT, entry=100, stop=105, initial_stop=105, take_profit=80, best_price=100)
    assert st.update_stop(p, high=99, low=90, atr=1) == 92
