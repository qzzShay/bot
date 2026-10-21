"""Benachrichtigungen (Telegram) + Logging-Setup."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import requests

from .config import NotificationConfig

log = logging.getLogger(__name__)


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("urllib3", "ccxt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Notifier:
    def __init__(self, cfg: NotificationConfig, prefix: str = ""):
        self.enabled = cfg.telegram_enabled and bool(cfg.telegram_token and cfg.telegram_chat_id)
        self.cfg = cfg
        self.prefix = prefix
        if cfg.telegram_enabled and not self.enabled:
            log.warning("Telegram aktiviert, aber TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID fehlen")

    def send(self, text: str) -> None:
        log.info("NOTIFY: %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                json={"chat_id": self.cfg.telegram_chat_id, "text": f"{self.prefix}{text}"},
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning("Telegram-Nachricht fehlgeschlagen: %s", e)
