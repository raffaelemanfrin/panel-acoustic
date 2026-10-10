#!/usr/bin/env python3
"""Cattura eventi dalla MCU e li salva come CSV pronti per Edge Impulse.

Esempi:
    python capture.py --port /dev/ttyMCU --label grandine --category training --n 30
    python capture.py --port /dev/ttyMCU --label grandine --category testing --n 10
    python fake_mcu.py --n 5 | python capture.py --input - --label colpo   # senza scheda

Output: data/<category>/<label>.NN.csv con colonne timestamp(ms),piezo,luce
Upload: edge-impulse-uploader --category training data/training/*.csv
"""
import argparse
import sys
from pathlib import Path

from protocol import SAMPLES_PER_BLOCK, Block, BlockParser, Rejected
from source import open_lines


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--port", help="porta seriale lato Linux")
    src.add_argument("--input", help="file con il flusso registrato ('-' = stdin)")
    p.add_argument("--baud", type=int, default=230400)
    p.add_argument("--label", required=True, help="classe (senza punti): calma, vento, grandine, ...")
    p.add_argument("--category", choices=["training", "testing"], default="training")
    p.add_argument("--out", default="data", help="cartella base di output")
    p.add_argument("--samples", type=int, default=SAMPLES_PER_BLOCK)
    p.add_argument("--time-col", choices=["index", "micros"], default="index")
    p.add_argument("--adc-bits", type=int, default=0, help="bit ADC del piezo (controllo di range)")
    p.add_argument("--n", type=int, default=0, help="stop dopo N eventi validi (0 = fino a Ctrl+C / fine file)")
    p.add_argument("--block-timeout", type=float, default=5.0)
    return p.parse_args()


def next_number(folder: Path, label: str) -> int:
    used = []
    for f in folder.glob(f"{label}.*.csv"):
        part = f.stem.split(".")[1]
        if part.isdigit():
            used.append(int(part))
    return max(used, default=0) + 1


def save(block: Block, folder: Path, label: str) -> Path:
    path = folder / f"{label}.{next_number(folder, label):02d}.csv"
    with open(path, "w") as f:
        f.write("timestamp,piezo,luce\n")
        for t, p, l in zip(block.t_ms, block.piezo, block.luce):
            f.write(f"{t:g},{p},{l}\n")
    return path


def main():
    args = parse_args()
    if "." in args.label:
        sys.exit("La label non deve contenere punti (Edge Impulse la ricava dal nome file prima del primo punto).")

    folder = Path(args.out) / args.category
    folder.mkdir(parents=True, exist_ok=True)

    parser = BlockParser(args.samples, args.time_col, args.adc_bits, args.block_timeout)
    saved = 0
    origin = args.port or args.input
    print(f"In ascolto su {origin} -> {folder}/{args.label}.NN.csv (Ctrl+C per fermare)")

    try:
        for line in open_lines(args.port, args.input, args.baud):
            result = parser.feed(line)
            if isinstance(result, Rejected):
                print(f"  ! blocco scartato: {result.reason}")
            elif isinstance(result, Block):
                path = save(result, folder, args.label)
                saved += 1
                print(f"  ok {path.name}  (picco piezo {max(result.piezo)})  [{saved} salvati]")
                if args.n and saved >= args.n:
                    break
    except KeyboardInterrupt:
        pass
    except OSError as e:  # include serial.SerialException
        sys.exit(f"Errore di lettura da {origin}: {e}")
    print(f"Fine: {saved} eventi salvati in {folder}")


if __name__ == "__main__":
    main()