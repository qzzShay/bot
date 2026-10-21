"""Konfiguration laden (YAML + .env) und validieren."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

LIVE_CONFIRMATION = "I_UNDERSTAND_THE_RISK"
VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


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
    entry: str = "15m"
    trend: str = "4h"
    history_bars: int = 400


@dataclass
class StrategyConfig:
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
    take_profit_r: float = 2.5
    breakeven_at_r: float = 1.0
    trailing_start_r: float = 1.5
    trailing_atr_mult: float = 2.5
    exit_on_opposite_signal: bool = True
    max_adverse_funding: float = 0.0005


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0
    max_leverage: int = 5
    max_position_notional_x: float = 3.0
    max_open_positions: int = 2
    max_daily_loss_pct: float = 4.0
    max_drawdown_pct: float = 15.0
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
class NotificationConfig:
    telegram_enabled: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class Config:
    mode: str = "paper"
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT:USDT"])
    timeframes: TimeframeConfig = field(default_factory=TimeframeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
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
        if self.timeframes.history_bars < self.strategy.ema_trend + 50:
            raise ConfigError("timeframes.history_bars muss >= ema_trend + 50 sein")
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
    )
    cfg.exchange.api_key = os.getenv("EXCHANGE_API_KEY", "")
    cfg.exchange.api_secret = os.getenv("EXCHANGE_API_SECRET", "")
    cfg.exchange.api_password = os.getenv("EXCHANGE_API_PASSWORD", "")
    cfg.notifications.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg.notifications.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    cfg.live_confirmed = os.getenv("CONFIRM_LIVE_TRADING", "") == LIVE_CONFIRMATION
    cfg.validate()
    return cfg
