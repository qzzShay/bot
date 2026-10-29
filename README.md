# CryptoBot – automatischer Krypto-Futures-Trading-Bot

Python-Bot für USDT-Perpetual-Futures (Binance, Bybit, OKX, Bitget via [ccxt](https://github.com/ccxt/ccxt))
mit Hebel, Marktanalyse, striktem Risikomanagement, Backtester und VPS-Deployment.

Die Standardstrategie (Donchian-Trendfolge auf 4h) wurde auf **echten Binance-Futures-Daten 2021–2026**
entwickelt und mit Coins und Zeiträumen geprüft, die bei der Entwicklung nicht benutzt wurden – siehe
[Backtest-Ergebnisse](#backtest-ergebnisse).

> ⚠️ **Risikohinweis:** Hebel-Trading kann zum Totalverlust des eingesetzten Kapitals führen. Keine
> Strategie ist garantiert profitabel – auch diese nicht. Der Bot ist ein Werkzeug, keine Gelddruckmaschine.
> Setze nur Geld ein, dessen Verlust du verkraften kannst, und gehe die Stufen unten der Reihe nach durch.

---

## Funktionen

| Bereich | Was drin ist |
|---|---|
| **Marktanalyse** | EMA (21/55/200), RSI, MACD, Bollinger Bands, ATR, ADX/DI, Supertrend, Stoch-RSI, VWAP, Volumen, Support/Resistance (Pivots), Funding-Rate |
| **Strategie** | Donchian-Breakout auf 4h (55-Kerzen-Hoch/-Tief) mit ATR-Stop und weitem Trailing-Stop – Gewinner laufen lassen, Verlierer früh schließen |
| **Marktregime** | BTC-Trendfilter: Longs nur, wenn BTC über EMA200 (4h), Shorts nur darunter |
| **Funding** | Kein Einstieg in überfüllte Trades (24h-Funding stark gegen die Position); Funding-Kosten im Backtest und Paper-Modus |
| **News** | Wirtschaftskalender (FOMC, CPI, NFP …), Krypto-Schlagzeilen (Cointelegraph, The Block, Decrypt, CoinDesk), Binance-Delistings, Fear & Greed – als Risikoschutz mit Telegram-Alarm |
| **Alternative** | Alte Indikator-Konfluenz (`strategy.type: confluence`) bleibt wählbar – verlor aber im Backtest in jedem Jahr |
| **Hebel** | Automatisch: so wenig wie nötig, max. `max_leverage`; Liquidationspreis muss *hinter* dem Stop liegen |
| **Positionsgröße** | Risikobasiert: fester %-Satz des Kontos pro Trade (Abstand Entry → Stop) |
| **Exits** | Stop-Loss (2 × ATR), Chandelier-Trailing-Stop (6 × ATR); optional Take-Profit, Break-Even, Gegensignal |
| **Absicherung** | SL & TP liegen als `reduceOnly`-Orders **auf der Börse** – Position bleibt geschützt, auch wenn der Bot/VPS abstürzt. Scheitert der SL, wird sofort geschlossen |
| **Risikolimits** | 0,5 % Risiko pro Trade (Shorts 0,25 %), max. 3 % Gesamtrisiko, max. 4 Positionen, Tagesverlust-Limit, Drawdown-Kill-Switch, Cooldown |
| **Modi** | `paper` (echte Live-Kurse, simulierte Orders) · `live` + `demo/testnet` (Spielgeld auf der Börse) · `live` + `none` (Echtgeld, doppelt gesichert) |
| **Backtest** | Gleicher Code wie live, mit Gebühren, Slippage, Funding, Multi-Symbol-Portfolio, Jahresrenditen, CSV-Export; Daten direkt aus den offiziellen Binance-Archiven |
| **Optimierung** | Grid-Search mit Train/Test-Split (Out-of-Sample) gegen Overfitting |
| **Betrieb** | SQLite-Persistenz (Neustart-sicher), Telegram-Benachrichtigungen, Tagesbericht, Docker & systemd, Log-Rotation |

---

## Schnellstart (lokal)

```bash
git clone <dein-repo> cryptobot && cd cryptobot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml
cp .env.example .env && chmod 600 .env

python -m bot analyze                       # aktuelle Marktanalyse (echte Daten, kein Key nötig)
python -m bot backtest --start 2022-01-01   # Strategie auf echten historischen Daten testen
python -m bot run                           # Paper-Trading mit Live-Kursen
```

## Der Weg zu echtem Geld – bitte in dieser Reihenfolge

1. **Backtest** auf echten Daten (`python -m bot backtest --start 2022-01-01`). Mehrere Zeiträume testen
   (Bullen-, Bären- und Seitwärtsmarkt). Wenn der Backtest nicht überzeugt, nicht weitermachen.
2. **Optimieren** (optional): `python -m bot optimize --start 2022-01-01`. Nur Parameter übernehmen, die auch im
   *Test*-Zeitraum gut sind. Die Standardwerte sind bewusst aus der Mitte eines stabilen Bereichs gewählt.
3. **Paper-Trading** (`mode: paper`) mindestens 2–4 Wochen auf dem VPS laufen lassen.
4. **Demo/Testnet** (`mode: live`, `sandbox: demo`) mit Demo-API-Keys der Börse – prüft den kompletten
   Order-Ablauf (Hebel, SL/TP-Orders, Trailing) ohne echtes Geld.
   - Binance: Demo-Trading auf binance.com aktivieren → API-Key im Demo-Bereich erstellen
   - Bybit: `sandbox: testnet` mit Key von testnet.bybit.com, oder `demo` mit Demo-Trading-Key
5. **Echtgeld, klein anfangen** (`mode: live`, `sandbox: none`):
   - in `.env`: `CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISK` (ohne diesen Wert verweigert der Bot den Start)
   - Unterkonto mit kleinem Betrag, `risk_per_trade_pct: 0.25–0.5`, `max_leverage: 3`
   - erst nach Wochen stabiler Ergebnisse langsam erhöhen

## API-Key-Sicherheit

- Nur **Futures-Trading** erlauben – **Auszahlungen immer deaktiviert lassen**
- Key auf die **IP deines VPS** beschränken
- Am besten ein **Unterkonto** nur für den Bot nutzen
- `.env` niemals committen (steht in `.gitignore`), Rechte `chmod 600 .env`

---

## Deployment auf dem VPS

Empfohlen: Ubuntu 22.04/24.04, 1 vCPU, 1 GB RAM reicht. Der VPS sollte in einer Region stehen, aus der deine
Börse erreichbar ist (z.B. blockiert Binance US-IPs, Bybit u.a. US/UK).

### Variante A: systemd (einfach)

```bash
git clone <dein-repo> cryptobot && cd cryptobot
sudo bash deploy/install_vps.sh            # installiert nach /opt/cryptobot als Dienst
sudo nano /opt/cryptobot/.env              # Keys + Telegram
sudo nano /opt/cryptobot/config/config.yaml
cd /opt/cryptobot && sudo -u cryptobot .venv/bin/python -m bot check
sudo systemctl start cryptobot
journalctl -u cryptobot -f                 # Live-Logs
```

Update: `git pull && sudo bash deploy/install_vps.sh && sudo systemctl restart cryptobot`
(Konfiguration, `.env` und Datenbank bleiben erhalten – neue Standardwerte also ggf. aus
`config/config.example.yaml` in deine `config.yaml` übernehmen.)

### Variante B: Docker

```bash
cp config/config.example.yaml config/config.yaml && cp .env.example .env && chmod 600 .env
mkdir -p data && sudo chown -R 1000:1000 data   # Container läuft als Nicht-root-User (UID 1000)
# config.yaml + .env bearbeiten
docker compose up -d --build
docker compose logs -f
docker compose run --rm cryptobot status
```

---

## Befehle

| Befehl | Beschreibung |
|---|---|
| `python -m bot run` | Bot starten |
| `python -m bot analyze [SYMBOL ...]` | Marktanalyse + aktuelles Signal |
| `python -m bot backtest --start 2022-01-01 [--end ...] [SYMBOL ...]` | Backtest mit Binance-Archivdaten inkl. Funding (Cache in `data/vision`) |
| `python -m bot backtest --source exchange ...` | Backtest mit Daten der Börsen-API (z.B. für Bybit/OKX) |
| `python -m bot backtest --csv datei.csv SYMBOL` | Backtest mit eigener CSV (`timestamp,open,high,low,close,volume`) |
| `python -m bot optimize --start 2023-01-01` | Parameter-Suche mit Out-of-Sample-Test |
| `python -m bot check` | Verbindung, Keys, Kontostand, Symbol-Limits prüfen |
| `python -m bot status` | Offene Positionen, letzte Trades, PnL |
| `python -m bot close-all` | Alle vom Bot verwalteten Positionen sofort schließen |
| `python -m bot reset-halt` | Kill-Switch nach Prüfung zurücksetzen |

Den Bot stoppen (`systemctl stop` / `Ctrl+C`) schließt **keine** Positionen – sie bleiben mit SL/TP auf der
Börse geschützt und werden nach dem Neustart weiter verwaltet.

---

## Wie die Strategie entscheidet

Alle 4 Stunden, nach Schluss jeder 4h-Kerze, pro Coin:

1. **Ausbruch:** Schlusskurs über dem Hoch der letzten 55 Kerzen → Long-Kandidat; unter dem Tief → Short-Kandidat.
2. **Marktregime:** Long nur, wenn BTC über seinem EMA200 (4h) liegt; Short nur, wenn darunter.
   Kryptos laufen stark gemeinsam – gegen den BTC-Trend zu handeln war im Test klar schlechter.
3. **Funding-Filter:** Kein Long, wenn Longs in den letzten 24h mehr als 0,09 % Funding gezahlt haben (überfüllt), Shorts analog.
4. **Stop:** 2 × ATR(14) unter/über dem Einstieg, sofort als Order auf der Börse.
5. **Trailing:** Der Stop folgt dem höchsten Kurs seit Einstieg mit 6 × ATR Abstand (nur in Gewinnrichtung).
   Kein festes Kursziel – die wenigen großen Trends bringen den Gewinn.
6. **Größe:** 0,5 % Risiko (Shorts 0,25 %), solange das Gesamtrisiko aller offenen Positionen unter 3 % bleibt.

Erwartung: ~30 % Trefferquote. Viele kleine Verluste, wenige große Gewinne (Ø Gewinn ≈ 3,5 × Ø Verlust).
Verlustserien von 10+ Trades sind normal und kein Fehler.

**Positionsgröße – Beispiel:** Konto 1.000 USDT, Risiko 0,5 % = 5 USDT. BTC-Entry 60.000, ATR 900 → Stop
58.200 (1.800 Abstand) → 0,0028 BTC ≈ 167 USDT Notional → **Hebel 1x**. Stop getroffen = −5 USDT (+ Gebühren).
Der Hebel bestimmt also *nicht* das Risiko – das tut der Stop-Abstand. Hebel spart nur Margin.

Alle Parameter stehen kommentiert in `config/config.example.yaml`.

---

## News & Ereignisse

**Was getestet wurde** (gleiches Verfahren wie bei der Strategie, echte Daten 2021–2026):

| Idee | Ergebnis | Entscheidung |
|---|---|---|
| Keine Trades ±4–12 h um FOMC-Zinsentscheidungen | neutral (+297 % statt +295 %) | **an** – kostet nichts, schützt vor Slippage bei Zins-Spikes |
| Fear & Greed (keine Longs bei Gier, keine Shorts bei Angst) | mal besser, mal schlechter – nicht stabil | **aus** (nur Anzeige) |
| Keine Einstiege auf extremen News-Kerzen | **schlechter** in allen Tests | **nicht verwendet** |

Kernerkenntnis: Der Kurs reagiert auf News schneller, als ein Bot Schlagzeilen lesen kann – und die besten
Ausbrüche *entstehen* auf News-Kerzen. Die Trendfolge handelt News also bereits über den Preis. Schlagzeilen
als Kaufsignal zu nutzen, würde zu spät kommen.

**Wofür News im Bot genutzt werden – Schutz vor Ereignissen, die der Kurs vorher nicht zeigt:**

- **Futures-Delisting** (offizielle Binance-Ankündigung) → Coin gesperrt, offene Position wird geschlossen
- **Hack, Klage, Depeg, Netzwerk-Ausfall** zu einem gehandelten Coin → 12 h keine neuen Trades auf diesem Coin
  (erst wenn ≥ 2 unabhängige Quellen berichten – verhindert Fehlalarme; Datenlecks/Phishing werden ignoriert)
- **Marktweite Krise** (Stablecoin-Depeg, Börse friert Auszahlungen ein, große Börse gehackt) → alle Coins gesperrt
- **Makro-Termine** (FOMC, CPI, NFP, PCE, GDP – USD, hohe Wichtigkeit) → ±4 h keine neuen Trades
- **Telegram-Alarm** bei jeder kritischen Meldung; Tagesbericht mit Fear & Greed und anstehenden Terminen

Offene Positionen behalten immer ihren Stop auf der Börse. Fällt eine News-Quelle aus, handelt der Bot normal weiter.
`python -m bot analyze` zeigt aktuelle Termine, Meldungen und Sperren.

Grenzen: Die Erkennung arbeitet mit Schlüsselwörtern auf englischen Schlagzeilen. Für Schlagzeilen gibt es keine
frei verfügbare Historie, deshalb ist dieser Teil (anders als der Makrofilter) nicht backtestbar – er sperrt
nur neue Einstiege und ist bewusst vorsichtig eingestellt.

---

## Backtest-Ergebnisse

Echte Binance-USDT-M-Futures-Daten, Startkapital 1.000 USDT, Taker-Gebühr 0,05 % + Slippage 0,03 % pro Seite,
historisches Funding, Standardkonfiguration, Zinseszins.

**Vorgehen gegen Overfitting:** Strategie und Parameter wurden nur mit 9 Coins (BTC, ETH, SOL, BNB, XRP, DOGE,
ADA, AVAX, LINK) und nur auf 2021–2023 entwickelt. 2024–2026 und 9 weitere Coins (LTC, DOT, TRX, BCH, ATOM,
NEAR, UNI, ETC, FIL) wurden erst ganz am Ende einmalig getestet.

| Test | Rendite | Max. DD | Profit-Faktor | Trades | Jahre |
|---|---|---|---|---|---|
| Alte Strategie (15m-Konfluenz), BTC/ETH/SOL | −13 bis −14 % **pro Jahr** | 15 % (Kill-Switch) | 0,44–0,77 | – | jedes Jahr Verlust |
| **Neu, 9 Majors, 2022–09/2026** | **+295 %** | **19,3 %** | **1,72** | 651 | 2022 +3 % · 2023 +106 % · 2024 +62 % · 2025 +10 % · 2026 +4 % |
| Neu, 9 Majors, nur 2024–09/2026 (Out-of-Sample-Zeitraum) | +87 % | 19,3 % | 1,54 | 391 | 2024 +64 % · 2025 +10 % · 2026 +4 % |
| Neu, 9 *nie benutzte* Coins, 2024–09/2026 | +6 % | 27,0 % | 1,06 | 377 | 2024 +3 % · 2025 −16 % · 2026 +22 % |
| Neu, 9 Majors, Slippage 0,1 % (3×) | +547 % (2021–26) | 20,4 % | 1,72 | 748 | robust gegen schlechtere Ausführung |

**Ehrliche Einordnung:**
- Die Kante ist bei liquiden Majors deutlich stärker als bei kleineren Altcoins → Standard-Symbolliste = Majors.
- 2025/2026 waren schwache Trendfolge-Jahre (+4 bis +10 %). Es wird auch Verlustjahre geben.
- Drawdowns um 20 % kamen vor und werden wieder vorkommen. Wer das nicht aushält, halbiert `risk_per_trade_pct`.
- Die getesteten Coins existieren alle noch heute (Survivorship-Bias) – die Realität ist etwas schlechter.
- Ein Backtest ist keine Garantie. Deshalb: Paper → Demo → kleines Echtgeld.

Selbst nachprüfen: `python -m bot backtest --start 2022-01-01 --end 2026-10-01`

---

## Projektstruktur

```
bot/
  analysis.py    Indikatoren → Features, Regime, Multi-Timeframe, Scores, Markt-Snapshot
  indicators.py  EMA, RSI, ATR, MACD, Bollinger, ADX, Supertrend, Stoch-RSI, VWAP, Pivots
  strategy.py    Einstiegs-/Ausstiegssignale, Break-Even & Trailing-Stop
  risk.py        Positionsgröße, Hebel, Liquidations-Check, Tages-/Drawdown-Limits, Cooldown
  broker.py      PaperBroker (Simulation) & LiveBroker (ccxt-Orders mit SL/TP auf der Börse)
  exchange.py    ccxt-Anbindung, Marktdaten, Limits/Präzision, Retry
  engine.py      Hauptschleife, Wiederherstellung nach Neustart, Benachrichtigungen
  backtest.py    Backtester, Kennzahlen, Optimierung
  datasets.py    Download historischer Kerzen + Funding aus den Binance-Archiven
  news.py        Wirtschaftskalender, Schlagzeilen, Binance-Delistings, Fear & Greed
  storage.py     SQLite (Trades, Equity, Zustand)
  notifier.py    Telegram + Logging
config/          Beispielkonfiguration
deploy/          systemd-Unit + VPS-Installationsskript
tests/           pytest-Suite (Indikatoren, Lookahead, Breakout, Risiko, Funding, News, Broker, Live-Order-Ablauf, Engine)
```

Tests: `pip install -r requirements-dev.txt && python -m pytest`

## Telegram einrichten

1. In Telegram `@BotFather` → `/newbot` → Token kopieren
2. Dem neuen Bot eine Nachricht schreiben, dann `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen → `chat.id`
3. In `.env` `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` setzen, in `config.yaml` `telegram_enabled: true`

Du bekommst Nachrichten zu Start/Stopp, jedem Ein- und Ausstieg, Kill-Switch, Fehlerserien und einen Tagesbericht.
