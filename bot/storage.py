"""SQLite-Persistenz: Trades, offene Positionen, Equity-Verlauf und Bot-Zustand."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    initial_stop REAL NOT NULL,
    take_profit REAL,
    leverage INTEGER NOT NULL,
    best_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    reason_open TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    exit REAL,
    closed_at TEXT,
    pnl REAL,
    fees REAL DEFAULT 0,
    reason_close TEXT,
    sl_order_id TEXT,
    tp_order_id TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        # v1 → v2: take_profit darf NULL sein (Breakout-Strategie arbeitet ohne festes Kursziel)
        cols = {r["name"]: r for r in self.conn.execute("PRAGMA table_info(trades)")}
        if cols.get("take_profit") is not None and cols["take_profit"]["notnull"]:
            names = ", ".join(cols)
            self.conn.executescript(f"""
                ALTER TABLE trades RENAME TO trades_v1;
                {SCHEMA.split(';')[0]};
                INSERT INTO trades ({names}) SELECT {names} FROM trades_v1;
                DROP TABLE trades_v1;
            """)

    # ---------- Trades ----------
    def insert_trade(self, **t: Any) -> int:
        cols = ", ".join(t)
        qs = ", ".join("?" for _ in t)
        cur = self.conn.execute(f"INSERT INTO trades ({cols}) VALUES ({qs})", tuple(t.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_trade(self, trade_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE trades SET {sets} WHERE id = ?", (*fields.values(), trade_id))
        self.conn.commit()

    def open_trades(self, mode: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades WHERE status='open' AND mode=?", (mode,)).fetchall()
        return [dict(r) for r in rows]

    def closed_trades(self, mode: str | None = None, limit: int = 1000) -> list[dict]:
        q = "SELECT * FROM trades WHERE status='closed'"
        args: tuple = ()
        if mode:
            q += " AND mode=?"
            args = (mode,)
        q += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, (*args, limit)).fetchall()]

    def count_opened_since(self, mode: str, symbol: str, since_iso: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM trades WHERE mode=? AND symbol=? AND opened_at>=?",
                                (mode, symbol, since_iso)).fetchone()
        return int(row["n"])

    # ---------- Equity ----------
    def log_equity(self, ts: str, mode: str, equity: float) -> None:
        self.conn.execute("INSERT INTO equity (ts, mode, equity) VALUES (?, ?, ?)", (ts, mode, equity))
        self.conn.commit()

    # ---------- Key/Value-Zustand ----------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()
