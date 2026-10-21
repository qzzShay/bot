"""Backtester: nutzt dieselbe Strategie, dasselbe Risikomanagement und denselben PaperBroker wie der Live-Bot."""
from __future__ import annotations

import copy
import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .analysis import compute_features, resample, tf_delta
from .broker import ClosedTrade, PaperBroker
from .config import Config
from .risk import RiskManager
from .strategy import Strategy

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity: pd.Series
    start_equity: float
    metrics: dict = field(default_factory=dict)

    def report(self) -> str:
        m = self.metrics
        lines = [
            "=" * 60, " BACKTEST-ERGEBNIS", "=" * 60,
            f" Zeitraum           : {m['start']} → {m['end']}",
            f" Startkapital       : {self.start_equity:,.2f}",
            f" Endkapital         : {m['final_equity']:,.2f}",
            f" Rendite            : {m['return_pct']:+.2f}%",
            f" Max. Drawdown      : {m['max_drawdown_pct']:.2f}%",
            f" Sharpe (ann.)      : {m['sharpe']:.2f}",
            f" Trades             : {m['trades']} (Long {m['longs']} / Short {m['shorts']})",
            f" Trefferquote       : {m['win_rate']:.1f}%",
            f" Profit-Faktor      : {m['profit_factor']:.2f}",
            f" Ø Gewinn / Verlust : {m['avg_win']:+.2f} / {m['avg_loss']:+.2f}",
            f" Gebühren gesamt    : {m['fees']:.2f}",
            "=" * 60,
        ]
        if m["exit_reasons"]:
            lines.insert(-1, " Ausstiege          : " + ", ".join(f"{k}: {v}" for k, v in m["exit_reasons"].items()))
        return "\n".join(lines)


def compute_metrics(trades: list[ClosedTrade], equity: pd.Series, start_equity: float, entry_tf: str) -> dict:
    pnls = np.array([t.pnl for t in trades]) if trades else np.array([])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    peak = equity.cummax()
    dd = ((peak - equity) / peak).max() * 100 if len(equity) else 0.0
    rets = equity.pct_change().dropna()
    bars_per_year = pd.Timedelta(days=365) / tf_delta(entry_tf)
    sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) if len(rets) > 1 and rets.std() > 0 else 0.0
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    final = float(equity.iloc[-1]) if len(equity) else start_equity
    return {
        "start": str(equity.index[0])[:16] if len(equity) else "-",
        "end": str(equity.index[-1])[:16] if len(equity) else "-",
        "final_equity": final,
        "return_pct": (final / start_equity - 1) * 100,
        "max_drawdown_pct": float(dd),
        "sharpe": sharpe,
        "trades": len(trades),
        "longs": sum(t.side == "long" for t in trades),
        "shorts": sum(t.side == "short" for t in trades),
        "win_rate": float(len(wins) / len(pnls) * 100) if len(pnls) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf") if len(wins) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "fees": float(sum(t.fees for t in trades)),
        "exit_reasons": reasons,
    }


def run_backtest(cfg: Config, data: dict[str, pd.DataFrame], htf_data: dict[str, pd.DataFrame] | None = None,
                 limits: dict | None = None, trade_start: pd.Timestamp | None = None) -> BacktestResult:
    """data: {symbol: OHLCV im Entry-Timeframe}. HTF-Daten werden – falls nicht übergeben – per Resampling erzeugt.
    trade_start: erst ab diesem Zeitpunkt handeln (Daten davor dienen nur zum Einschwingen der Indikatoren)."""
    tf = cfg.timeframes
    strategy = Strategy(cfg.strategy)
    risk = RiskManager(cfg.risk)
    broker = PaperBroker(cfg.exchange, cfg.risk.paper_start_equity)
    limits = limits or {}

    feats = {}
    for sym, df in data.items():
        htf = (htf_data or {}).get(sym)
        if htf is None:
            htf = resample(df, tf.trend)
        feats[sym] = compute_features(df, htf, cfg.strategy, tf.entry, tf.trend)

    index = sorted(set().union(*[f.index for f in feats.values()]))
    if trade_start is not None:
        index = [ts for ts in index if ts >= trade_start]
    pos_in_feat = {s: {ts: i for i, ts in enumerate(f.index)} for s, f in feats.items()}
    trades: list[ClosedTrade] = []
    equity_curve = []
    delta = tf_delta(tf.entry)

    for ts in index:
        close_time = ts + delta
        now = close_time.to_pydatetime()
        stamp = close_time.isoformat()
        rows = {}
        for sym, f in feats.items():
            i = pos_in_feat[sym].get(ts)
            if i is None:
                continue
            rows[sym] = i
            r = f.iloc[i]
            # 1) Stop/TP innerhalb der Kerze
            c = broker.check_exits(sym, float(r["high"]), float(r["low"]), stamp)
            if c:
                trades.append(c)
                risk.record_trade(c.pnl, now)
            broker.mark(sym, float(r["close"]))

        equity = broker.equity()
        if risk.update_equity(equity, now):
            for sym in list(broker.positions):
                c = broker.close(sym, broker.marks[sym], "Kill-Switch", stamp)
                trades.append(c)
                risk.record_trade(c.pnl, now)
            equity = broker.equity()

        for sym, i in rows.items():
            f = feats[sym]
            r = f.iloc[i]
            pos = broker.positions.get(sym)
            window = f.iloc[: i + 1]
            if pos:
                reason = strategy.exit_signal(window, pos.side)
                if reason:
                    c = broker.close(sym, float(r["close"]), reason, stamp)
                    trades.append(c)
                    risk.record_trade(c.pnl, now)
                else:
                    strategy.update_stop(pos, float(r["high"]), float(r["low"]), float(r["atr"]))
                continue
            sig = strategy.entry_signal(window)
            if not sig:
                continue
            ok, _ = risk.can_open(equity, len(broker.positions), now)
            if not ok:
                continue
            lim = limits.get(sym, {})
            sized = risk.size(sig, equity, free_balance=broker.free_balance(), **lim)
            if sized:
                broker.open(sym, sig, sized, stamp)
        equity_curve.append((close_time, broker.equity()))

    # Offene Positionen zum Schluss glattstellen
    for sym in list(broker.positions):
        trades.append(broker.close(sym, broker.marks[sym], "Backtest-Ende"))
    if equity_curve:
        equity_curve[-1] = (equity_curve[-1][0], broker.equity())

    eq = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve], dtype=float)
    res = BacktestResult(trades, eq, cfg.risk.paper_start_equity)
    res.metrics = compute_metrics(trades, eq, cfg.risk.paper_start_equity, tf.entry)
    return res


def save_result(res: BacktestResult, out_dir: str = "data/backtest") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([t.__dict__ for t in res.trades]).to_csv(out / "trades.csv", index=False)
    res.equity.rename("equity").to_csv(out / "equity.csv")
    return out


def load_or_fetch(exchange, symbol: str, timeframe: str, start: str, end: str | None,
                  cache_dir: str = "data/cache") -> pd.DataFrame:
    """Historische Kerzen laden (mit CSV-Cache)."""
    safe = symbol.replace("/", "-").replace(":", "_")
    path = Path(cache_dir) / f"{exchange.id}_{safe}_{timeframe}_{start}_{end or 'now'}.csv"
    if path.exists() and end:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    df = exchange.fetch_history(symbol, timeframe, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    return df


def load_csv(path: str) -> pd.DataFrame:
    """CSV mit Spalten timestamp/date, open, high, low, close, volume."""
    df = pd.read_csv(path)
    tcol = next(c for c in df.columns if c.lower() in ("timestamp", "date", "time", "datetime", "ts"))
    t = df.pop(tcol)
    df.index = pd.to_datetime(t, unit="ms", utc=True) if pd.api.types.is_numeric_dtype(t) else pd.to_datetime(t, utc=True)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()


# ---------- Parameter-Optimierung mit Out-of-Sample-Test ----------
GRID = {
    "strategy.entry_threshold": [0.5, 0.55, 0.6, 0.65],
    "strategy.atr_stop_mult": [1.5, 2.0, 2.5],
    "strategy.take_profit_r": [2.0, 2.5, 3.0],
}


def _with_params(cfg: Config, params: dict) -> Config:
    c = copy.deepcopy(cfg)
    for key, val in params.items():
        section, name = key.split(".")
        setattr(getattr(c, section), name, val)
    return c


def optimize(cfg: Config, data: dict[str, pd.DataFrame], train_frac: float = 0.7, grid: dict | None = None) -> list[dict]:
    """Grid-Search auf den ersten 70% der Daten, Bewertung der besten Kombinationen auf den letzten 30%.
    Nur Parameter verwenden, die AUCH out-of-sample funktionieren – sonst ist es Overfitting."""
    grid = grid or GRID
    split = min(df.index[int(len(df) * train_frac)] for df in data.values())
    train = {s: df[df.index < split] for s, df in data.items()}

    results = []
    keys = list(grid)
    for combo in itertools.product(*grid.values()):
        params = dict(zip(keys, combo))
        r = run_backtest(_with_params(cfg, params), train).metrics
        score = r["return_pct"] / max(r["max_drawdown_pct"], 1.0) if r["trades"] >= 10 else -999
        results.append({"params": params, "train": r, "score": score})
    results.sort(key=lambda x: x["score"], reverse=True)
    for res in results[:5]:
        # Out-of-Sample: komplette Daten für die Indikatoren, gehandelt wird aber erst ab dem Split
        res["test"] = run_backtest(_with_params(cfg, res["params"]), data, trade_start=split).metrics
    return results[:5]
