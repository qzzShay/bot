from datetime import datetime, timedelta, timezone

from bot.config import RiskConfig
from bot.risk import RiskManager, liquidation_price
from bot.strategy import LONG, SHORT, Signal


def sig(side=LONG, price=100.0, stop=98.0):
    tp = price + (price - stop) * 2
    return Signal(side, price, stop, tp, 0.7, "trend", 1.0, "test")


def test_size_risks_configured_percent():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_leverage=10, max_position_notional_x=10))
    s = rm.size(sig(), equity=1000)
    assert abs(s.risk_amount - 10) < 1e-9
    assert abs(s.qty - 5) < 1e-9          # 10 / (100-98)
    assert s.leverage == 1                # 500 Notional bei 1000 Equity


def test_size_uses_leverage_for_tight_stops():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_leverage=5, max_position_notional_x=5))
    s = rm.size(sig(stop=99.5), equity=1000)   # 20 Einheiten → 2000 Notional
    assert s.leverage == 3
    assert s.liquidation_price < 99.5


def test_size_capped_by_max_notional():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=2.0, max_leverage=5, max_position_notional_x=2))
    s = rm.size(sig(stop=99.9), equity=1000)
    assert s.notional <= 2000 + 1e-6
    assert s.risk_amount < 20


def test_rejects_when_liquidation_before_stop():
    rm = RiskManager(RiskConfig(max_leverage=50, max_position_notional_x=50, risk_per_trade_pct=5))
    # Stop 10% entfernt, aber der Hebel wäre nötig... hier: wenig freie Margin erzwingt hohen Hebel
    s = rm.size(sig(stop=90.0), equity=1000, free_balance=20)
    assert s is None


def test_qty_step_and_min_qty():
    rm = RiskManager(RiskConfig())
    s = rm.size(sig(), equity=1000, qty_step=0.1)
    assert abs(s.qty * 10 - round(s.qty * 10)) < 1e-9
    assert rm.size(sig(), equity=1000, min_qty=100) is None


def test_liquidation_formula():
    assert liquidation_price(LONG, 100, 10) < 100 < liquidation_price(SHORT, 100, 10)
    assert abs(liquidation_price(LONG, 100, 10) - 90.5) < 1e-9


def test_daily_loss_limit_and_kill_switch():
    rm = RiskManager(RiskConfig(max_daily_loss_pct=3, max_drawdown_pct=10))
    t = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
    rm.update_equity(1000, t)
    assert rm.can_open(1000, 0, t)[0]
    rm.update_equity(965, t)
    ok, why = rm.can_open(965, 0, t)
    assert not ok and "Tagesverlust" in why
    # neuer Tag → wieder erlaubt
    t2 = t + timedelta(days=1)
    rm.update_equity(965, t2)
    assert rm.can_open(965, 0, t2)[0]
    assert rm.update_equity(890, t2) is not None   # 11% DD vom Peak 1000
    assert rm.halted and not rm.can_open(890, 0, t2)[0]


def test_cooldown_after_losing_streak():
    rm = RiskManager(RiskConfig(max_consecutive_losses=3, cooldown_minutes=60))
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rm.update_equity(1000, t)
    for _ in range(3):
        rm.record_trade(-5, t)
    assert not rm.can_open(1000, 0, t + timedelta(minutes=30))[0]
    assert rm.can_open(1000, 0, t + timedelta(minutes=61))[0]


def test_state_roundtrip():
    rm = RiskManager(RiskConfig())
    rm.update_equity(1234)
    rm.halted, rm.halt_reason = True, "x"
    rm2 = RiskManager(RiskConfig())
    rm2.load_state(rm.to_state())
    assert rm2.halted and rm2.peak_equity == 1234
