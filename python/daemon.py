#!/usr/bin/env python3
"""Demone di rilevamento: righe MCU -> parser -> classificatore -> registro allarmi.

Esempi:
    python daemon.py --port /dev/ttyMCU                       # con la scheda
    python fakemcu.py --n 30 --interval 0.5 --corrupt-rate 0.2 | python daemon.py --input -
    python daemon.py --input registrazione.txt --db prova.db --print-all

Cosa fa:
  - ogni blocco valido viene classificato (oggi con le regole di detector.py);
  - se la classe e' di allarme, la scrive in SQLite (storage.py), salvo cooldown;
  - i blocchi scartati dal parser vengono contati e loggati, non fermano nulla;
  - con --port, se la seriale cade riprova a riaprirla (attesa crescente);
  - SIGTERM / Ctrl+C: chiude il registro e stampa le statistiche.

La logica e' tutta in Pipeline.handle_line(), senza I/O: si testa riga per riga
con un orologio finto (vedi testdaemon.py).

Latenza registrata = campioni post-trigger (200 ms, fissi) + tempo dalla riga
START alla decisione (trasmissione + parsing + classificazione). Con --input da
file la seconda parte misura solo la velocita' di lettura, non e' significativa.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from detector import ALARM_CLASSES, Detection, Thresholds, classify
from protocol import (SAMPLE_PERIOD_MS, SAMPLES_PER_BLOCK, START, TRIGGER_INDEX,
                      Block, BlockParser, Rejected)
from source import open_lines
from storage import DEFAULT_DB, AlarmStore

log = logging.getLogger("daemon")

POST_TRIGGER_MS = (SAMPLES_PER_BLOCK - TRIGGER_INDEX) * SAMPLE_PERIOD_MS

# Un classificatore prende un Block e restituisce una Detection.
# Oggi: le regole. Domani: la rete Edge Impulse, con la stessa firma.
Classifier = Callable[[Block], Detection]


@dataclass
class Stats:
    blocks_ok: int = 0
    blocks_rejected: int = 0
    by_class: Counter = field(default_factory=Counter)
    reject_reasons: Counter = field(default_factory=Counter)
    alarms_stored: int = 0
    alarms_suppressed: int = 0

    def summary(self) -> str:
        cls = ", ".join(f"{k}={v}" for k, v in sorted(self.by_class.items())) or "-"
        return (f"blocchi validi {self.blocks_ok}, scartati {self.blocks_rejected} | {cls} | "
                f"allarmi registrati {self.alarms_stored}, soppressi (cooldown) {self.alarms_suppressed}")


class Pipeline:
    """Stato del demone, senza I/O di rete o seriale.

    handle_line() va chiamata per ogni riga (anche vuota: serve al timeout).
    Restituisce la Detection quando un blocco e' stato classificato, altrimenti None.
    """

    def __init__(
        self,
        store: Optional[AlarmStore],
        parser: Optional[BlockParser] = None,
        classifier: Classifier = classify,
        source_name: str = "rules",
        cooldown_s: float = 10.0,
        alarm_classes=ALARM_CLASSES,
        on_alarm: Optional[Callable[[Detection, Block, dict], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.parser = parser or BlockParser(clock=clock)
        self.classifier = classifier
        self.source_name = source_name
        self.cooldown_s = cooldown_s
        self.alarm_classes = set(alarm_classes)
        self.on_alarm = on_alarm
        self.clock = clock
        self.stats = Stats()
        self._t_start: Optional[float] = None
        self._last_alarm: Dict[str, float] = {}

    def handle_line(self, line: str) -> Optional[Detection]:
        if line.strip() == START:
            self._t_start = self.clock()
        result = self.parser.feed(line)

        if isinstance(result, Rejected):
            self.stats.blocks_rejected += 1
            self.stats.reject_reasons[result.reason] += 1
            log.warning("blocco scartato: %s", result.reason)
            return None
        if not isinstance(result, Block):
            return None

        det = self.classifier(result)
        now = self.clock()
        transit_ms = (now - self._t_start) * 1000 if self._t_start is not None else 0.0
        latency_ms = POST_TRIGGER_MS + transit_ms
        self._t_start = None

        self.stats.blocks_ok += 1
        self.stats.by_class[det.cls] += 1
        peak = max(result.piezo)
        log.debug("blocco: %-8s (%s) picco=%d latenza=%.0f ms", det.cls, det.rule, peak, latency_ms)

        if det.cls in self.alarm_classes:
            self._alarm(det, result, peak, latency_ms, now)
        return det

    def _alarm(self, det: Detection, block: Block, peak: int, latency_ms: float, now: float) -> None:
        last = self._last_alarm.get(det.cls)
        if last is not None and now - last < self.cooldown_s:
            self.stats.alarms_suppressed += 1
            log.info("allarme %s soppresso (cooldown %.0f s)", det.cls, self.cooldown_s)
            return
        self._last_alarm[det.cls] = now

        confidence = getattr(det, "confidence", None)
        if self.store is not None:
            self.store.add(det.cls, self.source_name, confidence=confidence, peak=peak,
                           latency_ms=round(latency_ms, 1), detail=det.rule)
        self.stats.alarms_stored += 1
        log.warning("ALLARME %s (%s) latenza=%.0f ms", det.cls.upper(), det.rule, latency_ms)

        if self.on_alarm is not None:
            try:
                self.on_alarm(det, block, {"peak": peak, "latency_ms": latency_ms})
            except Exception:  # un'uscita rotta (buzzer, Bridge...) non deve fermare il demone
                log.exception("errore in on_alarm")


class Daemon:
    """Collega Pipeline a una sorgente di righe, con riconnessione e arresto pulito."""

    def __init__(self, pipeline: Pipeline, port: Optional[str] = None, input_path: Optional[str] = None,
                 baud: int = 230400, stats_every_s: float = 60.0, retry_max_s: float = 30.0):
        self.pipeline = pipeline
        self.port = port
        self.input_path = input_path
        self.baud = baud
        self.stats_every_s = stats_every_s
        self.retry_max_s = retry_max_s
        self._stop = False

    def stop(self, *_):
        self._stop = True

    def run(self) -> None:
        retry = 1.0
        last_stats = time.monotonic()
        while not self._stop:
            try:
                for line in open_lines(self.port, self.input_path, self.baud):
                    retry = 1.0  # la sorgente funziona: azzera l'attesa
                    self.pipeline.handle_line(line)
                    if self.stats_every_s and time.monotonic() - last_stats >= self.stats_every_s:
                        log.info("stats: %s", self.pipeline.stats.summary())
                        last_stats = time.monotonic()
                    if self._stop:
                        break
                if self.input_path is not None:
                    break  # fine file / stdin: il lavoro e' finito
            except OSError as e:  # include serial.SerialException
                if self.port is None:
                    raise
                log.error("seriale %s non disponibile (%s), riprovo tra %.0f s", self.port, e, retry)
                self._sleep(retry)
                retry = min(retry * 2, self.retry_max_s)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(0.2)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--port", help="porta seriale lato Linux")
    src.add_argument("--input", help="file con il flusso registrato ('-' = stdin)")
    p.add_argument("--baud", type=int, default=230400)
    p.add_argument("--db", default=str(DEFAULT_DB), help="file SQLite (':memory:' per non salvare)")
    p.add_argument("--time-col", choices=["index", "micros"], default="index")
    p.add_argument("--adc-bits", type=int, default=10, help="bit ADC del piezo (range e scala soglie)")
    p.add_argument("--block-timeout", type=float, default=5.0)
    p.add_argument("--cooldown", type=float, default=10.0,
                   help="secondi minimi tra due allarmi della stessa classe (0 = registra tutti)")
    p.add_argument("--stats-every", type=float, default=60.0, help="secondi tra due righe di statistiche (0 = mai)")
    p.add_argument("--print-all", action="store_true", help="logga ogni blocco, non solo gli allarmi")
    p.add_argument("--quiet", action="store_true", help="solo allarmi ed errori")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    level = logging.WARNING if args.quiet else (logging.DEBUG if args.print_all else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    th = Thresholds(adc_bits=args.adc_bits)
    parser = BlockParser(SAMPLES_PER_BLOCK, args.time_col, args.adc_bits, args.block_timeout)
    store = AlarmStore(args.db)
    pipeline = Pipeline(store, parser, classifier=lambda b: classify(b, th), cooldown_s=args.cooldown)
    daemon = Daemon(pipeline, args.port, args.input, args.baud, args.stats_every)

    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    log.info("avvio: sorgente %s, db %s, allarmi su %s", args.port or args.input, args.db,
             sorted(pipeline.alarm_classes))
    try:
        daemon.run()
    except OSError as e:
        log.error("errore di lettura: %s", e)
        return 1
    finally:
        store.close()
        log.warning("fine: %s", pipeline.stats.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
