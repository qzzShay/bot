"""Engine im Paper-Modus mit einer simulierten Börse (kein Netzwerk)."""

from conftest import make_ohlcv

from bot.analysis import resample
from bot.engine import Engine
from bot.exchange import MarketLimits
from bot.notifier import Notifier
from bot.storage import Storage


class FakeExchange:
    id = "fake"

    def __init__(self, data):
        self.data = data
        self.i = 600

    def load_markets(self):
        return {}

    def market(self, s):
        return {}

    def limits(self, s):
        return MarketLimits(0.0, 0.0, 0.0, 50, 1.0)

    def fetch_ohlcv(self, s, tf, bars, **kw):
        df = self.data[s].iloc[: self.i]
        if tf != "15m":
            df = resample(df, tf).iloc[:-1]   # nur abgeschlossene HTF-Kerzen
        return df.tail(bars)

    def last_price(self, s):
        return float(self.data[s]["close"].iloc[self.i - 1])

    def funding_rate(self, s):
        return 0.0001


def make_engine(cfg, fx, store):
    cfg.symbols = list(fx.data)
    cfg.timeframes.history_bars = 600
    e = Engine(cfg, exchange=fx, storage=store, notifier=Notifier(cfg.notifications))
    e._bar_due = lambda s, now: True
    return e


def test_engine_paper_trades_and_restores(cfg):
    fx = FakeExchange({"BTC/USDT:USDT": make_ohlcv(seed=11), "ETH/USDT:USDT": make_ohlcv(seed=12, start_price=2000)})
    store = Storage(":memory:")
    e = make_engine(cfg, fx, store)
    e.restore()
    for _ in range(900):
        fx.i += 1
        e.tick()
    key = e.mode_key
    closed = store.closed_trades(key)
    assert len(closed) > 0
    assert all(t["pnl"] is not None for t in closed)
    # Bargeld = Start + realisierte PnLs - Einstiegsgebühren noch offener Positionen
    realized = sum(t["pnl"] for t in closed)
    open_fees = sum(p.fees for p in e.broker.positions.values())
    assert abs(store.get_state(f"paper_cash:{key}") - (cfg.risk.paper_start_equity + realized - open_fees)) < 1e-6

    # Neustart: offene Positionen und Kontostand werden aus der DB wiederhergestellt
    e2 = make_engine(cfg, fx, store)
    e2.restore()
    assert set(e2.broker.positions) == set(e.broker.positions)
    assert abs(e2.broker.cash - e.broker.cash) < 1e-9


def test_kill_switch_closes_everything(cfg):
    fx = FakeExchange({"BTC/USDT:USDT": make_ohlcv(seed=11)})
    store = Storage(":memory:")
    e = make_engine(cfg, fx, store)
    e.restore()
    e.risk.peak_equity = 10_000  # künstlich hoher Höchststand → sofort Drawdown > 15%
    e.tick()
    assert e.risk.halted
    assert not e.broker.positions
    ok, _ = e.risk.can_open(e.broker.equity(), 0)
    assert not ok
