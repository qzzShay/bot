"""Day-Trading-Modus: Zeitfenster, Pflicht-Schließung, Tageslimit, Echtgeld-Sperre."""
import pandas as pd
import pytest
from conftest import make_ohlcv

from bot.backtest import run_backtest
from bot.config import Config, ConfigError, StrategyConfig
from bot.strategy import Strategy


def day_cfg(cfg):
    cfg.timeframes.entry, cfg.timeframes.trend = "1h", "4h"
    cfg.strategy.day_trading = True
    cfg.strategy.breakout_period = 48
    cfg.strategy.trailing_atr_mult = 3.0
    cfg.validate()
    return cfg


def test_entry_window():
    st = Strategy(StrategyConfig(day_trading=True, last_entry_hour_utc=21, flat_hour_utc=0))
    assert st.session_entry_allowed(pd.Timestamp("2025-01-01 10:00", tz="UTC"))
    assert st.session_entry_allowed(pd.Timestamp("2025-01-01 21:00", tz="UTC"))
    assert not st.session_entry_allowed(pd.Timestamp("2025-01-01 22:00", tz="UTC"))
    assert not st.session_entry_allowed(pd.Timestamp("2025-01-02 00:00", tz="UTC"))
    assert Strategy(StrategyConfig()).session_entry_allowed(pd.Timestamp("2025-01-01 23:00", tz="UTC"))


def test_flat_due():
    st = Strategy(StrategyConfig(day_trading=True, flat_hour_utc=0))
    opened = "2025-01-01T10:00:00+00:00"
    assert not st.session_flat_due(pd.Timestamp("2025-01-01 23:59", tz="UTC"), opened)
    assert st.session_flat_due(pd.Timestamp("2025-01-02 00:00", tz="UTC"), opened)
    assert st.session_flat_due(pd.Timestamp("2025-01-05 12:00", tz="UTC"), opened)   # nach Neustart
    st22 = Strategy(StrategyConfig(day_trading=True, flat_hour_utc=22, last_entry_hour_utc=20))
    assert st22.session_flat_due(pd.Timestamp("2025-01-01 22:00", tz="UTC"), opened)
    assert not Strategy(StrategyConfig()).session_flat_due(pd.Timestamp("2025-03-01", tz="UTC"), opened)


def test_backtest_never_holds_overnight(cfg):
    cfg = day_cfg(cfg)
    data = {"BTC/USDT:USDT": make_ohlcv(seed=1, n=4000, freq="1h"),
            "ETH/USDT:USDT": make_ohlcv(seed=2, n=4000, start_price=2000, freq="1h")}
    res = run_backtest(cfg, data)
    assert res.metrics["trades"] > 10
    per_day = {}
    for t in res.trades:
        o, c = pd.Timestamp(t.opened_at), pd.Timestamp(t.closed_at)
        assert (c - o) <= pd.Timedelta(hours=24)
        assert c.normalize() - o.normalize() <= pd.Timedelta(days=1)
        if c.normalize() > o.normalize():
            assert c.hour == 0 and c.minute == 0           # nur exakt zur Mitternacht
        assert 1 <= o.hour <= 21
        per_day[(t.symbol, o.date())] = per_day.get((t.symbol, o.date()), 0) + 1
    assert max(per_day.values()) == 1
    assert "Tagesende" in res.metrics["exit_reasons"]


def test_real_money_daytrading_blocked():
    c = Config(mode="live")
    c.exchange.sandbox, c.exchange.api_key, c.exchange.api_secret = "none", "k", "s"
    c.live_confirmed = True
    c.timeframes.entry = "1h"
    c.strategy.day_trading = True
    with pytest.raises(ConfigError, match="Day-Trading"):
        c.validate()
    c.strategy.day_trading_allow_real_money = True
    c.validate()
    c.exchange.sandbox = "demo"
    c.strategy.day_trading_allow_real_money = False
    c.validate()                                            # Demo ist erlaubt


def test_daytrade_needs_intraday_timeframe():
    c = Config()
    c.strategy.day_trading = True
    c.timeframes.entry = "1d"
    with pytest.raises(ConfigError):
        c.validate()


def test_engine_closes_at_day_change(cfg):
    from test_engine import FakeExchange, make_engine

    from bot.broker import Position
    from bot.storage import Storage

    cfg = day_cfg(cfg)
    fx = FakeExchange({"BTC/USDT:USDT": make_ohlcv(seed=4, n=600, freq="1h")})
    e = make_engine(cfg, fx, Storage(":memory:"))
    e.broker.positions["BTC/USDT:USDT"] = Position(
        side="long", entry=100, stop=1, initial_stop=1, take_profit=None, best_price=100,
        symbol="BTC/USDT:USDT", qty=0.01, opened_at="2020-01-01T10:00:00+00:00")
    e.tick()
    assert "BTC/USDT:USDT" not in e.broker.positions


def test_daytrade_example_config_loads():
    from bot.config import load_config
    c = load_config("config/config.daytrade.yaml", env_file=None)
    assert c.strategy.day_trading and c.timeframes.entry == "1h" and c.mode == "paper"
