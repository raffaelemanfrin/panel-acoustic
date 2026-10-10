"""Registro allarmi in SQLite.

Un solo file .db, accessibile in lettura dalla dashboard mentre il demone scrive
(modalita' WAL). Il file di default sta accanto a questo modulo, cosi' non
dipende dalla cartella da cui parte il processo; *.db e' gia' nel .gitignore.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_DB = Path(__file__).resolve().parent / "alarms.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,      -- epoch UTC in secondi
    cls         TEXT NOT NULL,      -- classe riconosciuta (grandine, colpo, ...)
    source      TEXT NOT NULL,      -- 'rules' | 'nn' | 'logic'
    confidence  REAL,               -- 0..1, se la sorgente lo fornisce
    peak        INTEGER,            -- picco del piezo nel blocco
    latency_ms  REAL,               -- dal trigger alla decisione
    detail      TEXT                -- regola scattata o altro testo libero
);
CREATE INDEX IF NOT EXISTS idx_alarms_ts ON alarms(ts);
"""


class AlarmStore:
    def __init__(self, path=DEFAULT_DB):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def add(self, cls: str, source: str, confidence: Optional[float] = None,
            peak: Optional[int] = None, latency_ms: Optional[float] = None,
            detail: Optional[str] = None, ts: Optional[float] = None) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO alarms (ts, cls, source, confidence, peak, latency_ms, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time() if ts is None else ts, cls, source, confidence, peak, latency_ms, detail),
            )
            return cur.lastrowid

    def last(self, n: int = 20) -> List[dict]:
        """Ultimi n allarmi, dal piu' recente."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM alarms ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def since(self, seconds: float) -> List[dict]:
        """Allarmi degli ultimi `seconds` secondi, dal piu' vecchio (utile per il grafico)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM alarms WHERE ts >= ? ORDER BY ts", (time.time() - seconds,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count_by_class(self, since_seconds: Optional[float] = None) -> Dict[str, int]:
        sql, args = "SELECT cls, COUNT(*) AS n FROM alarms", ()
        if since_seconds is not None:
            sql += " WHERE ts >= ?"
            args = (time.time() - since_seconds,)
        with self._lock:
            rows = self._db.execute(sql + " GROUP BY cls", args).fetchall()
        return {r["cls"]: r["n"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._db.close()