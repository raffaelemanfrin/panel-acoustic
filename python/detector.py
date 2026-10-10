"""Regole a soglia (piano B) e riferimento di confronto per la rete neurale.

Prende un Block (500 campioni, trigger all'indice 100) e restituisce la classe.
classify() guarda SOLO il piezo, come la rete: cosi' regole e rete sono
intercambiabili come ingresso nn_alarm della logica (logic.py). La luce entra
nella decisione solo attraverso light_flags() -> light_fast / light_slow.
Le soglie in Thresholds sono scalate su 10 bit (0-1023): con un ADC a 12 bit
si imposta adc_bits=12 e le ampiezze vengono riportate sulla stessa scala.

ATTENZIONE: i valori di default sono tarati sui blocchi di fakemcu.py. Servono
a far girare la pipeline; vanno ritarati sui dati reali della MCU.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from protocol import SAMPLE_PERIOD_MS, TRIGGER_INDEX, Block

# Classi che fanno scattare un allarme. DA CONFERMARE con il team.
ALARM_CLASSES = {"grandine", "colpo"}


@dataclass
class Thresholds:
    adc_bits: int = 10
    calm_rms: float = 4.0           # sotto questo rms post-trigger: calma
    hit_thr: float = 80.0           # ampiezza minima per contare un urto
    hit_gap_ms: float = 8.0         # urti piu' vicini di cosi' sono lo stesso urto
    colpo_max_len_ms: float = 40.0  # un colpo si smorza in fretta
    grandine_min_hits: int = 3      # numero di urti distinti per dire grandine
    zcr_rain: float = 0.15          # attraversamenti dello zero per campione (pioggia = rumore fitto)
    zcr_band: float = 2.0           # isteresi per non contare il rumore di fondo
    impact_min_amp: float = 130.0   # sotto questo picco un "urto" e' solo oscillazione (disturbo)
    light_fast_drop: float = 10.0   # calo luce rapido (ombra) entro 30 ms dal trigger
    light_slow_drop: float = 4.0    # calo luce lento (nuvola) a fine blocco


@dataclass
class Detection:
    cls: str
    alarm: bool
    rule: str
    features: Dict[str, float]
    confidence: Optional[float] = None   # dalla rete; None per le regole


def extract_features(block: Block, th: Thresholds) -> Dict[str, float]:
    T = TRIGGER_INDEX
    scale = 1024.0 / 2 ** th.adc_bits
    base = statistics.median(block.piezo)  # robusto agli urti e indipendente dall'offset
    x = [(v - base) * scale for v in block.piezo]
    post = x[T:]

    amp = max(abs(v) for v in post)
    rms = math.sqrt(sum(v * v for v in post) / len(post))

    # urti distinti: gli attraversamenti ravvicinati della soglia sono lo stesso urto
    gap = max(1, int(th.hit_gap_ms / SAMPLE_PERIOD_MS))
    hits, last, first_start, first_end = 0, None, 0, 0
    for i, v in enumerate(post):
        if abs(v) > th.hit_thr:
            if last is None or i - last > gap:
                hits += 1
                if hits == 1:
                    first_start = i
            if hits == 1:
                first_end = i
            last = i
    first_len_ms = (first_end - first_start + 1) * SAMPLE_PERIOD_MS if hits else 0.0

    # attraversamenti dello zero con isteresi: alti per rumore fitto, bassi per vento
    crossings, state = 0, 0
    for v in post:
        s = 1 if v > th.zcr_band else (-1 if v < -th.zcr_band else 0)
        if s and state and s != state:
            crossings += 1
        if s:
            state = s
    zcr = crossings / len(post)

    return {"amp": amp, "rms": rms, "hits": hits, "first_len_ms": first_len_ms, "zcr": zcr,
            **light_features(block, th)}


def light_features(block: Block, th: Thresholds) -> Dict[str, float]:
    """Calo di luce rispetto al pre-trigger: rapido (ombra) e lento (copertura)."""
    T = TRIGGER_INDEX
    light_base = statistics.median(block.luce[:T])
    fast_window = int(30 / SAMPLE_PERIOD_MS)
    light_fast = light_base - min(block.luce[T:T + fast_window])
    light_slow = light_base - statistics.mean(block.luce[-50:])
    return {
        "light_fast_drop": light_fast, "light_slow_drop": light_slow,
        "light_fast": light_fast >= th.light_fast_drop,   # ingressi della logica booleana
        "light_slow": light_slow >= th.light_slow_drop,
    }


def light_flags(block: Block, th: Thresholds = None) -> Tuple[bool, bool]:
    """(light_fast, light_slow) per logic.decide()."""
    f = light_features(block, th or Thresholds())
    return f["light_fast"], f["light_slow"]


def classify(block: Block, th: Thresholds = None) -> Detection:
    th = th or Thresholds()
    f = extract_features(block, th)

    if f["hits"] >= 1 and f["amp"] < th.impact_min_amp:
        # un'oscillazione che sfiora la soglia viene spezzata in piu' "urti":
        # senza un picco netto non e' un impatto (prima si usava il calo di luce,
        # ora la luce resta fuori dal classificatore e va nella logica)
        cls, rule = "disturbo", "oscillazione senza picco d'urto"
    elif f["hits"] >= th.grandine_min_hits:
        cls, rule = "grandine", "urti multipli"
    elif f["hits"] >= 1:
        if f["first_len_ms"] > th.colpo_max_len_ms:
            cls, rule = "disturbo", "evento lungo"
        else:
            cls, rule = "colpo", "urto singolo e breve"
    elif f["rms"] < th.calm_rms:
        cls, rule = "calma", "rms basso"
    elif f["zcr"] >= th.zcr_rain:
        cls, rule = "pioggia", "rumore fitto sotto soglia"
    else:
        cls, rule = "vento", "oscillazione lenta sotto soglia"

    return Detection(cls, cls in ALARM_CLASSES, rule, f)