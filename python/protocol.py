"""Protocollo MCU -> Linux: parsing e validazione dei blocchi evento.

Nessuna dipendenza da hardware: si testa sul PC con file di testo.

Formato (una riga per campione):
    ---EVENT_START---
    indice,valore_piezo,valore_luce        (SAMPLES_PER_BLOCK righe)
    ---EVENT_END---
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Union

START = "---EVENT_START---"
END = "---EVENT_END---"
SAMPLE_RATE_HZ = 2000
SAMPLES_PER_BLOCK = 500
TRIGGER_INDEX = 100  # l'impatto cade sempre sul campione 100
SAMPLE_PERIOD_MS = 1000.0 / SAMPLE_RATE_HZ


@dataclass
class Block:
    t_ms: List[float]
    piezo: List[int]
    luce: List[int]


@dataclass
class Rejected:
    reason: str


class BlockParser:
    """Si alimenta riga per riga con feed().

    Ritorna un Block quando il blocco e' valido, un Rejected quando viene
    scartato, None altrimenti. Le righe fuori dal blocco (log di debug) sono
    ignorate. Una riga vuota serve a controllare il timeout (poll_timeout).
    """

    def __init__(
        self,
        samples: int = SAMPLES_PER_BLOCK,
        time_col: str = "index",
        adc_bits: int = 0,
        block_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if time_col not in ("index", "micros"):
            raise ValueError("time_col deve essere 'index' o 'micros'")
        self.samples = samples
        self.time_col = time_col
        self.adc_bits = adc_bits
        self.block_timeout = block_timeout
        self._clock = clock
        self._rows: Optional[list] = None
        self._bad = False
        self._t_start = 0.0

    def poll_timeout(self) -> Optional[Rejected]:
        if self._rows is not None and self._clock() - self._t_start > self.block_timeout:
            self._rows = None
            return Rejected("blocco incompleto (timeout)")
        return None

    def feed(self, line: str) -> Union[Block, Rejected, None]:
        line = line.strip()
        if not line:
            return self.poll_timeout()

        if line == START:
            result = Rejected("START inatteso, blocco parziale scartato") if self._rows is not None else None
            self._rows, self._bad, self._t_start = [], False, self._clock()
            return result

        if line == END:
            if self._rows is None:
                return None
            rows, bad = self._rows, self._bad
            self._rows = None
            if bad:
                return Rejected("righe malformate nel blocco")
            return self._finish(rows)

        if self._rows is None:
            return None  # debug fuori dal blocco

        try:
            a, b, c = (int(x) for x in line.split(","))
        except ValueError:
            self._bad = True
            return None
        self._rows.append((a, b, c))
        if len(self._rows) > self.samples:
            self._rows = None  # END perso: ignora fino al prossimo START
            return Rejected(f"piu' di {self.samples} righe nel blocco")
        return None

    def _finish(self, rows: list) -> Union[Block, Rejected]:
        n = len(rows)
        if n != self.samples:
            return Rejected(f"{n} righe invece di {self.samples}")

        first = [r[0] for r in rows]
        if self.time_col == "index":
            if first != list(range(n)):
                return Rejected("indici non progressivi 0..N-1")
            t_ms = [i * SAMPLE_PERIOD_MS for i in range(n)]
        else:
            if any(b <= a for a, b in zip(first, first[1:])):
                return Rejected("micros non crescenti")
            t_ms = [(v - first[0]) / 1000.0 for v in first]

        piezo = [r[1] for r in rows]
        if self.adc_bits:
            top = 2 ** self.adc_bits - 1
            if any(v < 0 or v > top for v in piezo):
                return Rejected(f"valore piezo fuori range 0-{top}")

        return Block(t_ms, piezo, [r[2] for r in rows])