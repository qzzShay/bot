"""News-Modul: Erkennung, Quellen-Parsing, Sperrlogik, Fehlertoleranz, Delisting in der Engine."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.config import NewsConfig
from bot.news import (BINANCE_DELIST_URL, CALENDAR_URL, FEAR_GREED_URL, NewsEvent, NewsMonitor, classify,
                      fomc_events, parse_binance_delistings, parse_calendar, parse_rss)

COINS = ["BTC", "ETH", "SOL", "XRP", "LINK", "BCH"]
NOW = datetime.now(timezone.utc)


def rfc822(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def rss(*titles, when=None):
    when = when or NOW
    items = "".join(f"<item><title>{t}</title><link>https://x/{i}</link><pubDate>{rfc822(when)}</pubDate></item>"
                    for i, t in enumerate(titles))
    return f"<rss><channel>{items}</channel></rss>"


@pytest.mark.parametrize("title,kind,coins,market", [
    ("Bybit hacked: $1.4 billion in ETH drained from cold wallet", "hack", {"ETH"}, True),
    ("Tether's USDT loses peg, falls to $0.95", "depeg", set(), True),
    ("SEC sues Ripple over XRP sales", "regulatory", {"XRP"}, False),
    ("Solana network halt: chain stops producing blocks", "outage", {"SOL"}, False),
    ("Binance halts withdrawals amid surge", "insolvency", set(), True),
    ("Bitcoin Cash exploit drains $5m", "hack", {"BCH"}, False),
])
def test_classify_critical(title, kind, coins, market):
    ev = classify(title, COINS)
    assert ev.kind == kind and ev.coins == coins and ev.market_wide == market


@pytest.mark.parametrize("title", [
    "Bitcoin hits new all-time high",
    "Hackathon winners announced on Solana",
    "Coinbase Customer Data Hit in Analytics Vendor Breach, Funds Not Impacted",
    "Chainlink partners with Swift",
    "Click this link to read more about the exploit",   # 'link' klein ≠ LINK
])
def test_classify_ignores_noise(title):
    assert classify(title, COINS) is None


def test_parse_rss_and_dates():
    evs = parse_rss(rss("SEC sues Ripple over XRP sales", "ETH rallies 5%"), "feedA", COINS)
    assert len(evs) == 1 and evs[0].source == "feedA" and abs((evs[0].time - NOW).total_seconds()) < 2


def test_parse_binance_futures_delisting():
    data = {"data": {"catalogs": [{"articles": [
        {"title": "Binance Futures Will Delist LINKUSDT and FOOUSDT USDⓈ-M Perpetual Contracts", "releaseDate": 1_790_000_000_000, "code": "a"},
        {"title": "Notice of Removal of Spot Trading Pairs - 2026-10-30", "releaseDate": 1_790_000_000_000, "code": "b"},
    ]}]}}
    evs = parse_binance_delistings(data, COINS)
    assert len(evs) == 1 and evs[0].coins == {"LINK"} and evs[0].futures_delisting


def test_parse_calendar_high_impact_usd_only():
    data = [{"title": "CPI m/m", "country": "USD", "impact": "High", "date": "2026-11-12T08:30:00-05:00"},
            {"title": "ECB", "country": "EUR", "impact": "High", "date": "2026-11-12T08:30:00-05:00"},
            {"title": "Retail", "country": "USD", "impact": "Medium", "date": "2026-11-12T08:30:00-05:00"}]
    ev = parse_calendar(data)
    assert [e.title for e in ev] == ["CPI m/m"] and ev[0].time.hour == 13


def test_fomc_dates_in_utc():
    ev = fomc_events()
    assert len(ev) == 56
    jan21 = ev[0].time
    assert (jan21.year, jan21.month, jan21.day, jan21.hour) == (2021, 1, 27, 19)   # 14:00 EST
    jun = next(e.time for e in ev if e.time.year == 2024 and e.time.month == 6)
    assert jun.hour == 18                                                            # 14:00 EDT


def test_macro_window_blocks_entries():
    m = NewsMonitor(NewsConfig(macro_block_before_hours=4, macro_block_after_hours=4), [])
    fomc = next(e.time for e in m.macro if e.time.year == 2025)
    assert m.blocked("BTC/USDT:USDT", fomc - timedelta(hours=3))
    assert m.blocked("BTC/USDT:USDT", fomc + timedelta(hours=4))
    assert m.blocked("BTC/USDT:USDT", fomc + timedelta(hours=5)) is None
    assert NewsMonitor(NewsConfig(macro_filter=False), []).blocked("BTC/USDT:USDT", fomc) is None


def test_headline_needs_two_sources_unless_binance():
    m = NewsMonitor(NewsConfig(macro_filter=False, headline_min_sources=2), ["XRP/USDT:USDT", "ETH/USDT:USDT"])
    ev = classify("SEC sues Ripple over XRP sales", ["XRP"])
    ev.time, ev.source = NOW, "feedA"
    m.events.append(ev)
    assert m.blocked("XRP/USDT:USDT", NOW) is None                  # nur 1 Quelle
    ev2 = classify("SEC charges Ripple, XRP drops", ["XRP"])
    ev2.time, ev2.source = NOW, "feedB"
    m.events.append(ev2)
    assert "XRP" in m.blocked("XRP/USDT:USDT", NOW)
    assert m.blocked("ETH/USDT:USDT", NOW) is None                  # anderer Coin unberührt
    assert m.blocked("XRP/USDT:USDT", NOW + timedelta(hours=13)) is None   # Sperre läuft ab


def test_market_wide_event_blocks_all():
    m = NewsMonitor(NewsConfig(macro_filter=False), ["BTC/USDT:USDT"])
    for src in ("a", "b"):
        e = classify("Tether's USDT loses peg", ["BTC"])
        e.time, e.source = NOW, src
        m.events.append(e)
    assert "Markt" in m.blocked("BTC/USDT:USDT", NOW)


def test_futures_delisting_blocks_permanently():
    m = NewsMonitor(NewsConfig(macro_filter=False), ["LINK/USDT:USDT"])
    m.events.append(NewsEvent(NOW - timedelta(days=5), "binance", "Binance Futures Will Delist LINKUSDT", "delisting",
                              {"LINK"}, False, True))
    assert "Delisting" in m.blocked("LINK/USDT:USDT", NOW)
    assert m.delisted("LINK/USDT:USDT")


class FakeResp:
    def __init__(self, text="", data=None):
        self.text, self._data = text, data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, routes, fail=()):
        self.routes, self.fail, self.calls = routes, set(fail), []

    def get(self, url, **kw):
        self.calls.append(url)
        if url in self.fail:
            raise ConnectionError("down")
        return self.routes[url]


def make_http(fail=()):
    feed = NewsConfig().feeds[0]
    routes = {u: FakeResp(rss()) for u in NewsConfig().feeds}
    routes[feed] = FakeResp(rss("SEC sues Ripple over XRP sales"))
    routes[BINANCE_DELIST_URL] = FakeResp(data={"data": {"catalogs": []}})
    routes[CALENDAR_URL] = FakeResp(data=[])
    routes[FEAR_GREED_URL] = FakeResp(data={"data": [{"value": "77", "value_classification": "Greed"}]})
    return FakeHttp(routes, fail)


def test_refresh_returns_only_new_events_and_respects_interval():
    http = make_http()
    m = NewsMonitor(NewsConfig(poll_minutes=5), ["XRP/USDT:USDT"], http=http)
    assert len(m.refresh(force=True)) == 1
    assert m.fear_greed == (77, "Greed")
    assert m.refresh(force=True) == []          # schon bekannt
    n = len(http.calls)
    assert m.refresh() == [] and len(http.calls) == n   # Intervall noch nicht um → kein Abruf


def test_refresh_survives_failing_sources():
    feeds = NewsConfig().feeds
    http = make_http(fail=set(feeds[1:]) | {BINANCE_DELIST_URL, CALENDAR_URL, FEAR_GREED_URL})
    m = NewsMonitor(NewsConfig(), ["XRP/USDT:USDT"], http=http)
    assert len(m.refresh(force=True)) == 1      # eine Quelle reicht, Rest fällt still aus


def test_engine_closes_position_on_futures_delisting(cfg):
    from test_engine import FakeExchange, make_engine
    from conftest import make_ohlcv
    from bot.broker import Position
    from bot.storage import Storage

    fx = FakeExchange({"LINK/USDT:USDT": make_ohlcv(seed=3, n=600, freq="4h")})
    e = make_engine(cfg, fx, Storage(":memory:"))
    cfg.news.enabled = True
    e.news.cfg.enabled = True
    e.broker.positions["LINK/USDT:USDT"] = Position(side="long", entry=100, stop=90, initial_stop=90,
                                                    take_profit=None, best_price=100, symbol="LINK/USDT:USDT", qty=1)
    delist = NewsEvent(NOW, "binance", "Binance Futures Will Delist LINKUSDT", "delisting", {"LINK"}, False, True)
    e.news.refresh = lambda: [delist]
    e._check_news()
    assert "LINK/USDT:USDT" not in e.broker.positions
