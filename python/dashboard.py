#!/usr/bin/env python3
"""Dashboard in tempo reale: legge il registro SQLite del demone e lo mostra nel browser.

    python dashboard.py                      # http://<ip della UNO Q>:8080
    python dashboard.py --db prova.db --port 8000

Solo libreria standard e nessuna risorsa da internet: funziona anche offline
(la demo e' "senza internet"). Il demone scrive, la dashboard legge in sola
lettura (SQLite in modalita' WAL), quindi i due processi sono indipendenti.

In App Lab ricordarsi di esporre la porta in app.yaml (ports: [8080]).

Endpoint:
    /            pagina (dashboard.html)
    /api/state   JSON con eventi della finestra, ultimi allarmi e conteggi
                 ?window=300 (secondi, max 3600)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from storage import DEFAULT_DB

PAGE = Path(__file__).with_name("dashboard.html")


def read_state(db_path: str, window_s: float = 300.0) -> dict:
    now = time.time()
    state = {"now": now, "window": window_s, "db": True, "events": [], "alarms": [],
             "alarms_hour": {}, "warnings_hour": 0, "events_window": 0}
    if not Path(db_path).exists():
        state["db"] = False
        return state
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        state["db"] = False
        return state
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" in tables:
            rows = db.execute(
                "SELECT ts, cls, peak, nn_alarm, light_fast, light_slow, outcome, latency_ms "
                "FROM events WHERE ts >= ? ORDER BY ts", (now - window_s,)).fetchall()
            state["events"] = [dict(r) for r in rows]
            state["events_window"] = len(rows)
            state["warnings_hour"] = db.execute(
                "SELECT COUNT(*) FROM events WHERE outcome = 'avviso' AND ts >= ?", (now - 3600,)).fetchone()[0]
        if "alarms" in tables:
            state["alarms"] = [dict(r) for r in db.execute(
                "SELECT ts, cls, source, peak, latency_ms, detail FROM alarms ORDER BY id DESC LIMIT 12")]
            state["alarms_hour"] = {r["cls"]: r["n"] for r in db.execute(
                "SELECT cls, COUNT(*) AS n FROM alarms WHERE ts >= ? GROUP BY cls", (now - 3600,))}
    except sqlite3.Error as e:
        state["error"] = str(e)
    finally:
        db.close()
    return state


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, db_path: str, **kw):
        self.db_path = db_path
        super().__init__(*args, **kw)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            q = parse_qs(url.query)
            try:
                window = min(max(float(q.get("window", ["300"])[0]), 10.0), 3600.0)
            except ValueError:
                window = 300.0
            body = json.dumps(read_state(self.db_path, window)).encode()
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # niente log per ogni richiesta (polling ogni secondo)
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, db_path=args.db))
    print(f"Dashboard su http://{args.host}:{args.port}  (db {args.db}, Ctrl+C per fermare)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
