import math

from conftest import make_ohlcv

from bot.backtest import optimize, run_backtest


def test_backtest_runs_and_is_consistent(cfg):
    data = {"BTC/USDT:USDT": make_ohlcv(seed=1, n=3000, freq="4h"), "ETH": make_ohlcv(seed=2, n=3000, start_price=2000, freq="4h")}
    res = run_backtest(cfg, data)
    m = res.metrics
    assert m["trades"] > 5
    # Equity-Ende = Start + Summe aller Netto-PnLs
    assert math.isclose(m["final_equity"], cfg.risk.paper_start_equity + sum(t.pnl for t in res.trades), rel_tol=1e-9)
    assert 0 <= m["max_drawdown_pct"] < 100
    # Risiko pro Trade wird eingehalten (Verlust ≈ <= 1% + Gebühren/Slippage)
    worst = min(t.pnl for t in res.trades)
    assert worst > -cfg.risk.paper_start_equity * 0.03
    print(res.report())


def test_max_open_positions_respected(cfg):
    cfg.risk.max_open_positions = 1
    data = {s: make_ohlcv(seed=k, n=3000, freq="4h") for k, s in enumerate("ABC", 3)}
    res = run_backtest(cfg, data)
    spans = sorted((t.opened_at, t.closed_at) for t in res.trades)
    for (o1, c1), (o2, _) in zip(spans, spans[1:]):
        assert o2 >= c1


def test_optimize_small_grid(cfg):
    data = {"BTC": make_ohlcv(seed=7, n=3000, freq="4h")}
    grid = {"strategy.entry_threshold": [0.5, 0.6], "strategy.take_profit_r": [2.0]}
    res = optimize(cfg, data, grid=grid)
    assert res and "test" in res[0]
