#!/usr/bin/env python3
"""Demone di rilevamento: righe MCU -> parser -> classificatore -> registro allarmi.

Esempi:
    python daemon.py --bridge                                 # UNO Q: dati dalla MCU via arduino-router
    python daemon.py --port /dev/ttyACM0                      # MCU su una seriale vera (altre schede)
    python fakemcu.py --n 30 --interval 0.5 --corrupt-rate 0.2 | python daemon.py --input -
    python daemon.py --input registrazione.txt --db prova.db --print-all
    python fakemcu.py --n 200 --order random --interval 1 | python daemon.py --input - --siren silent

Cosa fa:
  - ogni blocco valido viene classificato sul piezo (oggi con le regole di
    detector.py, domani con la rete) -> nn_alarm;
  - dal blocco si ricavano light_fast / light_slow e la logica di logic.py
    decide allarme si/no e avviso si/no;
  - ogni blocco finisce nella tabella events (grafico), gli allarmi anche in
    alarms; allarmi e avvisi hanno un cooldown;
  - on_alarm (sirena sul Media Carrier, outputs.py) restituisce per quanti
    secondi resta attiva: in quel tempo, piu' una guardia, i blocchi che
    arrivano vengono scartati senza classificarli (la sirena non deve far
    scattare il piezo);
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
from typing import Callable, Dict, Optional, Tuple

from detector import ALARM_CLASSES, Detection, Thresholds, classify, light_flags
from logic import Decision, Logic
from protocol import (SAMPLE_PERIOD_MS, SAMPLES_PER_BLOCK, START, TRIGGER_INDEX,
                      Block, BlockParser, Rejected)
from source import open_lines
from storage import DEFAULT_DB, AlarmStore

log = logging.getLogger("daemon")

POST_TRIGGER_MS = (SAMPLES_PER_BLOCK - TRIGGER_INDEX) * SAMPLE_PERIOD_MS

# Un classificatore prende un Block e restituisce una Detection.
# Oggi: le regole. Domani: la rete Edge Impulse, con la stessa firma.
Classifier = Callable[[Block], Detection]
# on_alarm(det, block, info) -> secondi di uscita attiva (None/0 = nessuna sospensione)
OnAlarm = Callable[[Detection, Block, dict], Optional[float]]


@dataclass
class Stats:
    blocks_ok: int = 0
    blocks_rejected: int = 0
    by_class: Counter = field(default_factory=Counter)
    reject_reasons: Counter = field(default_factory=Counter)
    blocks_muted: int = 0
    alarms_stored: int = 0
    alarms_suppressed: int = 0
    warnings: int = 0

    def summary(self) -> str:
        cls = ", ".join(f"{k}={v}" for k, v in sorted(self.by_class.items())) or "-"
        return (f"blocchi validi {self.blocks_ok}, scartati {self.blocks_rejected}, "
                f"ignorati durante la sirena {self.blocks_muted} | {cls} | "
                f"allarmi registrati {self.alarms_stored}, soppressi (cooldown) {self.alarms_suppressed}, "
                f"avvisi {self.warnings}")


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
        on_alarm: Optional[OnAlarm] = None,
        clock: Callable[[], float] = time.monotonic,
        logic: Optional[Logic] = None,
        light: Callable[[Block], Tuple[bool, bool]] = light_flags,
        on_warning: Optional[Callable[[Detection, Block, dict], None]] = None,
        guard_s: float = 1.0,
    ):
        self.store = store
        self.parser = parser or BlockParser(clock=clock)
        self.classifier = classifier
        self.source_name = source_name
        self.cooldown_s = cooldown_s
        self.alarm_classes = set(alarm_classes)
        self.on_alarm = on_alarm
        self.on_warning = on_warning
        self.clock = clock
        self.logic = logic or Logic()
        self.light = light
        # guardia dopo la fine dell'uscita: copre la finestra del blocco (50 ms
        # prima + 200 ms dopo il trigger), la trasmissione e lo smorzamento del vetro
        self.guard_s = guard_s
        self.stats = Stats()
        self.last_decision: Optional[Decision] = None
        self._t_start: Optional[float] = None
        self._last_alarm: Dict[str, float] = {}
        self._mute_until = float("-inf")

    @property
    def muted(self) -> bool:
        return self.clock() < self._mute_until

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

        t_start, self._t_start = self._t_start, None
        if t_start is not None and t_start < self._mute_until:
            # START arrivato mentre la sirena suonava (o nella guardia): il blocco
            # contiene la sirena, non un evento sul pannello
            self.stats.blocks_muted += 1
            log.debug("blocco ignorato: uscita attiva")
            return None

        det = self.classifier(result)
        now = self.clock()
        transit_ms = (now - t_start) * 1000 if t_start is not None else 0.0
        latency_ms = POST_TRIGGER_MS + transit_ms

        nn_alarm = det.cls in self.alarm_classes
        light_fast, light_slow = self.light(result)
        dec = self.logic.decide(nn_alarm, light_fast, light_slow)
        self.last_decision = dec

        self.stats.blocks_ok += 1
        self.stats.by_class[det.cls] += 1
        peak = max(result.piezo)
        log.debug("blocco: %-8s (%s) %s picco=%d latenza=%.0f ms",
                  det.cls, det.rule, dec.describe(), peak, latency_ms)
        if self.store is not None:
            self.store.add_event(det.cls, self.source_name, dec.label, peak=peak, nn_alarm=nn_alarm,
                                 light_fast=light_fast, light_slow=light_slow,
                                 latency_ms=round(latency_ms, 1))

        info = {"peak": peak, "latency_ms": latency_ms, "decision": dec}
        if dec.alarm:
            # con shadow_alarm l'allarme puo' arrivare dalla sola ombra: classe "ombra"
            name = det.cls if nn_alarm else "ombra"
            self._alarm(name, det, result, info, now)
        elif dec.warning:
            self._warning(det, result, info, now)
        return det

    def _cooldown_hit(self, key: str, now: float) -> bool:
        last = self._last_alarm.get(key)
        if last is not None and now - last < self.cooldown_s:
            return True
        self._last_alarm[key] = now
        return False

    def _alarm(self, name: str, det: Detection, block: Block, info: dict, now: float) -> None:
        if self._cooldown_hit(name, now):
            self.stats.alarms_suppressed += 1
            log.info("allarme %s soppresso (cooldown %.0f s)", name, self.cooldown_s)
            return

        dec: Decision = info["decision"]
        confidence = getattr(det, "confidence", None)
        if self.store is not None:
            self.store.add(name, self.source_name, confidence=confidence, peak=info["peak"],
                           latency_ms=round(info["latency_ms"], 1), detail=f"{det.rule} | {dec.describe()}")
        self.stats.alarms_stored += 1
        log.warning("ALLARME %s (%s, %s) latenza=%.0f ms", name.upper(), det.rule, dec.describe(),
                    info["latency_ms"])

        if self.on_alarm is not None:
            try:
                active_s = self.on_alarm(det, block, {**info, "name": name})
            except Exception:  # un'uscita rotta (buzzer, Bridge...) non deve fermare il demone
                log.exception("errore in on_alarm")
                return
            if active_s:
                self._mute_until = max(self._mute_until, self.clock() + active_s + self.guard_s)
                log.info("rilevamento sospeso per %.1f s (uscita %.1f s + guardia %.1f s)",
                         active_s + self.guard_s, active_s, self.guard_s)

    def _warning(self, det: Detection, block: Block, info: dict, now: float) -> None:
        if self._cooldown_hit("avviso", now):
            return
        self.stats.warnings += 1
        log.warning("AVVISO calo lento di luce, neve/sporco? (%s)", info["decision"].describe())
        if self.on_warning is not None:
            try:
                self.on_warning(det, block, info)
            except Exception:
                log.exception("errore in on_warning")


class Daemon:
    """Collega Pipeline a una sorgente di righe, con riconnessione e arresto pulito."""

    def __init__(self, pipeline: Pipeline, port: Optional[str] = None, input_path: Optional[str] = None,
                 baud: int = 230400, stats_every_s: float = 60.0, retry_max_s: float = 30.0,
                 bridge: bool = False):
        self.pipeline = pipeline
        self.bridge = bridge
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
                for line in open_lines(self.port, self.input_path, self.baud, bridge=self.bridge):
                    retry = 1.0  # la sorgente funziona: azzera l'attesa
                    self.pipeline.handle_line(line)
                    if self.stats_every_s and time.monotonic() - last_stats >= self.stats_every_s:
                        log.info("stats: %s", self.pipeline.stats.summary())
                        last_stats = time.monotonic()
                    if self._stop:
                        break
                if self.input_path is not None:
                    break  # fine file / stdin: il lavoro e' finito
            except (OSError, ConnectionError) as e:  # include serial.SerialException
                if self.input_path is not None:
                    raise
                log.error("sorgente %s non disponibile (%s), riprovo tra %.0f s",
                          self.port or "Bridge", e, retry)
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
    src.add_argument("--bridge", action="store_true",
                     help="UNO Q: blocchi dalla MCU via arduino-router (notifiche pa_lines)")
    p.add_argument("--baud", type=int, default=230400, help="solo con --port")
    p.add_argument("--db", default=str(DEFAULT_DB), help="file SQLite (':memory:' per non salvare)")
    p.add_argument("--time-col", choices=["index", "micros"], default="index")
    p.add_argument("--adc-bits", type=int, default=12,
                   help="bit ADC del piezo (range e scala soglie); lo sketch usa 12")
    p.add_argument("--block-timeout", type=float, default=5.0)
    p.add_argument("--cooldown", type=float, default=10.0,
                   help="secondi minimi tra due allarmi della stessa classe (0 = registra tutti)")
    p.add_argument("--stats-every", type=float, default=60.0, help="secondi tra due righe di statistiche (0 = mai)")
    p.add_argument("--print-all", action="store_true", help="logga ogni blocco, non solo gli allarmi")
    p.add_argument("--quiet", action="store_true", help="solo allarmi ed errori")
    p.add_argument("--shadow-alarm", action="store_true",
                   help="ombra senza urto = allarme (default: no, vedi logic.py)")
    o = p.add_argument_group("uscite (Media Carrier)")
    o.add_argument("--siren", choices=["auto", "on", "silent", "off"], default="auto",
                   help="on = audio + LED; silent = solo LED (e sospensione); off = nessuna uscita; "
                        "auto = on con --port/--bridge, off con --input (un file si legge in un attimo e la "
                        "sospensione a tempo reale scarterebbe quasi tutto)")
    o.add_argument("--siren-s", type=float, default=3.0, help="durata della sirena in secondi")
    o.add_argument("--guard", type=float, default=1.0,
                   help="secondi di sospensione extra dopo la sirena (finestra del blocco + vetro che vibra)")
    o.add_argument("--siren-cmd", help='comando audio, es. "aplay -D plughw:1,0 {wav}"')
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    level = logging.WARNING if args.quiet else (logging.DEBUG if args.print_all else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    th = Thresholds(adc_bits=args.adc_bits)
    parser = BlockParser(SAMPLES_PER_BLOCK, args.time_col, args.adc_bits, args.block_timeout)
    store = AlarmStore(args.db)
    siren = args.siren if args.siren != "auto" else ("on" if args.port or args.bridge else "off")
    out = None
    if siren != "off":
        from outputs import MediaCarrierOutput
        out = MediaCarrierOutput(args.siren_s, sound=(siren == "on"), siren_cmd=args.siren_cmd)
    pipeline = Pipeline(store, parser, classifier=lambda b: classify(b, th), cooldown_s=args.cooldown,
                        logic=Logic(args.shadow_alarm), light=lambda b: light_flags(b, th),
                        on_alarm=out.alarm if out else None, on_warning=out.warning if out else None,
                        guard_s=args.guard)
    daemon = Daemon(pipeline, args.port, args.input, args.baud, args.stats_every, bridge=args.bridge)

    import threading
    if threading.current_thread() is threading.main_thread():  # in App Lab gira in un thread
        signal.signal(signal.SIGTERM, daemon.stop)
        signal.signal(signal.SIGINT, daemon.stop)
    log.info("avvio: sorgente %s, db %s, allarmi su %s, ombra senza urto = %s, sirena %s",
             args.port or args.input or "Bridge", args.db, sorted(pipeline.alarm_classes),
             "allarme" if args.shadow_alarm else "no", siren)
    try:
        daemon.run()
    except OSError as e:
        log.error("errore di lettura: %s", e)
        return 1
    finally:
        if out is not None:
            out.stop()
        store.close()
        log.warning("fine: %s", pipeline.stats.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
