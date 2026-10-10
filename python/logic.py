#!/usr/bin/env python3
"""Logica booleana finale: piezo (rete o regole) + luce -> allarme si/no, avviso si/no.

Ingressi (un bit ciascuno, calcolati su ogni blocco evento):
    A = nn_alarm    il classificatore del piezo dice grandine o colpo
    F = light_fast  calo di luce rapido entro 30 ms dal trigger (ombra: mano, persona)
    S = light_slow  calo di luce lento a fine blocco (copertura: neve, sporco, nuvola)

Uscite:
    Y = allarme     sirena + registro allarmi
    W = avviso      solo segnalazione (dashboard / LED), niente sirena

Decisioni del team (tabella sotto):
  - un colpo va segnalato anche senza ombra:     A=1, F=0 -> Y=1
  - mano e colpo sul vetro (demo):               A=1, F=1 -> Y=1
  - neve/sporco e' solo un avviso, mai allarme:  A=0, F=0, S=1 -> Y=0, W=1
  - ombra senza urto (A=0, F=1): trigger del piezo che la rete scarta (vento,
    disturbo) con un'ombra. Di default NON e' allarme, coerente con la classe
    "disturbo" dei dati finti. Con shadow_alarm=True diventa allarme
    (manomissione / furto). E' l'unica casella ancora da confermare.

W = S AND NOT Y: l'avviso compare solo se non c'e' gia' un allarme.

Nota: light_slow non genera trigger da solo (una copertura lenta non fa vibrare
il piezo), quindi S si vede solo quando un blocco arriva per altri motivi.
La teniamo comunque nella tabella, cosi' il comportamento e' definito.

    python logic.py                 # tabella di verita', mappa di Karnaugh, forma minima
    python logic.py --shadow-alarm  # la stessa con ombra senza urto = allarme
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from itertools import combinations, product
from typing import Dict, List, Tuple

NAMES = ("A", "F", "S")
LONG_NAMES = {"A": "nn_alarm", "F": "light_fast", "S": "light_slow"}
GRAY = [(0, 0), (0, 1), (1, 1), (1, 0)]  # ordine delle colonne F S nella mappa


def alarm_table(shadow_alarm: bool = False) -> Dict[Tuple[int, int, int], int]:
    """Tabella di verita' di Y, chiave (A, F, S)."""
    t = {}
    for a, f, s in product((0, 1), repeat=3):
        if a:
            y = 1                       # urto riconosciuto: allarme, con o senza ombra
        elif f:
            y = int(shadow_alarm)       # ombra senza urto: da decidere (default no)
        else:
            y = 0                       # niente urto, niente ombra: al massimo avviso
        t[(a, f, s)] = y
    return t


@dataclass(frozen=True)
class Decision:
    alarm: bool
    warning: bool
    inputs: Tuple[int, int, int]

    @property
    def label(self) -> str:
        return "allarme" if self.alarm else ("avviso" if self.warning else "-")

    def describe(self) -> str:
        a, f, s = self.inputs
        return f"A={a} F={f} S={s} -> Y={int(self.alarm)} W={int(self.warning)}"


class Logic:
    def __init__(self, shadow_alarm: bool = False):
        self.shadow_alarm = shadow_alarm
        self.table = alarm_table(shadow_alarm)

    def decide(self, nn_alarm: bool, light_fast: bool, light_slow: bool) -> Decision:
        key = (int(bool(nn_alarm)), int(bool(light_fast)), int(bool(light_slow)))
        y = bool(self.table[key])
        w = bool(key[2]) and not y
        return Decision(y, w, key)


def decide(nn_alarm: bool, light_fast: bool, light_slow: bool, shadow_alarm: bool = False) -> Decision:
    return Logic(shadow_alarm).decide(nn_alarm, light_fast, light_slow)


# ---------------------------------------------------------------- minimizzazione
# 3 variabili: si enumerano tutti i cubi (0, 1 o '-' per variabile), si tengono
# gli implicanti primi e si cerca la copertura con meno termini/letterali.
# Abbastanza piccolo da fare a forza bruta, ed e' lo stesso risultato della mappa.

Cube = Tuple[str, str, str]


def _covers(cube: Cube, m: Tuple[int, int, int]) -> bool:
    return all(c == "-" or int(c) == v for c, v in zip(cube, m))


def _minterms(cube: Cube):
    return [m for m in product((0, 1), repeat=3) if _covers(cube, m)]


def prime_implicants(ones: List[Tuple[int, int, int]]) -> List[Cube]:
    implicants = [c for c in product("01-", repeat=3) if all(m in ones for m in _minterms(c))]
    primes = []
    for c in implicants:
        bigger = [d for d in implicants if d != c and set(_minterms(c)) < set(_minterms(d))]
        if not bigger:
            primes.append(c)
    return primes


def minimal_sop(table: Dict[Tuple[int, int, int], int]) -> List[Cube]:
    ones = [m for m, v in table.items() if v]
    if not ones:
        return []
    if len(ones) == 8:
        return [("-", "-", "-")]
    primes = prime_implicants(ones)
    best = None
    for r in range(1, len(primes) + 1):
        for combo in combinations(primes, r):
            if all(any(_covers(c, m) for c in combo) for m in ones):
                cost = sum(ch != "-" for c in combo for ch in c)
                if best is None or cost < best[0]:
                    best = (cost, list(combo))
        if best:
            break
    return best[1]


def cube_to_text(cube: Cube) -> str:
    lits = [n if c == "1" else n + "'" for n, c in zip(NAMES, cube) if c != "-"]
    return "".join(lits) or "1"


def sop_to_text(cubes: List[Cube]) -> str:
    return " + ".join(cube_to_text(c) for c in cubes) if cubes else "0"


def karnaugh_text(table: Dict[Tuple[int, int, int], int], title: str) -> str:
    head = "    ".join(f"{f}{s}" for f, s in GRAY)
    lines = [title, f"        FS = {head}"]
    for a in (0, 1):
        cells = "    ".join(f" {table[(a, f, s)]}" for f, s in GRAY)
        lines.append(f"   A = {a}      {cells}")
    return "\n".join(lines)


def report(shadow_alarm: bool = False) -> str:
    logic = Logic(shadow_alarm)
    y = logic.table
    w = {k: int(logic.decide(*k).warning) for k in y}
    out = ["Ingressi: " + ", ".join(f"{n} = {LONG_NAMES[n]}" for n in NAMES),
           f"Ombra senza urto = allarme: {'si' if shadow_alarm else 'no'}", "",
           " A F S | Y W | esito"]
    for k in sorted(y):
        d = logic.decide(*k)
        out.append(f" {k[0]} {k[1]} {k[2]} | {y[k]} {w[k]} | {d.label}")
    out += ["", karnaugh_text(y, "Mappa di Karnaugh di Y (allarme)"),
            f"   Y = {sop_to_text(minimal_sop(y))}", "",
            karnaugh_text(w, "Mappa di Karnaugh di W (avviso)"),
            f"   W = {sop_to_text(minimal_sop(w))}"]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shadow-alarm", action="store_true", help="ombra senza urto = allarme")
    print(report(p.parse_args().shadow_alarm))


if __name__ == "__main__":
    main()
