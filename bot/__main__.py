"""Kommandozeile:  python -m bot <befehl>

  run          Bot starten (Paper oder Live, je nach config)
  analyze      Aktuelle Marktanalyse + News/Termine für alle Symbole anzeigen
  backtest     Strategie auf historischen Daten testen
  optimize     Parameter-Suche mit Out-of-Sample-Prüfung
  check        Verbindung, API-Keys, Kontostand und Symbole prüfen
  status       Offene Positionen und letzte Trades anzeigen
  reset-halt   Kill-Switch zurücksetzen
  close-all    Alle Positionen sofort schließen
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import ccxt

from .config import ConfigError, load_config
from .notifier import setup_logging

log = logging.getLogger("bot")


def cmd_run(cfg, args):
    from .engine import Engine

    if cfg.is_real_money:
        log.warning("=" * 60)
        log.warning(" ECHTGELD-MODUS AKTIV – es werden echte Orders platziert!")
        log.warning("=" * 60)
    Engine(cfg).run()


def cmd_analyze(cfg, args):
    from .analysis import compute_features, snapshot
    from .engine import BTC_SYMBOL
    from .exchange import Exchange
    from .strategy import Strategy

    x = Exchange(cfg.exchange, authenticated=False)
    strat = Strategy(cfg.strategy)
    tf = cfg.timeframes
    btc = x.fetch_ohlcv(BTC_SYMBOL, tf.entry, tf.history_bars) if cfg.strategy.btc_filter else None
    for sym in args.symbols or cfg.symbols:
        entry = x.fetch_ohlcv(sym, tf.entry, tf.history_bars)
        trend = x.fetch_ohlcv(sym, tf.trend, tf.history_bars)
        feat = compute_features(entry, trend, cfg.strategy, tf.entry, tf.trend, btc=btc)
        fr = x.funding_rate(sym)
        print(snapshot(sym, feat, fr).summary())
        sig = strat.entry_signal(feat, funding_rate=fr)
        if sig:
            tp = f"{sig.take_profit:.6g}" if sig.take_profit else "Trailing"
            print(f"  >>> SIGNAL: {sig.side.upper()} @ {sig.price:.6g} SL {sig.stop:.6g} TP {tp} ({sig.reason})")
        print()
    if cfg.news.enabled:
        from .news import NewsMonitor

        news = NewsMonitor(cfg.news, cfg.symbols)
        news.refresh(force=True)
        print("NEWS & EREIGNISSE\n" + news.summary())
        for sym in args.symbols or cfg.symbols:
            reason = news.blocked(sym)
            if reason:
                print(f"  {sym}: neue Trades gesperrt – {reason}")


def _load_data(cfg, args):
    """Liefert (data, limits, funding, btc). Standardquelle: offizielle Binance-Archive (inkl. Funding)."""
    import pandas as pd

    from .backtest import load_csv, load_or_fetch
    from .engine import BTC_SYMBOL
    from .exchange import Exchange

    tf = cfg.timeframes.entry
    if args.csv:
        return {args.symbols[0] if args.symbols else "CSV": load_csv(args.csv)}, {}, None, None
    symbols = list(args.symbols or cfg.symbols)
    # Vorlauf, damit Indikatoren (EMA200 etc.) zum Start bereits eingeschwungen sind
    warm_start = (pd.Timestamp(args.start) - cfg.timeframes.history_bars * pd.Timedelta(TF_HOURS[tf], "h")).strftime("%Y-%m-%d")
    data, limits, funding = {}, {}, {}
    if args.source == "vision":
        from .datasets import download_funding, download_klines

        for sym in dict.fromkeys(symbols + [BTC_SYMBOL]):
            log.info("Lade %s %s ab %s (Binance-Archiv) ...", sym, tf, warm_start)
            data[sym] = download_klines(sym, tf, warm_start, args.end)[["open", "high", "low", "close", "volume"]]
            funding[sym] = download_funding(sym, warm_start, args.end)
    else:
        x = Exchange(cfg.exchange, authenticated=False)
        for sym in dict.fromkeys(symbols + [BTC_SYMBOL]):
            log.info("Lade %s %s ab %s ...", sym, tf, warm_start)
            data[sym] = load_or_fetch(x, sym, tf, warm_start, args.end)
            lim = x.limits(sym)
            limits[sym] = dict(min_qty=lim.min_qty, qty_step=lim.qty_step, min_notional=lim.min_notional,
                               max_leverage_exchange=lim.max_leverage)
    btc = data[BTC_SYMBOL]
    data = {s: data[s] for s in symbols}
    return data, limits, funding or None, btc


TF_HOURS = {"1m": 1 / 60, "3m": 0.05, "5m": 1 / 12, "15m": 0.25, "30m": 0.5, "1h": 1, "2h": 2, "4h": 4,
            "6h": 6, "12h": 12, "1d": 24}


def cmd_backtest(cfg, args):
    import pandas as pd

    from .backtest import run_backtest, save_result

    data, limits, funding, btc = _load_data(cfg, args)
    start = None if args.csv else pd.Timestamp(args.start, tz="UTC")
    res = run_backtest(cfg, data, limits=limits, funding=funding, btc=btc, trade_start=start)
    print(res.report())
    eq = res.equity
    if len(eq):
        yearly = eq.resample("YE").last()
        prev = [res.start_equity] + list(yearly.values[:-1])
        print(" Jahresrenditen     : " + " | ".join(f"{t.year}: {(v / p - 1) * 100:+.1f}%"
                                                for t, v, p in zip(yearly.index, yearly.values, prev)))
    out = save_result(res)
    print(f"Trades & Equity-Kurve gespeichert in {out}/")


def cmd_optimize(cfg, args):
    from .backtest import optimize

    data, _, funding, btc = _load_data(cfg, args)
    print("Optimiere (kann einige Minuten dauern) ...")
    for i, r in enumerate(optimize(cfg, data, funding=funding, btc=btc), 1):
        tr, te = r["train"], r["test"]
        print(f"\n#{i} {json.dumps(r['params'])}")
        print(f"   Training: {tr['return_pct']:+.1f}% | DD {tr['max_drawdown_pct']:.1f}% | {tr['trades']} Trades | PF {tr['profit_factor']:.2f}")
        print(f"   Test    : {te['return_pct']:+.1f}% | DD {te['max_drawdown_pct']:.1f}% | {te['trades']} Trades | PF {te['profit_factor']:.2f}")
    print("\nNur Parameter übernehmen, die auch im TEST-Zeitraum überzeugen!")


def cmd_check(cfg, args):
    from .broker import LiveBroker
    from .exchange import Exchange

    x = Exchange(cfg.exchange, authenticated=True)
    x.load_markets()
    print(f"Börse: {x.id} | Sandbox: {cfg.exchange.sandbox} | Modus: {cfg.mode}")
    for sym in cfg.symbols:
        lim = x.limits(sym)
        print(f"  {sym}: Kurs {x.last_price(sym):.6g} | min. Menge {lim.min_qty} | Schritt {lim.qty_step} | "
              f"max. Hebel {lim.max_leverage}")
    if cfg.exchange.api_key:
        b = LiveBroker(x, cfg.exchange)
        print(f"  Equity: {b.equity():.2f} {cfg.exchange.quote_currency} | frei: {b.free_balance():.2f}")
        for p in b.exchange_positions():
            print(f"  Offene Position: {p['symbol']} {p['side']} {p['contracts']} @ {p.get('entryPrice')}")
    else:
        print("  Keine API-Keys gesetzt (für Paper-Modus nicht nötig).")
    print("OK")


def cmd_status(cfg, args):
    from .storage import Storage

    st = Storage(cfg.engine.db_path)
    key = f"{cfg.mode}:{cfg.exchange.name}:{cfg.exchange.sandbox}"
    risk = st.get_state(f"risk:{key}", {})
    print(f"Modus: {key}")
    if risk.get("halted"):
        print(f"!!! GESTOPPT: {risk.get('halt_reason')}")
    if key.startswith("paper"):
        print(f"Paper-Kontostand (realisiert): {st.get_state(f'paper_cash:{key}', cfg.risk.paper_start_equity):.2f}")
    print("\nOffene Positionen:")
    for t in st.open_trades(key):
        print(f"  #{t['id']} {t['symbol']} {t['side']} {t['qty']:.6g} @ {t['entry']:.6g} | SL {t['stop']:.6g} | "
              f"TP {t['take_profit'] or 0:.6g} | {t['leverage']}x | seit {t['opened_at']}")
    closed = st.closed_trades(key, args.limit)
    print(f"\nLetzte {len(closed)} Trades:")
    for t in closed:
        print(f"  #{t['id']} {t['closed_at'][:16]} {t['symbol']} {t['side']:5} {t['pnl']:+9.2f} ({t['reason_close']})")
    total = sum(t["pnl"] or 0 for t in st.closed_trades(key, 100000))
    print(f"\nRealisierter PnL gesamt: {total:+.2f}")


def cmd_reset_halt(cfg, args):
    from .storage import Storage

    st = Storage(cfg.engine.db_path)
    key = f"risk:{cfg.mode}:{cfg.exchange.name}:{cfg.exchange.sandbox}"
    s = st.get_state(key, {})
    s.update(halted=False, halt_reason="", peak_equity=None, cooldown_until=None)
    st.set_state(key, s)
    print("Kill-Switch zurückgesetzt. Höchststand wird beim nächsten Start neu gesetzt.")


def cmd_close_all(cfg, args):
    from .engine import Engine

    e = Engine(cfg)
    e.x.load_markets()
    e.restore()
    e.close_all("Manuell (close-all)")
    print("Alle verwalteten Positionen geschlossen.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m bot", description="Krypto-Futures-Trading-Bot")
    p.add_argument("-c", "--config", default="config/config.yaml")
    p.add_argument("--env", default=".env")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    a = sub.add_parser("analyze")
    a.add_argument("symbols", nargs="*")
    for name in ("backtest", "optimize"):
        b = sub.add_parser(name)
        b.add_argument("--start", default="2022-01-01")
        b.add_argument("--end", default=None)
        b.add_argument("--csv", default=None, help="eigene OHLCV-CSV statt Börsendaten")
        b.add_argument("--source", choices=("vision", "exchange"), default="vision",
                       help="vision = offizielle Binance-Archive inkl. Funding (schnell), exchange = Börsen-API")
        b.add_argument("symbols", nargs="*")
    sub.add_parser("check")
    s = sub.add_parser("status")
    s.add_argument("--limit", type=int, default=20)
    sub.add_parser("reset-halt")
    sub.add_parser("close-all")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config, args.env)
    except ConfigError as e:
        print(f"Konfigurationsfehler: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.engine.log_level, cfg.engine.log_file if args.cmd == "run" else None)
    handlers = {
        "run": cmd_run, "analyze": cmd_analyze, "backtest": cmd_backtest, "optimize": cmd_optimize,
        "check": cmd_check, "status": cmd_status, "reset-halt": cmd_reset_halt, "close-all": cmd_close_all,
    }
    try:
        handlers[args.cmd](cfg, args)
    except ccxt.AuthenticationError as e:
        print(f"API-Key abgelehnt: {e}\nKey/Secret, Berechtigungen (Futures) und IP-Whitelist prüfen.", file=sys.stderr)
        return 3
    except ccxt.NetworkError as e:
        print(f"Börse nicht erreichbar: {e}\nInternetverbindung/Firewall prüfen oder später erneut versuchen.", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
