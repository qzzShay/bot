# CryptoBot – automatischer Krypto-Futures-Trading-Bot

Python-Bot für USDT-Perpetual-Futures (Binance, Bybit, OKX, Bitget via [ccxt](https://github.com/ccxt/ccxt))
mit Hebel, Multi-Timeframe-Marktanalyse, striktem Risikomanagement, Backtester und VPS-Deployment.

> ⚠️ **Risikohinweis:** Hebel-Trading kann zum Totalverlust des eingesetzten Kapitals führen. Keine
> Strategie ist garantiert profitabel – auch diese nicht. Der Bot ist ein Werkzeug, keine Gelddruckmaschine.
> Setze nur Geld ein, dessen Verlust du verkraften kannst, und gehe die Stufen unten der Reihe nach durch.

---

## Funktionen

| Bereich | Was drin ist |
|---|---|
| **Marktanalyse** | EMA (21/55/200), RSI, MACD, Bollinger Bands, ATR, ADX/DI, Supertrend, Stoch-RSI, VWAP, Volumen, Support/Resistance (Pivots), Funding-Rate |
| **Marktregime** | Automatische Erkennung: *Trend* (ADX hoch) → Trendfolge, *Seitwärts* → Mean-Reversion, *Extrem-Volatil* → Pause |
| **Multi-Timeframe** | Einstieg auf 15m, Trendfilter auf 4h – ohne Lookahead (nur abgeschlossene Kerzen) |
| **Signale** | Gewichteter Konfluenz-Score 0–1 für Long und Short, Einstieg nur bei frischem Durchbruch der Schwelle |
| **Hebel** | Automatisch: so wenig wie nötig, max. `max_leverage`; Liquidationspreis muss *hinter* dem Stop liegen |
| **Positionsgröße** | Risikobasiert: fester %-Satz des Kontos pro Trade (Abstand Entry → Stop) |
| **Exits** | Stop-Loss (ATR), Take-Profit (R-Vielfaches), Break-Even, ATR-Trailing-Stop, Ausstieg bei Gegensignal |
| **Absicherung** | SL & TP liegen als `reduceOnly`-Orders **auf der Börse** – Position bleibt geschützt, auch wenn der Bot/VPS abstürzt. Scheitert der SL, wird sofort geschlossen |
| **Risikolimits** | Tagesverlust-Limit, Max-Drawdown-Kill-Switch, max. offene Positionen, Cooldown nach Verlustserie, Funding-Filter |
| **Modi** | `paper` (echte Live-Kurse, simulierte Orders) · `live` + `demo/testnet` (Spielgeld auf der Börse) · `live` + `none` (Echtgeld, doppelt gesichert) |
| **Backtest** | Gleicher Code wie live, mit Gebühren, Slippage, Multi-Symbol-Portfolio, Kennzahlen, CSV-Export |
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
python -m bot backtest --start 2024-01-01   # Strategie auf echten historischen Daten testen
python -m bot run                           # Paper-Trading mit Live-Kursen
```

## Der Weg zu echtem Geld – bitte in dieser Reihenfolge

1. **Backtest** auf echten Daten (`python -m bot backtest --start 2023-01-01`). Mehrere Zeiträume testen
   (Bullen-, Bären- und Seitwärtsmarkt). Wenn der Backtest nicht überzeugt, nicht weitermachen.
2. **Optimieren** (optional): `python -m bot optimize --start 2023-01-01`. Nur Parameter übernehmen, die auch im
   *Test*-Zeitraum gut sind.
3. **Paper-Trading** (`mode: paper`) mindestens 2–4 Wochen auf dem VPS laufen lassen.
4. **Demo/Testnet** (`mode: live`, `sandbox: demo`) mit Demo-API-Keys der Börse – prüft den kompletten
   Order-Ablauf (Hebel, SL/TP-Orders, Trailing) ohne echtes Geld.
   - Binance: Demo-Trading auf binance.com aktivieren → API-Key im Demo-Bereich erstellen
   - Bybit: `sandbox: testnet` mit Key von testnet.bybit.com, oder `demo` mit Demo-Trading-Key
5. **Echtgeld, klein anfangen** (`mode: live`, `sandbox: none`):
   - in `.env`: `CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISK` (ohne diesen Wert verweigert der Bot den Start)
   - Unterkonto mit kleinem Betrag, `risk_per_trade_pct: 0.5`, `max_leverage: 3`
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
(Konfiguration, `.env` und Datenbank bleiben erhalten.)

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
| `python -m bot backtest --start 2024-01-01 [--end ...] [SYMBOL ...]` | Backtest mit Börsendaten (Cache in `data/cache`) |
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

1. **Trendfilter (4h):** Bullish, wenn Kurs > EMA200, EMA55 > EMA200 und Supertrend aufwärts (bearish analog).
2. **Regime (15m):** ADX ≥ 22 → Trend; sonst Seitwärts; ATR im obersten 2%-Perzentil → keine Trades.
3. **Score Trendfolge:** EMA-Ausrichtung (20%), Supertrend (15%), MACD-Histogramm (15%), RSI-Zone (15%),
   Volumen (10%), Nähe zum EMA – kein Hinterherlaufen (10%), 4h-Trend (15%).
4. **Score Mean-Reversion:** Bollinger %B (35%), RSI überverkauft/-kauft (30%), Stoch-RSI-Kreuzung (20%),
   4h-Trend nicht dagegen (15%). TP max. 1.5 R.
5. **Einstieg:** Score kreuzt `entry_threshold` (Standard 0.55) und Funding-Rate ist nicht stark dagegen.
6. **Stop:** 2 × ATR · **TP:** 2.5 R · **Break-Even** ab 1 R · **Trailing** ab 1.5 R mit 2.5 × ATR.

**Positionsgröße – Beispiel:** Konto 1.000 USDT, Risiko 1 % = 10 USDT. BTC-Entry 60.000, Stop 59.400
(600 Abstand) → 0,0167 BTC ≈ 1.000 USDT Notional → **Hebel 2x**. Stop getroffen = −10 USDT (+ Gebühren).
Der Hebel bestimmt also *nicht* das Risiko – das tut der Stop-Abstand. Hebel spart nur Margin.

Alle Parameter stehen kommentiert in `config/config.example.yaml`.

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
  storage.py     SQLite (Trades, Equity, Zustand)
  notifier.py    Telegram + Logging
config/          Beispielkonfiguration
deploy/          systemd-Unit + VPS-Installationsskript
tests/           pytest-Suite (Indikatoren, Lookahead, Risiko, Broker, Live-Order-Ablauf, Engine)
```

Tests: `pip install -r requirements-dev.txt && python -m pytest`

## Telegram einrichten

1. In Telegram `@BotFather` → `/newbot` → Token kopieren
2. Dem neuen Bot eine Nachricht schreiben, dann `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen → `chat.id`
3. In `.env` `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` setzen, in `config.yaml` `telegram_enabled: true`

Du bekommst Nachrichten zu Start/Stopp, jedem Ein- und Ausstieg, Kill-Switch, Fehlerserien und einen Tagesbericht.
