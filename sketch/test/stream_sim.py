#!/usr/bin/env python3
"""Flusso continuo finto per capture_sim: "piezo,luce" a 2000 Hz, ADC 12 bit.

Piezo collegato direttamente all'ADC (a riposo ~0, solo semionde positive) con rumore, vento a tratti e, ogni tanto, un
evento (colpo, grandine, ombra+colpo). Gli istanti veri degli eventi vanno su
stderr come "# evento <campione> <classe>", per controllare l'allineamento.
    python3 stream_sim.py --seconds 30 | ./capture_sim | python3 ../../python/daemon.py --input -
"""
import argparse
import math
import random
import sys

FS = 2000


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--every", type=float, default=1.5, help="secondi tra due eventi")
    a = p.parse_args()
    rng = random.Random(a.seed)
    n = int(a.seconds * FS)
    k = 4.0  # 12 bit: ampiezze dei dati finti (scala 10 bit) x4; piezo non polarizzato

    events = []
    t = int(2.0 * FS)  # 1 s di riscaldamento del trigger + margine
    while t < n - FS:
        cls = rng.choice(["colpo", "grandine", "colpo_ombra"])
        events.append((t, cls))
        print(f"# evento {t} {cls}", file=sys.stderr)
        t += int(a.every * FS * rng.uniform(0.8, 1.2))

    hits = []      # (inizio, ampiezza, decadimento ms, f Hz)
    shadows = []   # (inizio, fine)
    for t0, cls in events:
        if cls.startswith("colpo"):
            hits.append((t0, rng.uniform(300, 450), 15, rng.uniform(100, 200)))
        else:
            hits.append((t0, rng.uniform(150, 350), 6, rng.uniform(300, 600)))
            hits += [(t0 + rng.randint(20, 400), rng.uniform(100, 300), 6, rng.uniform(300, 600))
                     for _ in range(rng.randint(3, 7))]
        if cls == "colpo_ombra":
            shadows.append((t0 - 20, t0 + 600))

    out = sys.stdout
    light0 = 480  # partitore 100k/10k sul pannello
    for i in range(n):
        v = rng.gauss(0, 2)
        for (t0, amp, dec, f) in hits:
            if t0 <= i < t0 + 400:
                tt = (i - t0) / FS * 1000
                v += amp * math.exp(-tt / dec) * math.sin(2 * math.pi * f * tt / 1000)
        light = light0 + rng.gauss(0, 1.5)
        if any(s <= i < e for s, e in shadows):
            light -= 90
        pz = int(round(min(max(v * k, 0), 4095)))
        out.write(f"{pz},{int(round(light))}\n")


if __name__ == "__main__":
    main()
