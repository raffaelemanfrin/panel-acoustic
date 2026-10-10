"""Da dove arrivano le righe del protocollo.

Tutte le sorgenti sono generatori di stringhe. Quella seriale produce una
stringa vuota quando scade il timeout di lettura, cosi' il parser puo'
controllare il timeout del blocco anche a flusso fermo.

Se in App Lab passerete dal Bridge invece che da una tty, basta aggiungere
qui una nuova funzione: il resto del codice non cambia.
"""
from __future__ import annotations

import sys
from typing import Iterator, Optional


def serial_lines(port: str, baud: int = 115200, timeout: float = 1.0) -> Iterator[str]:
    import serial  # import pigro: serve solo con la scheda collegata

    ser = serial.Serial(port, baud, timeout=timeout)
    ser.reset_input_buffer()
    try:
        while True:
            yield ser.readline().decode("ascii", errors="ignore")
    finally:
        ser.close()


def file_lines(path: str) -> Iterator[str]:
    """Legge da file; '-' = stdin (utile in pipe con fake_mcu.py)."""
    if path == "-":
        yield from sys.stdin
        return
    with open(path, encoding="ascii", errors="ignore") as f:
        yield from f


def open_lines(port: Optional[str] = None, input_path: Optional[str] = None, baud: int = 115200) -> Iterator[str]:
    if (port is None) == (input_path is None):
        raise ValueError("indica esattamente uno tra --port e --input")
    return serial_lines(port, baud) if port else file_lines(input_path)