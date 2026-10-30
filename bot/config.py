"""Konfiguration laden (YAML + .env) und validieren."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .news import DEFAULT_FEEDS

log = logging.getLogger(__name__)

LIVE_CONFIRMATION = "I_UNDERSTAND_THE_RISK"
DEFAULT_SYMBOLS = [f"{c}/USDT:USDT" for c in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK")]
VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


def tf_hours(tf: str) -> float:
    unit = {"m": 1 / 60, "h": 1, "d": 24}[tf[-1]]
    return int(tf[:-1]) * unit


class ConfigError(ValueError):
    pass


@dataclass
class ExchangeConfig:
    name: str = "binanceusdm"
    sandbox: str = "demo"
    margin_mode: str = "isolated"
    quote_currency: str = "USDT"
    taker_fee: float = 0.0005
    slippage: float = 0.0003
    api_key: str = ""
    api_secret: str = ""
    api_password: str = ""


@dataclass
class TimeframeConfig:
    entry: str = "4h"
    trend: str = "1d"
    history_bars: int = 400


@dataclass
class StrategyConfig:
    # breakout   = Donchian-Trendfolge auf 4h (Standard, auf echten Daten 2021–2026 getestet)
    # confluence = alte Indikator-Konfluenz (verlor im Backtest in jedem Jahr – nur zu Vergleichszwecken)
    type: str = "breakout"
    breakout_period: int = 55
    btc_filter: bool = True
    btc_filter_ema: int = 200
    max_funding_24h: float = 0.0009
    short_risk_factor: float = 0.5
    entry_threshold: float = 0.55
    require_trend_alignment: bool = True
    allow_mean_reversion: bool = True
    adx_trend_min: float = 22
    ema_fast: int = 21
    ema_slow: int = 55
    ema_trend: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    take_profit_r: float | None = None
    breakeven_at_r: float | None = None
    trailing_start_r: float = 0.0
    trailing_atr_mult: float = 6.0
    exit_on_opposite_signal: bool = False
    # Day-Trading: alle Positionen am selben UTC-Tag schließen (siehe config/config.daytrade.yaml).
    # Im Backtest 2021–2026 nach Kosten ohne nachweisbare Gewinnkante – nur für Paper/Demo gedacht.
    day_trading: bool = False
    last_entry_hour_utc: int = 21     # keine neuen Einstiege bei Kerzen, die später schließen
    flat_hour_utc: int = 0            # 0 = um Mitternacht UTC alles schließen, sonst ab dieser Stunde
    max_trades_per_day: int = 1       # pro Coin
    # Echtgeld mit Day-Trading ist gesperrt, solange das nicht ausdrücklich auf true steht
    day_trading_allow_real_money: bool = False
    max_adverse_funding: float = 0.0005


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_leverage: int = 5
    max_position_notional_x: float = 3.0
    max_open_positions: int = 4
    # Summe des Risikos aller offenen Positionen (Abstand zum Stop) in % des Kontos
    max_portfolio_risk_pct: float = 3.0
    max_daily_loss_pct: float = 4.0
    max_drawdown_pct: float = 30.0
    max_consecutive_losses: int = 4
    cooldown_minutes: int = 240
    liquidation_buffer_pct: float = 1.0
    paper_start_equity: float = 1000.0


@dataclass
class EngineConfig:
    poll_seconds: int = 20
    db_path: str = "data/cryptobot.sqlite"
    log_level: str = "INFO"
    log_file: str = "data/cryptobot.log"


@dataclass
class NewsConfig:
    enabled: bool = True
    poll_minutes: int = 5
    # Keine neuen Trades rund um FOMC-Zinsentscheidungen und wichtige US-Makrodaten (CPI, NFP, PCE …)
    macro_filter: bool = True
    macro_block_before_hours: float = 4
    macro_block_after_hours: float = 4
    # Keine neuen Trades auf einem Coin nach kritischen Schlagzeilen (Hack, Delisting, Klage, Depeg …)
    headline_filter: bool = True
    headline_block_hours: float = 12
    # Sperre erst, wenn so viele unabhängige Quellen berichten (weniger Fehlalarme)
    headline_min_sources: int = 2
    # Futures-Delisting eines gehandelten Coins → offene Position schließen
    close_on_delisting: bool = True
    feeds: list[str] = field(default_factory=lambda: list(DEFAULT_FEEDS))
    # Fear & Greed als Filter (im Backtest NICHT stabil besser – Standard aus, nur Anzeige)
    fear_greed_filter: bool = False
    greed_max: int = 80
    fear_min: int = 20


@dataclass
class NotificationConfig:
    telegram_enabled: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class Config:
    mode: str = "paper"
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    timeframes: TimeframeConfig = field(default_factory=TimeframeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    live_confirmed: bool = False

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_real_money(self) -> bool:
        return self.is_live and self.exchange.sandbox == "none"

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ConfigError("mode muss 'paper' oder 'live' sein")
        if self.exchange.sandbox not in ("none", "testnet", "demo"):
            raise ConfigError("exchange.sandbox muss none, testnet oder demo sein")
        if self.exchange.margin_mode not in ("isolated", "cross"):
            raise ConfigError("exchange.margin_mode muss isolated oder cross sein")
        for tf in (self.timeframes.entry, self.timeframes.trend):
            if tf not in VALID_TIMEFRAMES:
                raise ConfigError(f"Ungültige Zeiteinheit: {tf}")
        if not self.symbols:
            raise ConfigError("Mindestens ein Symbol angeben")
        st = self.strategy
        if st.type not in ("breakout", "confluence"):
            raise ConfigError("strategy.type muss breakout oder confluence sein")
        need = max(st.ema_trend, st.btc_filter_ema, st.breakout_period) + 50
        if self.timeframes.history_bars < need:
            raise ConfigError(f"timeframes.history_bars muss >= {need} sein")
        if st.day_trading:
            if tf_hours(self.timeframes.entry) > 4:
                raise ConfigError("Day-Trading braucht timeframes.entry von höchstens 4h")
            if not (0 <= st.flat_hour_utc <= 23 and 0 <= st.last_entry_hour_utc <= 23):
                raise ConfigError("flat_hour_utc und last_entry_hour_utc müssen zwischen 0 und 23 liegen")
            if st.flat_hour_utc and st.last_entry_hour_utc >= st.flat_hour_utc:
                raise ConfigError("last_entry_hour_utc muss vor flat_hour_utc liegen")
            if self.is_real_money:
                if not st.day_trading_allow_real_money:
                    raise ConfigError(
                        "Day-Trading ist für Echtgeld gesperrt: im Backtest 2022–2026 −44 % (Profit-Faktor 0,93). "
                        "Nutze Paper/Demo – oder setze strategy.day_trading_allow_real_money: true auf eigenes Risiko.")
                log.warning("Day-Trading-Modus mit ECHTGELD: im Backtest ohne Gewinnkante!")
        if not 0 < st.short_risk_factor <= 1:
            raise ConfigError("short_risk_factor muss zwischen 0 und 1 liegen")
        r = self.risk
        if not 0 < r.risk_per_trade_pct <= 5:
            raise ConfigError("risk_per_trade_pct muss zwischen 0 und 5 liegen")
        if not 1 <= r.max_leverage <= 50:
            raise ConfigError("max_leverage muss zwischen 1 und 50 liegen")
        if r.max_daily_loss_pct <= 0 or r.max_drawdown_pct <= 0:
            raise ConfigError("Verlustlimits müssen > 0 sein")
        if not 0 < self.strategy.entry_threshold <= 1:
            raise ConfigError("entry_threshold muss zwischen 0 und 1 liegen")
        if self.is_live:
            if not (self.exchange.api_key and self.exchange.api_secret):
                raise ConfigError("Live-Modus benötigt EXCHANGE_API_KEY und EXCHANGE_API_SECRET in .env")
            if self.is_real_money and not self.live_confirmed:
                raise ConfigError(
                    "Echtgeld-Trading ist gesperrt. Setze CONFIRM_LIVE_TRADING="
                    f"{LIVE_CONFIRMATION} in .env, wenn du das wirklich willst."
                )


def _build(cls, data: dict[str, Any] | None):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unbekannte Optionen in '{cls.__name__}': {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | os.PathLike = "config/config.yaml", env_file: str | None = ".env") -> Config:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file)
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {p} (kopiere config/config.example.yaml)")
    raw = yaml.safe_load(p.read_text()) or {}

    cfg = Config(
        mode=raw.get("mode", "paper"),
        exchange=_build(ExchangeConfig, raw.get("exchange")),
        symbols=list(raw.get("symbols") or []),
        timeframes=_build(TimeframeConfig, raw.get("timeframes")),
        strategy=_build(StrategyConfig, raw.get("strategy")),
        risk=_build(RiskConfig, raw.get("risk")),
        engine=_build(EngineConfig, raw.get("engine")),
        notifications=_build(NotificationConfig, raw.get("notifications")),
        news=_build(NewsConfig, raw.get("news")),
    )
    cfg.exchange.api_key = os.getenv("EXCHANGE_API_KEY", "")
    cfg.exchange.api_secret = os.getenv("EXCHANGE_API_SECRET", "")
    cfg.exchange.api_password = os.getenv("EXCHANGE_API_PASSWORD", "")
    cfg.notifications.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg.notifications.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    cfg.live_confirmed = os.getenv("CONFIRM_LIVE_TRADING", "") == LIVE_CONFIRMATION
    cfg.validate()
    return cfg
