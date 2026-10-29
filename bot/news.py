"""News & Ereignisse: Wirtschaftskalender, Krypto-Schlagzeilen, Binance-Delistings, Fear & Greed.

Wichtig – was der Backtest gezeigt hat (siehe README):
  * Kurse reagieren auf News schneller, als ein Bot Schlagzeilen lesen kann. Die besten Breakouts
    entstehen gerade auf News-Kerzen – News werden deshalb NICHT als Kauf-/Verkaufssignal genutzt.
  * News dienen als Risikoschutz: keine NEUEN Einstiege rund um Zinsentscheidungen/Makrodaten und
    nach kritischen Meldungen (Hack, Delisting, Klage …) zu einem Coin. Offene Positionen behalten
    ihren Stop; nur bei einem Futures-Delisting wird (optional) geschlossen.

Alle Abrufe sind fehlertolerant: fällt eine Quelle aus, handelt der Bot normal weiter.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (cryptobot news monitor)"}

# Zinsentscheidungen der US-Notenbank (2. Sitzungstag, 14:00 New York), Quelle: federalreserve.gov
FOMC_DECISION_DATES = [
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16", "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]

DEFAULT_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BINANCE_DELIST_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                      "?type=1&catalogId=161&pageNo=1&pageSize=20")
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"

# Namen, unter denen Coins in Schlagzeilen auftauchen (Ticker werden zusätzlich als GROSSES Wort erkannt)
COIN_ALIASES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "BNB": ["bnb chain", "bnb smart chain"],
    "XRP": ["ripple"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "AVAX": ["avalanche"], "LINK": ["chainlink"],
    "LTC": ["litecoin"], "DOT": ["polkadot"], "TRX": ["tron"], "BCH": ["bitcoin cash"], "ATOM": ["cosmos"],
    "NEAR": ["near protocol"], "UNI": ["uniswap"], "ETC": ["ethereum classic"], "FIL": ["filecoin"],
}
# Kritische Meldungen (Wortanfänge, Groß-/Kleinschreibung egal)
CRITICAL_PATTERNS = {
    "hack": r"\b(hack(s|ed|er|ers)?|exploit(s|ed)?|drain(s|ed)?|stolen|breach(ed)?|attack(ed)?)\b",
    "delisting": r"\b(delist(s|ed|ing)?|will remove)\b",
    "regulatory": r"\b(sec (sues|charges)|lawsuit|indict(s|ed|ment)?|(?<!un)ban(s|ned)?\b|cftc charges)",
    "insolvency": r"\b(halts? withdrawals|suspends? withdrawals|insolven(t|cy)|bankrupt(cy)?|collapse[sd]?)\b",
    "depeg": r"\b(de-?peg(s|ged)?|loses? (its )?peg)\b",
    "outage": r"\b(network (halt|outage)|chain halt(s|ed)?|stops producing blocks)\b",
}
# Betreffen den ganzen Markt (Stablecoins, große Börsen) – nur bei echtem Geldverlust/Krise
MARKET_WIDE = r"\b(tether|usdt|usdc|stablecoin|binance|coinbase|bybit|okx)\b"
FUNDS_LOST = r"\b(stolen|drain(s|ed)?|exploit(s|ed)?)\b"
# Datenlecks/Phishing bewegen den Kurs kaum → keine Sperre
NOT_CRITICAL = r"\b(data|personal|phishing|kyc|privacy|leak(s|ed)?)\b"


@dataclass
class NewsEvent:
    time: datetime
    source: str
    title: str
    kind: str                  # hack | delisting | regulatory | insolvency | depeg | outage | macro
    coins: set[str] = field(default_factory=set)
    market_wide: bool = False
    futures_delisting: bool = False
    link: str = ""

    def describe(self) -> str:
        scope = "GANZER MARKT" if self.market_wide else ", ".join(sorted(self.coins)) or "-"
        return f"[{self.kind}] {scope}: {self.title} ({self.source}, {self.time:%d.%m. %H:%M} UTC)"


@dataclass
class MacroEvent:
    time: datetime
    title: str
    source: str = "calendar"


def fomc_events() -> list[MacroEvent]:
    import pandas as pd

    out = []
    for d in FOMC_DECISION_DATES:
        t = pd.Timestamp(f"{d} 14:00").tz_localize("America/New_York").tz_convert("UTC").to_pydatetime()
        out.append(MacroEvent(t, "FOMC-Zinsentscheidung", "federalreserve.gov"))
    return out


def classify(title: str, coins: list[str]) -> NewsEvent | None:
    """Ordnet eine Schlagzeile einer kritischen Kategorie und den betroffenen Coins zu."""
    low = title.lower()
    kind = next((k for k, pat in CRITICAL_PATTERNS.items() if re.search(pat, low)), None)
    if kind is None or (kind == "hack" and re.search(NOT_CRITICAL, low)):
        return None
    hit = set()
    for c in coins:
        if re.search(rf"\b{re.escape(c)}\b", title) or any(re.search(rf"\b{re.escape(a)}\b", low)
                                                          for a in COIN_ALIASES.get(c, [])):
            hit.add(c)
    # "Bitcoin Cash"/"Ethereum Classic" nicht zusätzlich BTC/ETH zuordnen
    if "bitcoin cash" in low and not re.search(r"\bbitcoin(?! cash)\b", low):
        hit.discard("BTC")
    if "ethereum classic" in low and not re.search(r"\bethereum(?! classic)\b", low):
        hit.discard("ETH")
    market = bool(re.search(MARKET_WIDE, low)) and (
        kind in ("insolvency", "depeg") or (kind == "hack" and bool(re.search(FUNDS_LOST, low))))
    if not hit and not market:
        return None
    return NewsEvent(datetime.now(timezone.utc), "", title, kind, hit, market)


def parse_rss(xml_text: str, source: str, coins: list[str]) -> list[NewsEvent]:
    events = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        ev = classify(title, coins)
        if ev is None:
            continue
        try:
            ev.time = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
        ev.source, ev.link = source, (item.findtext("link") or "").strip()
        events.append(ev)
    return events


def parse_binance_delistings(data: dict, coins: list[str]) -> list[NewsEvent]:
    events = []
    for cat in (data.get("data") or {}).get("catalogs", []):
        for a in cat.get("articles", []):
            title = a.get("title", "")
            futures = "futures" in title.lower() and "delist" in title.lower()
            listed = set(re.findall(r"\b([A-Z0-9]{2,15})USDT\b", title))
            words = set(re.findall(r"\b[A-Z0-9]{2,15}\b", title))
            hit = {c for c in coins if c in listed or (not futures and c in words)}
            if not hit:
                continue
            t = datetime.fromtimestamp(a.get("releaseDate", 0) / 1000, timezone.utc)
            events.append(NewsEvent(t, "binance", title, "delisting", hit, False, futures,
                                    f"https://www.binance.com/en/support/announcement/{a.get('code', '')}"))
    return events


def parse_calendar(data: list, impact: str = "High", country: str = "USD") -> list[MacroEvent]:
    out = []
    for e in data:
        if e.get("impact") != impact or e.get("country") != country:
            continue
        try:
            t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        out.append(MacroEvent(t, e.get("title", "?"), "forexfactory"))
    return out


class NewsMonitor:
    def __init__(self, cfg, symbols: list[str], http=None):
        self.cfg = cfg
        self.coins = sorted({s.split("/")[0] for s in symbols})
        self.http = http or requests
        self.events: list[NewsEvent] = []
        self.macro: list[MacroEvent] = fomc_events()
        self.fear_greed: tuple[int, str] | None = None
        self._seen: set[str] = set()
        self._last_poll = 0.0
        self._last_calendar = 0.0

    # ---------- Abruf ----------
    def _get(self, url: str):
        r = self.http.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        return r

    def refresh(self, force: bool = False) -> list[NewsEvent]:
        """Holt neue Meldungen (höchstens alle poll_minutes). Gibt nur NEUE relevante Ereignisse zurück."""
        if not self.cfg.enabled:
            return []
        now = time.time()
        if not force and now - self._last_poll < self.cfg.poll_minutes * 60:
            return []
        self._last_poll = now
        found: list[NewsEvent] = []
        if self.cfg.headline_filter:
            for url in self.cfg.feeds:
                try:
                    found += parse_rss(self._get(url).text, url.split("/")[2], self.coins)
                except Exception as e:
                    log.debug("Feed %s nicht verfügbar: %s", url, e)
            try:
                found += parse_binance_delistings(self._get(BINANCE_DELIST_URL).json(), self.coins)
            except Exception as e:
                log.debug("Binance-Ankündigungen nicht verfügbar: %s", e)
        if self.cfg.macro_filter and (force or now - self._last_calendar > 6 * 3600):
            try:
                cal = parse_calendar(self._get(CALENDAR_URL).json())
                known = {(m.time, m.title) for m in self.macro}
                self.macro += [m for m in cal if (m.time, m.title) not in known]
                self._last_calendar = now
            except Exception as e:
                log.debug("Wirtschaftskalender nicht verfügbar: %s", e)
        try:
            d = self._get(FEAR_GREED_URL).json()["data"][0]
            self.fear_greed = (int(d["value"]), d["value_classification"])
        except Exception as e:
            log.debug("Fear & Greed nicht verfügbar: %s", e)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(self.cfg.headline_block_hours, 24))
        fresh = []
        for ev in found:
            key = ev.link or ev.title
            if key in self._seen or ev.time < cutoff:
                continue
            self._seen.add(key)
            self.events.append(ev)
            fresh.append(ev)
        self.events = [e for e in self.events if e.time >= cutoff or e.futures_delisting]
        return fresh

    # ---------- Abfragen ----------
    def macro_blocked(self, now: datetime) -> MacroEvent | None:
        if not self.cfg.macro_filter:
            return None
        before = timedelta(hours=self.cfg.macro_block_before_hours)
        after = timedelta(hours=self.cfg.macro_block_after_hours)
        return next((m for m in self.macro if m.time - before <= now <= m.time + after), None)

    def blocked(self, symbol: str, now: datetime | None = None) -> str | None:
        """Grund, warum für dieses Symbol gerade KEIN neuer Trade eröffnet werden soll (sonst None)."""
        if not self.cfg.enabled:
            return None
        now = now or datetime.now(timezone.utc)
        m = self.macro_blocked(now)
        if m:
            return f"Makro-Termin {m.title} um {m.time:%d.%m. %H:%M} UTC"
        coin = symbol.split("/")[0]
        window = timedelta(hours=self.cfg.headline_block_hours)
        for ev in self.events:
            if ev.futures_delisting and coin in ev.coins:
                return f"Futures-Delisting angekündigt: {ev.title}"
        if self.cfg.headline_filter:
            recent = [e for e in self.events if now - e.time <= window]
            for scope, evs in (("Markt", [e for e in recent if e.market_wide]),
                               (coin, [e for e in recent if coin in e.coins])):
                # Sperre nur, wenn mehrere unabhängige Quellen berichten (oder Binance offiziell)
                sources = {e.source for e in evs}
                if evs and ("binance" in sources or len(sources) >= self.cfg.headline_min_sources):
                    return f"Kritische Meldung {scope} ({evs[-1].kind}, {len(sources)} Quellen): {evs[-1].title}"
        if self.cfg.fear_greed_filter and self.fear_greed:
            v = self.fear_greed[0]
            if v >= self.cfg.greed_max or v <= self.cfg.fear_min:
                return f"Fear & Greed extrem ({v})"
        return None

    def delisted(self, symbol: str) -> NewsEvent | None:
        coin = symbol.split("/")[0]
        return next((e for e in self.events if e.futures_delisting and coin in e.coins), None)

    def upcoming(self, now: datetime | None = None, hours: int = 48) -> list[MacroEvent]:
        now = now or datetime.now(timezone.utc)
        return sorted((m for m in self.macro if now <= m.time <= now + timedelta(hours=hours)), key=lambda m: m.time)

    def summary(self) -> str:
        lines = []
        if self.fear_greed:
            lines.append(f"Fear & Greed: {self.fear_greed[0]} ({self.fear_greed[1]})")
        up = self.upcoming()
        lines.append("Makro-Termine (48h): " + (", ".join(f"{m.title} {m.time:%d.%m. %H:%M}" for m in up) or "keine"))
        lines.append(f"Kritische Meldungen ({self.cfg.headline_block_hours}h): " + (str(len(self.events)) if self.events else "keine"))
        lines += ["  " + e.describe() for e in self.events[-10:]]
        return "\n".join(lines)
