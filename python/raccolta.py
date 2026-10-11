"""Modalita' raccolta per App Lab: registra i blocchi in CSV invece di far girare il demone.

Il canale "pa_lines" del Bridge puo' averlo un solo processo alla volta, quindi
in App Lab si sceglie all'avvio: se esiste python/raccolta.json, main.py
avvia la raccolta (questo modulo) e non il demone.

Si pilota riscrivendo raccolta.json mentre l'app gira (in SSH sulla scheda):
    echo '{"label": "grandine", "n": 30}' > raccolta.json
    echo '{"label": "calma", "n": 30, "force": true}' > raccolta.json
    echo '{"label": "colpo", "n": 15, "category": "testing"}' > raccolta.json
Ogni volta che il file cambia parte una nuova sessione; l'avanzamento si
legge nella console Python di App Lab e in raccolta_stato.txt.

Campi: label (obbligatorio), n (default 30), category ("training"/"testing"),
force (true = blocchi senza trigger, uno al secondo: per calma e vento; se la
MCU non risponde alla richiesta, mettere il ponticello D2-GND per la durata della raccolta).
I CSV finiscono in python/data/<category>/<label>.NN.csv. Per portarli sul PC:
    scp -r arduino@<ip>:/home/arduino/ArduinoApps/panel-acoustic/python/data .
Per tornare al demone: rm raccolta.json e di nuovo Run.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from capture import Session
from protocol import BlockParser

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "raccolta.json"
STATUS = HERE / "raccolta_stato.txt"
DATA = HERE / "data"

log = logging.getLogger("raccolta")


def read_config(path: Path) -> Optional[dict]:
    try:
        cfg = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.error("raccolta.json non valido: %s", e)
        return None
    if not isinstance(cfg, dict) or not cfg.get("label"):
        log.error("raccolta.json: serve almeno {\"label\": \"...\"}")
        return None
    return cfg


class Collector:
    """Legge le righe del protocollo e le passa alla sessione indicata da raccolta.json."""

    def __init__(self, config: Path = CONFIG, out: Path = DATA, status: Path = STATUS,
                 request_force: Callable[[int], None] = lambda n: None, adc_bits: int = 12):
        self.config = config
        self.out = out
        self.status = status
        self.request_force = request_force
        self.adc_bits = adc_bits
        self.session: Optional[Session] = None
        self._mtime = None
        self._last_check = 0.0

    def _write_status(self, text: str) -> None:
        log.info(text)
        try:
            self.status.write_text(time.strftime("%H:%M:%S ") + text + "\n")
        except OSError:
            pass

    def check_config(self) -> None:
        try:
            mtime = self.config.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        cfg = read_config(self.config)
        if cfg is None:
            return
        n = int(cfg.get("n", 30))
        force = bool(cfg.get("force", False))
        try:
            self.session = Session(str(cfg["label"]), cfg.get("category", "training"), self.out, n,
                                   forced_only=force, parser=BlockParser(adc_bits=self.adc_bits),
                                   log=self._write_status)
        except ValueError as e:
            self._write_status(f"configurazione rifiutata: {e}")
            self.session = None
            return
        what = "blocchi forzati" if force else "eventi da trigger"
        self._write_status(f"nuova raccolta: {self.session.label} ({self.session.folder.name}), "
                           f"{n} {what}")
        if force:
            self.request_force(n)

    def feed(self, line: str) -> None:
        now = time.monotonic()
        if now - self._last_check >= 1.0:
            self._last_check = now
            self.check_config()
        s = self.session
        if s is None or s.done:
            return
        if s.feed(line):
            self._write_status(f"raccolta {s.label} completata: {s.saved} file in {s.folder}. "
                               "Riscrivi raccolta.json per la prossima.")

    def run(self, lines: Iterator[str]) -> None:
        self._write_status("modalita' raccolta: in attesa di raccolta.json")
        for line in lines:
            self.feed(line)


def run_on_bridge() -> None:
    """Usata da main.py: un solo collegamento al router per tutte le sessioni."""
    from source import _open_bridge, bridge_lines, request_force

    bridge, close = _open_bridge()
    try:
        Collector(request_force=lambda n: request_force(bridge, n)).run(bridge_lines(bridge=bridge))
    finally:
        close()
