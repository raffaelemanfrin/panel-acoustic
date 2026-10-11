#!/usr/bin/env python3
"""Emettitore di blocchi finti: stampa il protocollo come farebbe l'MCU.

ATTENZIONE: le forme d'onda sono inventate, servono solo a far girare la
pipeline (parser, SQLite, soglie, dashboard). NON addestrare la rete su questi dati.

Imitano il circuito finale: piezo collegato direttamente all'ADC (a riposo ~0,
solo semionde positive, ADC a 12 bit) e luce = tensione del pannello dal
partitore 100k/10k (~400-550 conteggi a 12 bit).

Esempi:
    python fake_mcu.py --n 10 --seed 1
    python fake_mcu.py --n 20 --classes grandine,colpo --interval 1 --corrupt-rate 0.2
    python fake_mcu.py --n 5 | python capture.py --input - --label grandine

La classe vera di ogni blocco va su stderr ("# 3 grandine"): serve per calcolare
matrice di confusione e falsi allarmi. Con --truth FILE la salva anche in CSV.
"""
import argparse
import math
import random
import sys
import time

from protocol import END, SAMPLE_PERIOD_MS, SAMPLES_PER_BLOCK, START, TRIGGER_INDEX

CLASSES = ["calma", "vento", "disturbo", "pioggia", "grandine", "colpo"]


def impulse(i, t0, amp, decay_ms, freq_hz):
    """Oscillazione smorzata che parte al campione t0."""
    if i < t0:
        return 0.0
    t = (i - t0) * SAMPLE_PERIOD_MS
    return amp * math.exp(-t / decay_ms) * math.sin(2 * math.pi * freq_hz * t / 1000.0)


LIGHT_K = 4.4  # luce: unita' "storiche" (~110) -> conteggi a 12 bit del partitore (~480)


def gen_block(cls, rng, adc_bits=12, n=SAMPLES_PER_BLOCK, shadow=False):
    """Ritorna le righe (indice, piezo, luce). Ampiezze del piezo pensate su scala 10 bit.

    shadow=True aggiunge un'ombra rapida al trigger (mano sul vetro) a qualunque classe.
    """
    full = 2 ** adc_bits - 1
    k = 2 ** adc_bits / 1024.0
    light0 = rng.randint(95, 125)
    T = TRIGGER_INDEX

    hits = []  # impulsi (posizione, ampiezza, decadimento ms, frequenza Hz)
    if cls == "colpo":
        hits = [(T, rng.uniform(300, 450), 15, rng.uniform(100, 200))]
    elif cls == "grandine":
        hits = [(T, rng.uniform(150, 350), 6, rng.uniform(300, 600))]
        hits += [(rng.randint(T + 20, n - 40), rng.uniform(100, 300), 6, rng.uniform(300, 600))
                 for _ in range(rng.randint(3, 7))]
    wind_f = rng.uniform(3, 8)
    wind_a = rng.uniform(15, 30)
    dist_f = rng.uniform(40, 90)

    rows = []
    for i in range(n):
        v = rng.gauss(0, 2)
        light = light0 + rng.gauss(0, 0.7)

        if cls == "vento":
            v += wind_a * math.sin(2 * math.pi * wind_f * i * SAMPLE_PERIOD_MS / 1000.0) + rng.gauss(0, 5)
        elif cls == "pioggia":
            v += rng.gauss(0, 6 if i < T else 10)
            if rng.random() < 0.04:
                v += rng.choice([-1, 1]) * rng.uniform(20, 45)
            light -= 6.0 * i / n  # calo lento (nuvola)
        elif cls == "grandine":
            light -= 10.0 * i / n
        elif cls == "disturbo":
            if T <= i < T + 160:
                v += 90 * math.sin(2 * math.pi * dist_f * (i - T) * SAMPLE_PERIOD_MS / 1000.0)
            if T <= i < T + 40:
                light -= 20  # calo veloce (ombra)
        if shadow and i >= T:
            light -= 20  # mano che resta sul pannello
        for h in hits:
            v += impulse(i, *h)

        p = int(round(min(max(v * k, 0), full)))  # niente polarizzazione: il negativo si perde
        rows.append((i, p, int(round(light * LIGHT_K))))
    return rows


def corrupt(lines, rng):
    """Rompe un blocco in uno dei modi tipici di un collegamento seriale."""
    kind = rng.choice(["short", "garbage", "no_end"])
    body = lines[1:-1]
    if kind == "short":
        cut = rng.randint(10, 40)
        body = body[:-cut]
        return [lines[0]] + body + [lines[-1]]
    if kind == "garbage":
        body[rng.randint(0, len(body) - 1)] = "xx,??"
        return [lines[0]] + body + [lines[-1]]
    return lines[:-1]  # END perso


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=10, help="numero di blocchi")
    p.add_argument("--classes", default=",".join(CLASSES), help="classi separate da virgola")
    p.add_argument("--order", choices=["cycle", "random"], default="cycle")
    p.add_argument("--adc-bits", type=int, default=12)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--interval", type=float, default=0.0, help="secondi di pausa tra blocchi")
    p.add_argument("--corrupt-rate", type=float, default=0.0, help="frazione di blocchi da rovinare")
    p.add_argument("--out", default="-", help="file di output ('-' = stdout)")
    p.add_argument("--truth", help="CSV con la classe vera di ogni blocco")
    args = p.parse_args()

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    unknown = [c for c in classes if c not in CLASSES]
    if unknown:
        sys.exit(f"Classi sconosciute: {unknown}. Valide: {CLASSES}")

    rng = random.Random(args.seed)
    out = sys.stdout if args.out == "-" else open(args.out, "w")
    truth = open(args.truth, "w") if args.truth else None
    if truth:
        truth.write("block,class,corrupted\n")

    try:
        for b in range(args.n):
            cls = classes[b % len(classes)] if args.order == "cycle" else rng.choice(classes)
            rows = gen_block(cls, rng, args.adc_bits)
            lines = [START] + [f"{i},{pz},{lu}" for i, pz, lu in rows] + [END]
            broken = rng.random() < args.corrupt_rate
            if broken:
                lines = corrupt(lines, rng)
            out.write("\n".join(lines) + "\n")
            out.flush()
            print(f"# {b} {cls}{' (rovinato)' if broken else ''}", file=sys.stderr)
            if truth:
                truth.write(f"{b},{cls},{int(broken)}\n")
            if args.interval:
                time.sleep(args.interval)
    finally:
        if out is not sys.stdout:
            out.close()
        if truth:
            truth.close()


if __name__ == "__main__":
    main()