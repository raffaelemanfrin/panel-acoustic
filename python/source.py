"""Da dove arrivano le righe del protocollo.

Tutte le sorgenti sono generatori di stringhe. Quella seriale produce una
stringa vuota quando scade il timeout di lettura, cosi' il parser puo'
controllare il timeout del blocco anche a flusso fermo.

Sulla UNO Q la MCU non ha una tty verso Linux (l'UART interno e' di
arduino-router): lo sketch manda il testo del protocollo come notifiche Bridge
"pa_lines" e bridge_lines() le trasforma nelle stesse righe di una seriale.
"""
from __future__ import annotations

import queue
import sys
from typing import Callable, Iterator, Optional, Tuple

BRIDGE_METHOD = "pa_lines"   # notifiche MCU -> Linux con le righe del protocollo
FORCE_METHOD = "pa_force"    # Linux -> MCU: invia N blocchi senza trigger (calma, vento)


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


def _open_bridge() -> Tuple[object, Callable[[], None]]:
    """Client del router: libreria arduino-router-bridge se c'e', altrimenti quella di App Lab."""
    try:
        from arduino.router_bridge import Bridge  # pip install arduino-router-bridge
    except ImportError:
        try:
            from arduino.app_utils import Bridge as app_bridge  # dentro un'App di App Lab
        except ImportError as e:
            raise OSError("nessuna libreria Bridge: pip install arduino-router-bridge "
                          "oppure avvia dentro App Lab") from e
        return app_bridge, lambda: None
    b = Bridge()
    if not b.connect(timeout=10):
        b.disconnect()
        raise OSError("arduino-router non raggiungibile (/var/run/arduino-router.sock)")
    return b, b.disconnect


def bridge_lines(method: str = BRIDGE_METHOD, force: int = 0, timeout: float = 1.0,
                 bridge=None) -> Iterator[str]:
    """Righe del protocollo ricevute dal Bridge. "" ogni `timeout` s senza dati.

    force > 0 chiede alla MCU N blocchi forzati (per registrare calma e vento).
    Un solo processo alla volta puo' ricevere "pa_lines" (daemon O capture).
    """
    lines: "queue.Queue[str]" = queue.Queue()

    def on_chunk(text) -> None:
        for line in str(text).splitlines():
            lines.put(line)

    close = lambda: None  # noqa: E731
    if bridge is None:
        bridge, close = _open_bridge()
    bridge.provide(method, on_chunk)
    try:
        if force:
            bridge.call(FORCE_METHOD, int(force))
        while True:
            try:
                yield lines.get(timeout=timeout)
            except queue.Empty:
                yield ""
    finally:
        close()


def open_lines(port: Optional[str] = None, input_path: Optional[str] = None, baud: int = 115200,
               bridge: bool = False, force: int = 0) -> Iterator[str]:
    if sum((port is not None, input_path is not None, bool(bridge))) != 1:
        raise ValueError("indica esattamente uno tra --port, --input e --bridge")
    if bridge:
        return bridge_lines(force=force)
    return serial_lines(port, baud) if port else file_lines(input_path)