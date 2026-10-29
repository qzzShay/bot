"""Breakout-Strategie, Portfolio-Risiko, Funding und DB-Migration."""
import sqlite3

import numpy as np
import pandas as pd
from conftest import make_ohlcv

from bot.analysis import compute_features, resample
from bot.backtest import funding_to_bars, run_backtest
from bot.broker import PaperBroker
from bot.config import ExchangeConfig, RiskConfig, StrategyConfig
from bot.datasets import to_binance_symbol
from bot.risk import RiskManager, SizedOrder
from bot.storage import Storage
from bot.strategy import LONG, SHORT, Signal, Strategy


def breakout_frame(direction=1, n=400):
    """Seitwärts, dann ein klarer Ausbruch auf der letzten Kerze."""
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")
    rng = np.random.default_rng(0)
    close = 100 + rng.normal(0, 0.5, n).cumsum() * 0.1
    close[-1] = close[:-1].max() + 5 if direction > 0 else close[:-1].min() - 5
    df = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
                       "volume": 1000.0}, index=idx)
    df.iloc[-1, df.columns.get_loc("high")] = close[-1] + 0.2
    df.iloc[-1, df.columns.get_loc("low")] = close[-1] - 0.2
    return df


def btc_trend(up=True, n=400):
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")
    c = np.linspace(100, 200, n) if up else np.linspace(200, 100, n)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx)


def feats(df, btc, funding=None, cfg=None):
    cfg = cfg or StrategyConfig()
    return compute_features(df, resample(df, "1d"), cfg, "4h", "1d", btc=btc, funding=funding)


def test_long_breakout_with_btc_uptrend():
    sig = Strategy(StrategyConfig()).entry_signal(feats(breakout_frame(1), btc_trend(True)))
    assert sig and sig.side == LONG and sig.take_profit is None and sig.risk_factor == 1.0
    assert abs(sig.price - sig.stop - 2 * sig.atr) < 1e-9


def test_btc_filter_blocks_counter_trend():
    st = Strategy(StrategyConfig())
    assert st.entry_signal(feats(breakout_frame(1), btc_trend(False))) is None
    assert st.entry_signal(feats(breakout_frame(-1), btc_trend(True))) is None
    sig = st.entry_signal(feats(breakout_frame(-1), btc_trend(False)))
    assert sig.side == SHORT and sig.risk_factor == 0.5 and sig.stop > sig.price


def test_funding_filter():
    st = Strategy(StrategyConfig(max_funding_24h=0.0009))
    f = feats(breakout_frame(1), btc_trend(True))
    assert st.entry_signal(f, funding_rate=0.0001) is not None
    assert st.entry_signal(f, funding_rate=0.0004) is None     # 3 * 0.04% = 0.12% / 24h
    # Backtest-Pfad: historische Funding-Summe der letzten 24h
    df = breakout_frame(1)
    fund = pd.Series(0.0005, index=df.index)
    assert st.entry_signal(feats(df, btc_trend(True), funding=fund)) is None


def test_portfolio_risk_budget():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_portfolio_risk_pct=3.0, max_leverage=10,
                                max_position_notional_x=10))
    sig = Signal(LONG, 100, 98, None, 1, "breakout", 1, "t")
    assert abs(rm.size(sig, 1000).risk_amount - 10) < 1e-9
    assert abs(rm.size(sig, 1000, open_risk=25).risk_amount - 5) < 1e-9   # nur noch 5 Budget
    assert rm.size(sig, 1000, open_risk=29) is None                       # Rest zu klein
    sig.risk_factor = 0.5
    assert abs(rm.size(sig, 1000).risk_amount - 5) < 1e-9


def test_funding_payment_paper():
    b = PaperBroker(ExchangeConfig(taker_fee=0, slippage=0), 1000)
    b.open("X", Signal(LONG, 100, 90, None, 1, "b", 1, "t"), SizedOrder(2, 200, 1, 200, 20, 0))
    assert abs(b.apply_funding("X", 0.001) - 0.2) < 1e-12   # Long zahlt 0.1% von 200
    assert abs(b.cash - 999.8) < 1e-9
    c = b.close("X", 100, "x")
    assert abs(c.pnl + 0.2) < 1e-9


def test_trailing_without_take_profit_exits_on_stop():
    b = PaperBroker(ExchangeConfig(taker_fee=0, slippage=0), 1000)
    b.open("X", Signal(LONG, 100, 95, None, 1, "b", 1, "t"), SizedOrder(1, 100, 1, 100, 5, 0))
    assert b.check_exits("X", high=1000, low=96) is None      # kein TP → Gewinner läuft weiter
    b.set_stop("X", 150)
    c = b.check_exits("X", high=160, low=149)
    assert c.reason == "Trailing-Stop" and c.pnl == 50


def test_backtest_with_funding_and_btc(cfg):
    data = {"BTC/USDT:USDT": make_ohlcv(seed=1, n=3000, freq="4h"),
            "ETH/USDT:USDT": make_ohlcv(seed=2, n=3000, start_price=2000, freq="4h")}
    events = {s: pd.Series(0.0001, index=pd.date_range(df.index[0], df.index[-1], freq="8h"))
              for s, df in data.items()}
    with_f = run_backtest(cfg, data, funding=events)
    without = run_backtest(cfg, data)
    assert with_f.metrics["trades"] > 0
    assert with_f.metrics["fees"] > without.metrics["fees"]     # Funding wird gebucht
    assert abs(with_f.metrics["final_equity"] - (cfg.risk.paper_start_equity + sum(t.pnl for t in with_f.trades))) < 1e-6


def test_funding_to_bars_sums_events():
    ev = pd.Series([0.001, 0.002, 0.003], index=pd.to_datetime(
        ["2025-01-01 00:00", "2025-01-01 08:00", "2025-01-01 16:00"], utc=True))
    bars = funding_to_bars(ev, "1d")
    assert abs(bars.iloc[0] - 0.006) < 1e-12


def test_symbol_conversion():
    assert to_binance_symbol("BTC/USDT:USDT") == "BTCUSDT"


def test_storage_migrates_old_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL,
        symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL,
        initial_stop REAL NOT NULL, take_profit REAL NOT NULL, leverage INTEGER NOT NULL, best_price REAL NOT NULL,
        opened_at TEXT NOT NULL, reason_open TEXT, status TEXT NOT NULL DEFAULT 'open', exit REAL, closed_at TEXT,
        pnl REAL, fees REAL DEFAULT 0, reason_close TEXT, sl_order_id TEXT, tp_order_id TEXT);
        INSERT INTO trades (mode, symbol, side, qty, entry, stop, initial_stop, take_profit, leverage, best_price,
        opened_at) VALUES ('paper:x', 'BTC', 'long', 1, 100, 90, 90, 120, 1, 100, '2025-01-01');""")
    con.commit()
    con.close()
    st = Storage(str(path))
    assert len(st.open_trades("paper:x")) == 1
    st.insert_trade(mode="paper:x", symbol="ETH", side="long", qty=1, entry=1, stop=0.9, initial_stop=0.9,
                    take_profit=None, leverage=1, best_price=1, opened_at="2025-01-02")
    assert len(st.open_trades("paper:x")) == 2
