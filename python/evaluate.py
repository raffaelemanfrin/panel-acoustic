#!/usr/bin/env python3
"""Valuta classificatore + logica su blocchi etichettati.

Sorgenti (una delle due):
    --data   cartelle o file CSV nel formato di capture.py
             (<label>.NN.csv, colonne timestamp,piezo,luce; label = nome prima del primo punto)
    --fake   blocchi generati da fakemcu.py (solo per provare la pipeline)

    python evaluate.py --data data/testing
    python evaluate.py --data data/testing data/training/colpo.03.csv --baud 230400
    python evaluate.py --fake --n 200 --seed 1 --shadow-rate 0.3
    python evaluate.py --data data/testing --triggers-per-hour 40 --shadow-alarm

Stampa:
  - matrice di confusione del classificatore del piezo (= ingresso nn_alarm);
  - esito allarme si/no DOPO la logica (logic.py, con light_fast / light_slow):
    rilevati, mancati, falsi allarmi, e gli avvisi per classe vera;
  - falsi allarmi/ora (con --triggers-per-hour) e latenza stimata.

Con dati finti il risultato verifica la pipeline, NON le prestazioni vere.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Tuple

from detector import ALARM_CLASSES, Detection, Thresholds, classify, light_flags
from logic import Logic
from protocol import END, SAMPLE_PERIOD_MS, SAMPLES_PER_BLOCK, START, TRIGGER_INDEX, Block

# Ordine di stampa delle classi note; quelle nuove trovate nei dati vanno in coda.
KNOWN = ["calma", "vento", "disturbo", "pioggia", "grandine", "colpo"]

Labeled = Tuple[str, Block, str]  # (classe vera, blocco, origine)


# ------------------------------------------------------------------ sorgenti
def csv_files(paths: List[str]) -> List[Path]:
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(p.glob("*.csv"))
        elif p.suffix == ".csv" and p.exists():
            files.append(p)
        else:
            print(f"  ! {p}: non e' una cartella ne' un .csv, ignorato", file=sys.stderr)
    return files


def load_csv(path: Path, samples: int = SAMPLES_PER_BLOCK, adc_bits: int = 0) -> Block:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or not {"piezo", "luce"} <= set(rows[0]):
        raise ValueError("servono le colonne piezo e luce")
    if len(rows) != samples:
        raise ValueError(f"{len(rows)} righe invece di {samples}")
    piezo = [int(float(r["piezo"])) for r in rows]
    luce = [int(float(r["luce"])) for r in rows]
    if adc_bits and any(v < 0 or v >= 2 ** adc_bits for v in piezo):
        raise ValueError(f"piezo fuori range 0-{2 ** adc_bits - 1}")
    return Block([i * SAMPLE_PERIOD_MS for i in range(samples)], piezo, luce)


def from_csv(paths: List[str], adc_bits: int) -> Iterator[Labeled]:
    files = csv_files(paths)
    if not files:
        sys.exit("Nessun CSV trovato. Registra con capture.py (data/<training|testing>/<label>.NN.csv).")
    for f in files:
        label = f.name.split(".")[0]
        try:
            yield label, load_csv(f, adc_bits=adc_bits), f.name
        except (ValueError, KeyError) as e:
            print(f"  ! {f.name} scartato: {e}", file=sys.stderr)


def from_fake(n: int, seed: int, adc_bits: int, shadow_rate: float) -> Iterator[Labeled]:
    from fakemcu import CLASSES, gen_block  # import solo qui: --data non dipende dal generatore

    rng = random.Random(seed)
    for truth in CLASSES:
        for k in range(n):
            shadow = rng.random() < shadow_rate
            rows = gen_block(truth, rng, adc_bits, shadow=shadow)
            block = Block([i * SAMPLE_PERIOD_MS for i in range(len(rows))],
                          [r[1] for r in rows], [r[2] for r in rows])
            yield truth, block, f"fake {truth} #{k}{' +ombra' if shadow else ''}"


def block_bytes(block: Block) -> int:
    """Byte trasmessi per questo blocco nel protocollo seriale."""
    lines = [START] + [f"{i},{p},{l}" for i, (p, l) in enumerate(zip(block.piezo, block.luce))] + [END]
    return len("\n".join(lines)) + 1


# ------------------------------------------------------------------ valutazione
def evaluate(samples: Iterator[Labeled], classifier: Callable[[Block], Detection],
             light: Callable[[Block], Tuple[bool, bool]], logic: Logic, verbose: bool = False):
    conf: Dict[str, Counter] = defaultdict(Counter)
    alarm = Counter()          # tp/fn/fp/tn sull'allarme dopo la logica
    nn_only = Counter()        # stesso conto usando solo nn_alarm (per vedere cosa cambia la logica)
    warnings = Counter()
    proc_ms, sizes = [], []

    for truth, block, origin in samples:
        t0 = time.perf_counter()
        det = classifier(block)
        nn_alarm = det.cls in ALARM_CLASSES
        fast, slow = light(block)
        dec = logic.decide(nn_alarm, fast, slow)
        proc_ms.append((time.perf_counter() - t0) * 1000)
        sizes.append(block_bytes(block))

        conf[truth][det.cls] += 1
        want = truth in ALARM_CLASSES
        alarm[("t" if dec.alarm == want else "f") + ("p" if dec.alarm else "n")] += 1
        nn_only[("t" if nn_alarm == want else "f") + ("p" if nn_alarm else "n")] += 1
        warnings[truth] += dec.warning
        if verbose and (det.cls != truth or dec.alarm != want):
            print(f"  {origin}: previsto {det.cls}, {dec.describe()}")
    return conf, alarm, nn_only, warnings, proc_ms, sizes


def print_report(conf, alarm, nn_only, warnings, proc_ms, sizes, args):
    if not proc_ms:
        sys.exit("Nessun blocco valido da valutare.")
    seen = set(conf) | {c for row in conf.values() for c in row}
    classes = [c for c in KNOWN if c in seen] + sorted(seen - set(KNOWN))
    w = max(9, max(len(c) for c in classes) + 1)

    print(f"\n{len(proc_ms)} blocchi. Matrice di confusione del piezo "
          f"(righe = classe vera, colonne = prevista)")
    print(" " * w + "".join(f"{c:>{w}}" for c in classes) + f"{'recall':>{w}}")
    for t in classes:
        tot = sum(conf[t].values())
        if not tot:
            continue
        print(f"{t:<{w}}" + "".join(f"{conf[t][c]:>{w}}" for c in classes) + f"{conf[t][t] / tot:>{w}.2f}")

    def line(c, name):
        return (f"{name}: rilevati {c['tp']}, mancati {c['fn']}, falsi allarmi {c['fp']}, "
                f"corretti non-allarme {c['tn']}")
    print(f"\nAllarme vero = classe in {sorted(ALARM_CLASSES)}; "
          f"ombra senza urto = {'allarme' if args.shadow_alarm else 'no'}")
    print(line(nn_only, "Solo piezo (nn_alarm)"))
    print(line(alarm, "Dopo la logica (Y)   "))
    w_txt = ", ".join(f"{k}={v}" for k, v in warnings.items() if v) or "nessuno"
    print(f"Avvisi (W) per classe vera: {w_txt}")

    neg = alarm["fp"] + alarm["tn"]
    if neg:
        fa_rate = alarm["fp"] / neg
        print(f"\nFalsi allarmi per blocco non di allarme: {fa_rate:.3f}")
        if args.triggers_per_hour:
            print(f"Falsi allarmi/ora con {args.triggers_per_hour:g} trigger/ora non di allarme: "
                  f"{fa_rate * args.triggers_per_hour:.2f}")
        else:
            print("Falsi allarmi/ora: servono i trigger/ora non di allarme misurati in campo (--triggers-per-hour)")

    post_ms = (SAMPLES_PER_BLOCK - TRIGGER_INDEX) * SAMPLE_PERIOD_MS
    size = sum(sizes) / len(sizes)
    tx_ms = size * 10 / args.baud * 1000  # 8N1 = 10 bit per byte
    avg_proc = sum(proc_ms) / len(proc_ms)
    print(f"\nLatenza stimata dal trigger all'allarme: {post_ms + tx_ms + avg_proc:.0f} ms = "
          f"{post_ms:.0f} (campioni post-trigger) + {tx_ms:.0f} (trasmissione ~{size:.0f} byte a "
          f"{args.baud} baud) + {avg_proc:.2f} (classificatore + logica)")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", nargs="+", metavar="PATH", help="cartelle o file CSV etichettati")
    src.add_argument("--fake", action="store_true", help="usa i blocchi finti di fakemcu.py")
    p.add_argument("--n", type=int, default=200, help="(--fake) blocchi per classe")
    p.add_argument("--seed", type=int, default=1, help="(--fake) seme")
    p.add_argument("--shadow-rate", type=float, default=0.0,
                   help="(--fake) frazione di blocchi con un'ombra rapida aggiunta (mano sul vetro)")
    p.add_argument("--adc-bits", type=int, default=10)
    p.add_argument("--baud", type=int, default=230400)
    p.add_argument("--shadow-alarm", action="store_true", help="ombra senza urto = allarme")
    p.add_argument("--triggers-per-hour", type=float, default=None,
                   help="trigger/ora NON di allarme in campo (da misurare); abilita i falsi allarmi/ora")
    p.add_argument("-v", "--verbose", action="store_true", help="elenca i blocchi sbagliati")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    th = Thresholds(adc_bits=args.adc_bits)
    # Quando arriva la rete: un classificatore con la stessa firma (Block -> Detection)
    classifier = lambda b: classify(b, th)  # noqa: E731
    samples = (from_csv(args.data, args.adc_bits) if args.data
               else from_fake(args.n, args.seed, args.adc_bits, args.shadow_rate))
    result = evaluate(samples, classifier, lambda b: light_flags(b, th), Logic(args.shadow_alarm),
                      args.verbose)
    print_report(*result, args)


if __name__ == "__main__":
    main()
