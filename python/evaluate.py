#!/usr/bin/env python3
"""Valuta il detector su blocchi finti con classe nota.

    python evaluate.py --n 200 --seed 1
    python evaluate.py --n 200 --triggers-per-hour 40 --baud 460800

Stampa matrice di confusione, esito allarme si/no e latenza stimata.
Con dati finti il risultato verifica la pipeline, NON le prestazioni vere:
quando arrivano i CSV reali si valuta sullo stesso schema.
"""
import argparse
import random
import time

from detector import ALARM_CLASSES, Thresholds, classify
from fakemcu import CLASSES, gen_block
from protocol import END, SAMPLE_PERIOD_MS, SAMPLES_PER_BLOCK, START, TRIGGER_INDEX, Block


def to_block(rows):
    return Block([i * SAMPLE_PERIOD_MS for i in range(len(rows))],
                 [r[1] for r in rows], [r[2] for r in rows])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=200, help="blocchi per classe")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--adc-bits", type=int, default=10)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--triggers-per-hour", type=float, default=None,
                   help="trigger/ora NON di allarme in campo (da misurare); abilita i falsi allarmi/ora")
    args = p.parse_args()

    rng = random.Random(args.seed)
    th = Thresholds(adc_bits=args.adc_bits)
    conf = {t: {c: 0 for c in CLASSES} for t in CLASSES}
    proc_ms, block_bytes = [], 0

    for truth in CLASSES:
        for _ in range(args.n):
            rows = gen_block(truth, rng, args.adc_bits)
            if not block_bytes:  # dimensione di un blocco trasmesso
                lines = [START] + [f"{i},{a},{b}" for i, a, b in rows] + [END]
                block_bytes = len("\n".join(lines)) + 1
            block = to_block(rows)
            t0 = time.perf_counter()
            pred = classify(block, th).cls
            proc_ms.append((time.perf_counter() - t0) * 1000)
            conf[truth][pred] += 1

    w = 9
    print("\nMatrice di confusione (righe = classe vera, colonne = classe prevista)")
    print(" " * w + "".join(f"{c:>{w}}" for c in CLASSES) + f"{'recall':>{w}}")
    for t in CLASSES:
        tot = sum(conf[t].values())
        print(f"{t:<{w}}" + "".join(f"{conf[t][c]:>{w}}" for c in CLASSES) + f"{conf[t][t] / tot:>{w}.2f}")

    tp = sum(conf[t][c] for t in CLASSES if t in ALARM_CLASSES for c in CLASSES if c in ALARM_CLASSES)
    fn = sum(conf[t][c] for t in CLASSES if t in ALARM_CLASSES for c in CLASSES if c not in ALARM_CLASSES)
    fp = sum(conf[t][c] for t in CLASSES if t not in ALARM_CLASSES for c in CLASSES if c in ALARM_CLASSES)
    tn = sum(conf[t][c] for t in CLASSES if t not in ALARM_CLASSES for c in CLASSES if c not in ALARM_CLASSES)
    print(f"\nAllarme (classi {sorted(ALARM_CLASSES)}): rilevati {tp}, mancati {fn}, "
          f"falsi allarmi {fp}, corretti non-allarme {tn}")
    fa_rate = fp / (fp + tn)
    print(f"Falsi allarmi per blocco non di allarme: {fa_rate:.3f}")
    if args.triggers_per_hour:
        print(f"Falsi allarmi/ora con {args.triggers_per_hour:g} trigger/ora non di allarme: "
              f"{fa_rate * args.triggers_per_hour:.2f}")
    else:
        print("Falsi allarmi/ora: servono i trigger/ora non di allarme misurati in campo (--triggers-per-hour)")

    post_ms = (SAMPLES_PER_BLOCK - TRIGGER_INDEX) * SAMPLE_PERIOD_MS
    tx_ms = block_bytes * 10 / args.baud * 1000  # 8N1 = 10 bit per byte
    avg_proc = sum(proc_ms) / len(proc_ms)
    print(f"\nLatenza stimata dal trigger all'allarme: {post_ms + tx_ms + avg_proc:.0f} ms = "
          f"{post_ms:.0f} (campioni post-trigger) + {tx_ms:.0f} (trasmissione {block_bytes} byte a "
          f"{args.baud} baud) + {avg_proc:.2f} (elaborazione)")


if __name__ == "__main__":
    main()