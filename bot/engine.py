"""Haupt-Schleife des Bots (Paper- und Live-Betrieb)."""
from __future__ import annotations

import logging
import signal as os_signal
import time
from datetime import datetime, timezone

import pandas as pd

from .analysis import compute_features, tf_delta
from .broker import ClosedTrade, LiveBroker, PaperBroker, Position, now_iso
from .config import Config
from .exchange import Exchange
from .news import NewsMonitor
from .notifier import Notifier
from .risk import RiskManager
from .storage import Storage
from .strategy import Strategy

log = logging.getLogger(__name__)

BTC_SYMBOL = "BTC/USDT:USDT"


class Engine:
    def __init__(self, cfg: Config, exchange: Exchange | None = None, storage: Storage | None = None,
                 notifier: Notifier | None = None):
        self.cfg = cfg
        self.x = exchange or Exchange(cfg.exchange, authenticated=cfg.is_live)
        self.store = storage or Storage(cfg.engine.db_path)
        label = "LIVE" if cfg.is_real_money else ("TESTNET" if cfg.is_live else "PAPER")
        self.label = label
        self.notify = notifier or Notifier(cfg.notifications, prefix=f"[{label}] ")
        self.strategy = Strategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk)
        self.mode_key = f"{cfg.mode}:{cfg.exchange.name}:{cfg.exchange.sandbox}"
        self.broker: PaperBroker | LiveBroker = (
            LiveBroker(self.x, cfg.exchange) if cfg.is_live else PaperBroker(cfg.exchange, cfg.risk.paper_start_equity)
        )
        self.news = NewsMonitor(cfg.news, cfg.symbols)
        self.last_bar: dict[str, pd.Timestamp] = {}
        self._btc_cache: pd.DataFrame | None = None
        self.running = True
        self._last_equity_log = 0.0

    # ---------- Start / Wiederherstellung ----------
    def restore(self) -> None:
        self.risk.load_state(self.store.get_state(f"risk:{self.mode_key}", {}))
        if isinstance(self.broker, PaperBroker):
            self.broker.cash = self.store.get_state(f"paper_cash:{self.mode_key}", self.cfg.risk.paper_start_equity)

        for t in self.store.open_trades(self.mode_key):
            pos = Position(
                side=t["side"], entry=t["entry"], stop=t["stop"], initial_stop=t["initial_stop"],
                take_profit=t["take_profit"], best_price=t["best_price"], symbol=t["symbol"], qty=t["qty"],
                leverage=t["leverage"], opened_at=t["opened_at"], fees=t["fees"] or 0,
                reason_open=t["reason_open"] or "", trade_id=t["id"],
                sl_order_id=t["sl_order_id"], tp_order_id=t["tp_order_id"],
            )
            if isinstance(self.broker, LiveBroker):
                self.broker.adopt(pos)
                if pos.sl_order_id != t["sl_order_id"]:
                    self.store.update_trade(pos.trade_id, sl_order_id=pos.sl_order_id)
            else:
                self.broker.positions[pos.symbol] = pos
            log.info("Offene Position wiederhergestellt: %s %s %.6g @ %.6g", pos.symbol, pos.side, pos.qty, pos.entry)

        if isinstance(self.broker, LiveBroker):
            for p in self.broker.exchange_positions():
                if p["symbol"] not in self.broker.positions:
                    msg = (f"⚠️ Unbekannte Position auf der Börse: {p['symbol']} {p['side']} "
                           f"{p['contracts']} Kontrakte – wird vom Bot NICHT verwaltet.")
                    log.warning(msg)
                    self.notify.send(msg)

    def _persist_state(self) -> None:
        self.store.set_state(f"risk:{self.mode_key}", self.risk.to_state())
        if isinstance(self.broker, PaperBroker):
            self.store.set_state(f"paper_cash:{self.mode_key}", self.broker.cash)

    # ---------- Daten ----------
    def features(self, symbol: str) -> pd.DataFrame:
        tf = self.cfg.timeframes
        entry = self.x.fetch_ohlcv(symbol, tf.entry, tf.history_bars)
        trend = self.x.fetch_ohlcv(symbol, tf.trend, tf.history_bars)
        return compute_features(entry, trend, self.cfg.strategy, tf.entry, tf.trend, btc=self.btc_data())

    def btc_data(self) -> pd.DataFrame | None:
        """BTC-Kerzen für den Marktregime-Filter (einmal pro Kerze geladen)."""
        if not self.cfg.strategy.btc_filter:
            return None
        tf = self.cfg.timeframes
        cached = self._btc_cache
        if cached is not None and pd.Timestamp.now(tz="UTC") < cached.index[-1] + 2 * tf_delta(tf.entry):
            return cached
        self._btc_cache = self.x.fetch_ohlcv(BTC_SYMBOL, tf.entry, tf.history_bars)
        return self._btc_cache

    def _bar_due(self, symbol: str, now: pd.Timestamp) -> bool:
        last = self.last_bar.get(symbol)
        if last is None:
            return True
        # Die nächste Kerze schließt bei last + 2*delta (Index = Eröffnungszeit)
        return now >= last + 2 * tf_delta(self.cfg.timeframes.entry) + pd.Timedelta(seconds=3)

    # ---------- Trading-Logik ----------
    def _on_closed(self, c: ClosedTrade) -> None:
        if c.trade_id:
            self.store.update_trade(c.trade_id, status="closed", exit=c.exit, pnl=c.pnl, fees=c.fees,
                                    closed_at=c.closed_at, reason_close=c.reason)
        self.risk.record_trade(c.pnl)
        emoji = "✅" if c.pnl >= 0 else "❌"
        self.notify.send(f"{emoji} {c.symbol} {c.side.upper()} geschlossen ({c.reason})\n"
                         f"Entry {c.entry:.6g} → Exit {c.exit:.6g} | PnL {c.pnl:+.2f} {self.cfg.exchange.quote_currency}")
        self._persist_state()

    def close_all(self, reason: str) -> None:
        for sym in list(self.broker.positions):
            price = self.x.last_price(sym)
            c = self.broker.close(sym, price, reason)
            if c:
                self._on_closed(c)

    def _manage_position(self, symbol: str, pos: Position, feat: pd.DataFrame) -> None:
        row = feat.iloc[-1]
        reason = self.strategy.exit_signal(feat, pos.side)
        if reason:
            c = self.broker.close(symbol, float(row["close"]), reason)
            if c:
                self._on_closed(c)
            return
        old = pos.stop
        new = self.strategy.update_stop(pos, float(row["high"]), float(row["low"]), float(row["atr"]))
        if new != old:
            pos.stop = old
            try:
                self.broker.set_stop(symbol, new)
                log.info("%s Stop nachgezogen: %.6g → %.6g", symbol, old, new)
            except Exception:
                log.exception("%s: Stop-Anpassung fehlgeschlagen, alter Stop bleibt aktiv", symbol)
        if pos.trade_id:
            self.store.update_trade(pos.trade_id, stop=pos.stop, best_price=pos.best_price,
                                    sl_order_id=pos.sl_order_id)

    def _try_entry(self, symbol: str, feat: pd.DataFrame, equity: float) -> None:
        sig = self.strategy.entry_signal(feat)
        if not sig:
            return
        st = self.cfg.strategy
        now = pd.Timestamp.now(tz="UTC")
        if st.day_trading:
            close_time = feat.index[-1] + tf_delta(self.cfg.timeframes.entry)
            if not (self.strategy.session_entry_allowed(close_time) and self.strategy.session_entry_allowed(now)):
                log.info("%s: Signal %s außerhalb des Day-Trading-Fensters", symbol, sig.side)
                return
            today = now.normalize().isoformat()
            if self.store.count_opened_since(self.mode_key, symbol, today) >= st.max_trades_per_day:
                log.info("%s: Signal %s – Tageslimit von %d Trade(s) erreicht", symbol, sig.side, st.max_trades_per_day)
                return
        blocked = self.news.blocked(symbol)
        if blocked:
            log.info("%s: Signal %s wegen News ausgesetzt – %s", symbol, sig.side, blocked)
            return
        fr = self.x.funding_rate(symbol)
        sig = self.strategy.entry_signal(feat, funding_rate=fr)
        if not sig:
            log.info("%s: Signal verworfen (Funding-Rate %.4f%%)", symbol, (fr or 0) * 100)
            return
        ok, why = self.risk.can_open(equity, len(self.broker.positions))
        if not ok:
            log.info("%s: Signal %s ignoriert – %s", symbol, sig.side, why)
            return
        # Kurs kann sich seit Kerzenschluss bewegt haben → SL/TP mit gleichem Abstand zum aktuellen Kurs
        sig.shift(self.x.last_price(symbol) - sig.price)
        lim = self.x.limits(symbol)
        open_risk = sum(p.open_risk for p in self.broker.positions.values())
        sized = self.risk.size(sig, equity, lim.min_qty, lim.qty_step, lim.min_notional, lim.max_leverage,
                               free_balance=self.broker.free_balance(), open_risk=open_risk)
        if not sized:
            log.info("%s: Signal %s – Positionsgröße unzulässig (zu klein/Hebel/Liquidation)", symbol, sig.side)
            return
        pos = self.broker.open(symbol, sig, sized)
        pos.trade_id = self.store.insert_trade(
            mode=self.mode_key, symbol=symbol, side=pos.side, qty=pos.qty, entry=pos.entry, stop=pos.stop,
            initial_stop=pos.initial_stop, take_profit=pos.take_profit, leverage=pos.leverage,
            best_price=pos.best_price, opened_at=pos.opened_at, reason_open=sig.reason, fees=pos.fees,
            sl_order_id=pos.sl_order_id, tp_order_id=pos.tp_order_id,
        )
        self.notify.send(
            f"🚀 {symbol} {pos.side.upper()} eröffnet @ {pos.entry:.6g}\n"
            f"Menge {pos.qty:.6g} | Hebel {pos.leverage}x | SL {pos.stop:.6g} | "
            f"TP {f'{pos.take_profit:.6g}' if pos.take_profit else 'Trailing'}\n"
            f"Risiko {sized.risk_amount:.2f} {self.cfg.exchange.quote_currency} | Liq ~{sized.liquidation_price:.6g}\n"
            f"{sig.reason}"
        )
        self._persist_state()

    def _check_news(self) -> None:
        """Neue kritische Meldungen melden; bei Futures-Delisting betroffene Positionen schließen."""
        try:
            fresh = self.news.refresh()
        except Exception:
            log.exception("News-Abruf fehlgeschlagen – Bot handelt normal weiter")
            return
        for ev in fresh:
            self.notify.send(f"📰 {ev.describe()}\n{ev.link}")
            if not (ev.futures_delisting and self.cfg.news.close_on_delisting):
                continue
            for sym in list(self.broker.positions):
                if sym.split("/")[0] in ev.coins:
                    c = self.broker.close(sym, self.x.last_price(sym), "Futures-Delisting angekündigt")
                    if c:
                        self._on_closed(c)

    def tick(self) -> None:
        now = pd.Timestamp.now(tz="UTC")
        self._check_news()
        # 1) Stops/TPs prüfen (Paper: simuliert, Live: von der Börse ausgeführt)
        for sym in self.cfg.symbols:
            price = self.x.last_price(sym)
            self.broker.mark(sym, price)
            c = self.broker.check_exits(sym, price, price)
            if c:
                self._on_closed(c)
            pos = self.broker.positions.get(sym)
            if pos and self.strategy.session_flat_due(now, pos.opened_at):
                c = self.broker.close(sym, price, "Tagesende")
                if c:
                    self._on_closed(c)

        # 2) Konto & Kill-Switch
        equity = self.broker.equity()
        prev_day = self.risk.day
        kill = self.risk.update_equity(equity)
        if prev_day and prev_day != self.risk.day:
            self.daily_report(equity)
        if kill:
            self.notify.send(f"🛑 KILL-SWITCH: {kill}. Alle Positionen werden geschlossen, keine neuen Trades. "
                             f"Zum Fortsetzen: python -m bot reset-halt")
            self.close_all("Kill-Switch")
            equity = self.broker.equity()

        # 3) Neue Kerzen auswerten
        for sym in self.cfg.symbols:
            if not self._bar_due(sym, now):
                continue
            feat = self.features(sym)
            bar = feat.index[-1]
            if self.last_bar.get(sym) == bar:
                continue
            self.last_bar[sym] = bar
            row = feat.iloc[-1]
            log.info("%s %s | Kurs %.6g | Kanal %.6g–%.6g | ATR %.4g | BTC %s | ADX %.1f RSI %.1f", sym,
                     bar.strftime("%m-%d %H:%M"), row["close"], row["donch_low"], row["donch_high"], row["atr"],
                     {True: "↑", False: "↓"}.get(row["btc_up"], "?"), row["adx"], row["rsi"])
            pos = self.broker.positions.get(sym)
            if pos and isinstance(self.broker, PaperBroker):
                # Funding simulieren (live bucht die Börse das selbst): Rate pro 8h anteilig je Kerze
                fr = self.x.funding_rate(sym)
                if fr:
                    self.broker.apply_funding(sym, fr * tf_delta(self.cfg.timeframes.entry) / pd.Timedelta("8h"))
            if pos:
                self._manage_position(sym, pos, feat)
            else:
                self._try_entry(sym, feat, equity)

        if time.time() - self._last_equity_log > 900:
            self.store.log_equity(now_iso(), self.mode_key, self.broker.equity())
            self._last_equity_log = time.time()
        self._persist_state()

    def daily_report(self, equity: float) -> None:
        trades = [t for t in self.store.closed_trades(self.mode_key, 200)
                  if (t["closed_at"] or "")[:10] == (self.risk.day and _yesterday(self.risk.day))]
        pnl = sum(t["pnl"] or 0 for t in trades)
        self.notify.send(f"📊 Tagesbericht: {len(trades)} Trades, PnL {pnl:+.2f} | Equity {equity:.2f} "
                         f"| Offene Positionen: {len(self.broker.positions)}\n{self.news.summary()}")

    def stop(self, *_):
        log.info("Stop-Signal empfangen – beende nach aktuellem Durchlauf (offene Positionen bleiben mit SL/TP bestehen)")
        self.running = False

    def run(self) -> None:
        os_signal.signal(os_signal.SIGTERM, self.stop)
        os_signal.signal(os_signal.SIGINT, self.stop)
        self.x.load_markets()
        for s in self.cfg.symbols:
            self.x.market(s)  # validiert Symbole
        self.restore()
        equity = self.broker.equity()
        self.risk.update_equity(equity)
        self.notify.send(
            f"🤖 Bot gestartet ({self.cfg.exchange.name}, {self.label}) | Equity {equity:.2f} "
            f"{self.cfg.exchange.quote_currency} | Symbole: {', '.join(self.cfg.symbols)} | "
            f"TF {self.cfg.timeframes.entry}/{self.cfg.timeframes.trend} | Max-Hebel {self.cfg.risk.max_leverage}x"
        )
        errors = 0
        while self.running:
            try:
                self.tick()
                errors = 0
            except Exception as e:
                errors += 1
                log.exception("Fehler im Durchlauf: %s", e)
                if errors in (3, 20):
                    self.notify.send(f"⚠️ {errors} Fehler in Folge: {type(e).__name__}: {e}")
            for _ in range(self.cfg.engine.poll_seconds * (1 if errors == 0 else min(errors, 6))):
                if not self.running:
                    break
                time.sleep(1)
        self._persist_state()
        self.notify.send("Bot gestoppt.")


def _yesterday(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

