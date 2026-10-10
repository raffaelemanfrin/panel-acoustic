"""Uscite del demone sull'UNO Q + Media Carrier: sirena audio e LED.

Il Media Carrier porta l'audio della UNO Q su jack da 3,5 mm (uscita line-out
per un altoparlante amplificato). Lato Linux l'audio passa da PipeWire (il
socket e' gia' montato nel container dell'App), quindi la sirena e' un WAV
riprodotto con pw-play, con paplay o aplay come ripiego. Se il dispositivo di
default non e' il jack giusto si passa il comando a mano:
    --siren-cmd "aplay -D plughw:1,0 {wav}"

Il LED rosso della UNO Q (/sys/class/leds/red:user, montato nel container) si
accende per la durata dell'allarme. Per un avviso si accendono rosso e verde
insieme (giallo), senza suono.

Niente blocca il demone: il player gira in un processo a parte e un timer
spegne tutto. alarm() restituisce la durata dell'uscita, cosi' la Pipeline
sospende il rilevamento finche' la sirena suona (checklist: la sirena non deve
far scattare il piezo).
"""
from __future__ import annotations

import logging
import math
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("outputs")

LED_ROOT = Path("/sys/class/leds")
PLAYERS = (["pw-play"], ["paplay"], ["aplay", "-q"])


def make_siren_wav(path: Path, seconds: float = 3.0, rate: int = 16000,
                   f_low: float = 650.0, f_high: float = 1300.0, volume: float = 0.6) -> Path:
    """Sirena bitonale (alterna due toni ogni 0,25 s), mono 16 bit."""
    n = int(seconds * rate)
    half = int(0.25 * rate)
    phase, frames = 0.0, bytearray()
    for i in range(n):
        f = f_low if (i // half) % 2 == 0 else f_high
        phase += 2 * math.pi * f / rate
        fade = min(1.0, i / (0.01 * rate), (n - i) / (0.01 * rate))  # niente click all'inizio/fine
        frames += struct.pack("<h", int(32767 * volume * fade * math.sin(phase)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class Led:
    def __init__(self, name: str, root: Path = LED_ROOT):
        self.path = root / name / "brightness"

    @property
    def available(self) -> bool:
        return self.path.exists()

    def set(self, on: bool) -> None:
        try:
            self.path.write_text("1" if on else "0")
        except OSError as e:
            log.debug("LED %s non scrivibile: %s", self.path, e)


class MediaCarrierOutput:
    """Callback per Pipeline: on_alarm=out.alarm, on_warning=out.warning."""

    def __init__(self, siren_s: float = 3.0, sound: bool = True, siren_cmd: Optional[str] = None,
                 led_root: Path = LED_ROOT, wav_dir: Optional[Path] = None):
        self.siren_s = siren_s
        self.sound = sound
        self.red = Led("red:user", led_root)
        self.green = Led("green:user", led_root)
        self._proc: Optional[subprocess.Popen] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self.cmd: Optional[List[str]] = None
        if sound:
            wav_dir = wav_dir or Path(tempfile.gettempdir())
            self.wav = make_siren_wav(wav_dir / "solar_acoustics_siren.wav", siren_s)
            self.cmd = self._player(siren_cmd)
            if self.cmd is None:
                log.warning("nessun player audio trovato (pw-play/paplay/aplay): sirena solo simulata")
        leds = [l.path.parent.name for l in (self.red, self.green) if l.available]
        log.info("uscite: audio %s, LED %s", " ".join(self.cmd) if self.cmd else "no", leds or "no")

    def _player(self, siren_cmd: Optional[str]) -> Optional[List[str]]:
        if siren_cmd:
            return [a.replace("{wav}", str(self.wav)) for a in shlex.split(siren_cmd)]
        for p in PLAYERS:
            if shutil.which(p[0]):
                return p + [str(self.wav)]
        return None

    def alarm(self, det, block, info) -> float:
        """Avvia sirena + LED rosso; restituisce per quanti secondi restano attivi."""
        with self._lock:
            self._stop_locked()
            self.red.set(True)
            self.green.set(False)
            if self.cmd:
                try:
                    self._proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL)
                except OSError as e:
                    log.error("sirena non avviata (%s): %s", " ".join(self.cmd), e)
            self._timer = threading.Timer(self.siren_s, self.stop)
            self._timer.daemon = True
            self._timer.start()
        log.info("sirena %.1f s per %s", self.siren_s, info.get("name", det.cls))
        # anche senza audio si sospende il rilevamento: il comportamento in demo
        # resta lo stesso che si prova sul PC
        return self.siren_s

    def warning(self, det, block, info) -> None:
        """Avviso neve/sporco: LED giallo fisso, nessun suono (si spegne al prossimo allarme o stop)."""
        with self._lock:
            if self._proc is None:
                self.red.set(True)
                self.green.set(True)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        self.red.set(False)
        self.green.set(False)
