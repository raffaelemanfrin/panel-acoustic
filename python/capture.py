#!/usr/bin/env python3
"""Cattura eventi dalla MCU e li salva come CSV pronti per Edge Impulse.

Esempi:
    python capture.py --bridge --label grandine --category training --n 30   # UNO Q
    python capture.py --bridge --label calma --force 30      # blocchi forzati: calma, vento
    python capture.py --port /dev/ttyMCU --label grandine --category testing --n 10
    python fake_mcu.py --n 5 | python capture.py --input - --label colpo   # senza scheda

Output: data/<category>/<label>.NN.csv con colonne timestamp(ms),piezo,luce
Upload: edge-impulse-uploader --category training data/training/*.csv

Con --force si salvano SOLO i blocchi forzati (preceduti da "#FORCED"): un
evento vero che scatta durante una raccolta di "calma" non finisce nella
classe sbagliata. Senza --force, al contrario, i blocchi forzati si scartano.
Dentro App Lab la raccolta si pilota con raccolta.json (vedi raccolta.py).
"""
import argparse
import sys
from pathlib import Path

from protocol import SAMPLES_PER_BLOCK, Block, BlockParser, Rejected
from source import open_lines


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--port", help="porta seriale lato Linux")
    src.add_argument("--input", help="file con il flusso registrato ('-' = stdin)")
    src.add_argument("--bridge", action="store_true", help="UNO Q: blocchi via arduino-router")
    p.add_argument("--baud", type=int, default=230400)
    p.add_argument("--force", type=int, default=0,
                   help="(--bridge) chiede alla MCU N blocchi senza trigger, uno al secondo: per calma e vento")
    p.add_argument("--label", required=True, help="classe (senza punti): calma, vento, grandine, ...")
    p.add_argument("--category", choices=["training", "testing"], default="training")
    p.add_argument("--out", default="data", help="cartella base di output")
    p.add_argument("--samples", type=int, default=SAMPLES_PER_BLOCK)
    p.add_argument("--time-col", choices=["index", "micros"], default="index")
    p.add_argument("--adc-bits", type=int, default=12, help="bit ADC del piezo (controllo di range, 0 = nessuno)")
    p.add_argument("--n", type=int, default=0, help="stop dopo N eventi validi (0 = fino a Ctrl+C / fine file)")
    p.add_argument("--block-timeout", type=float, default=5.0)
    return p.parse_args(argv)


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


FORCED_MARK = "#FORCED"


class Session:
    """Una raccolta: riga per riga, salva i blocchi validi come CSV finche' non arriva a n.

    forced_only=True  -> tiene solo i blocchi forzati (calma, vento)
    forced_only=False -> tiene solo i blocchi da trigger vero
    forced_only=None  -> tiene tutto (file registrati, generatore finto)
    """

    def __init__(self, label: str, category: str = "training", out="data", n: int = 0,
                 forced_only=None, parser: BlockParser = None, log=print):
        if not label or "." in label:
            raise ValueError("label vuota o con punti (Edge Impulse la ricava dal nome file prima del primo punto)")
        self.label = label
        self.folder = Path(out) / category
        self.folder.mkdir(parents=True, exist_ok=True)
        self.n = n
        self.forced_only = forced_only
        self.parser = parser or BlockParser()
        self.log = log
        self.saved = 0
        self.skipped = 0
        self._forced_next = False

    @property
    def done(self) -> bool:
        return bool(self.n) and self.saved >= self.n

    def feed(self, line: str) -> bool:
        """True quando la sessione ha raggiunto n blocchi."""
        if line.strip() == FORCED_MARK:
            self._forced_next = True
            return False
        result = self.parser.feed(line)
        if isinstance(result, Rejected):
            self.log(f"  ! blocco scartato: {result.reason}")
            self._forced_next = False
        elif isinstance(result, Block):
            forced, self._forced_next = self._forced_next, False
            if self.forced_only is not None and forced != self.forced_only:
                self.skipped += 1
                what = "da trigger" if self.forced_only else "forzato"
                self.log(f"  - blocco {what} ignorato in questa raccolta")
                return False
            path = save(result, self.folder, self.label)
            self.saved += 1
            self.log(f"  ok {path.name}  (picco piezo {max(result.piezo)})  "
                     f"[{self.saved}{'/' + str(self.n) if self.n else ''}]")
        return self.done


def main(argv=None):
    args = parse_args(argv)
    if args.force and not args.bridge:
        sys.exit("--force funziona solo con --bridge (lo chiede alla MCU)")
    if args.force and not args.n:
        args.n = args.force
    parser = BlockParser(args.samples, args.time_col, args.adc_bits, args.block_timeout)
    forced_only = (args.force > 0) if args.bridge else None
    try:
        session = Session(args.label, args.category, args.out, args.n, forced_only, parser)
    except ValueError as e:
        sys.exit(str(e))
    origin = args.port or args.input or "Bridge"
    print(f"In ascolto su {origin} -> {session.folder}/{args.label}.NN.csv (Ctrl+C per fermare)")

    try:
        for line in open_lines(args.port, args.input, args.baud, bridge=args.bridge, force=args.force):
            if session.feed(line):
                break
    except KeyboardInterrupt:
        pass
    except OSError as e:  # include serial.SerialException
        sys.exit(f"Errore di lettura da {origin}: {e}")
    print(f"Fine: {session.saved} eventi salvati in {session.folder}")


if __name__ == "__main__":
    main()