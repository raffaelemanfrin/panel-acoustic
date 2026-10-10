"""Rete Edge Impulse come classificatore del piezo (ingresso nn_alarm della logica).

Si esporta il modello da Edge Impulse come "Linux (AARCH64)" -> file .eim
(eseguibile). Questo modulo lo avvia e gli parla con lo stesso protocollo
dell'SDK ufficiale (JSON su socket Unix, risposta terminata da \\x00), senza
dipendenze da installare nel container di App Lab.

    from nn import NNClassifier
    clf = NNClassifier("modello.eim")
    det = clf(block)            # Detection come detector.classify: stessa firma
    clf.close()

Da riga di comando:
    python daemon.py --bridge --classifier nn --model modello.eim
    python evaluate.py --data data/testing --classifier nn --model modello.eim
    python nn.py modello.eim                    # stampa le info del modello

Il modello va addestrato sui blocchi grezzi (conteggi ADC, 12 bit) della
sola colonna piezo: la luce resta fuori dalla rete ed entra nella logica.
Se il modello ha 2 assi si passano piezo e luce alternati, nell'ordine dei CSV.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional

from detector import ALARM_CLASSES, Detection
from protocol import SAMPLES_PER_BLOCK, TRIGGER_INDEX, Block


class EimRunner:
    """Client minimo del runner .eim (stesso protocollo di edge_impulse_linux.runner)."""

    def __init__(self, model_path: str, timeout: float = 10.0):
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"modello non trovato: {model_path}")
        if not os.access(model_path, os.X_OK):
            raise PermissionError(f"il modello non e' eseguibile: chmod +x {model_path}")
        self._dir = tempfile.mkdtemp()
        sock_path = os.path.join(self._dir, "runner.sock")
        self._proc = subprocess.Popen([model_path, sock_path],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while not os.path.exists(sock_path):
            if self._proc.poll() is not None:
                self.close()
                raise RuntimeError(f"il modello si e' chiuso all'avvio (codice {self._proc.returncode}): "
                                   "e' l'export per Linux AARCH64?")
            if time.monotonic() > deadline:
                self.close()
                raise TimeoutError("il modello non ha aperto il socket")
            time.sleep(0.05)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(sock_path)
        self._id = 0
        self.info = self.send({"hello": 1})

    def send(self, msg: dict) -> dict:
        self._id += 1
        msg = dict(msg, id=self._id)
        self._sock.sendall(json.dumps(msg).encode())
        data = b""
        while not data.endswith(b"\x00"):
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("il modello ha chiuso la connessione")
            data += chunk
        resp = json.loads(data[:-1].decode())
        if resp.get("id") != self._id:
            raise RuntimeError(f"risposta con id {resp.get('id')}, atteso {self._id}")
        if not resp.get("success", False):
            raise RuntimeError(resp.get("error", "errore del modello"))
        return resp

    def classify(self, features: List[float]) -> dict:
        return self.send({"classify": features})

    def close(self) -> None:
        if getattr(self, "_sock", None) is not None:
            self._sock.close()
            self._sock = None
        if getattr(self, "_proc", None) is not None and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if getattr(self, "_dir", None):
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None


class NNClassifier:
    """Block -> Detection, come detector.classify. confidence = probabilita' della classe scelta.

    Sotto min_confidence la classe diventa "incerto" (mai allarme): meglio un
    allarme mancato che una sirena per un evento che la rete non conosce.
    """

    def __init__(self, model_path: str, min_confidence: float = 0.6):
        self.runner = EimRunner(model_path)
        params = self.runner.info.get("model_parameters", {})
        self.labels: List[str] = list(params.get("labels", []))
        self.axes = int(params.get("axis_count", 1)) or 1
        self.n_features = int(params.get("input_features_count", SAMPLES_PER_BLOCK))
        self.frequency = params.get("frequency")
        self.min_confidence = min_confidence
        if self.n_features % self.axes:
            raise ValueError(f"input_features_count {self.n_features} non divisibile per {self.axes} assi")
        self.samples = self.n_features // self.axes
        if self.samples > SAMPLES_PER_BLOCK:
            raise ValueError(f"il modello vuole {self.samples} campioni, il blocco ne ha {SAMPLES_PER_BLOCK}")
        if self.frequency and abs(float(self.frequency) - 2000) > 1:
            print(f"ATTENZIONE: modello a {self.frequency} Hz, i blocchi sono a 2000 Hz", file=sys.stderr)
        # se la finestra del modello e' piu' corta del blocco, si tiene la stessa
        # proporzione pre/post trigger del blocco (100/500)
        pre = round(self.samples * TRIGGER_INDEX / SAMPLES_PER_BLOCK)
        self.start = max(0, min(TRIGGER_INDEX - pre, SAMPLES_PER_BLOCK - self.samples))

    def features(self, block: Block) -> List[float]:
        sl = slice(self.start, self.start + self.samples)
        if self.axes == 1:
            return [float(v) for v in block.piezo[sl]]
        if self.axes == 2:
            out: List[float] = []
            for p, l in zip(block.piezo[sl], block.luce[sl]):
                out += [float(p), float(l)]
            return out
        raise ValueError(f"modello con {self.axes} assi: attesi 1 (piezo) o 2 (piezo, luce)")

    def __call__(self, block: Block) -> Detection:
        res = self.runner.classify(self.features(block))
        probs: Dict[str, float] = res.get("result", {}).get("classification", {})
        if not probs:
            return Detection("incerto", False, "rete: nessuna classificazione", {}, None)
        cls, p = max(probs.items(), key=lambda kv: kv[1])
        rule = f"rete p={p:.2f}"
        if p < self.min_confidence:
            cls, rule = "incerto", f"rete incerta ({cls} p={p:.2f})"
        return Detection(cls, cls in ALARM_CLASSES, rule, dict(probs), p)

    def close(self) -> None:
        self.runner.close()


def build_classifier(kind: str, th, model: Optional[str] = None, min_confidence: float = 0.6):
    """("rules"|"nn") -> (classificatore, nome sorgente per il registro, funzione di chiusura)."""
    if kind == "rules":
        from detector import classify
        return (lambda b: classify(b, th)), "rules", (lambda: None)
    if kind == "nn":
        if not model:
            raise SystemExit("--classifier nn richiede --model modello.eim")
        clf = NNClassifier(model, min_confidence)
        return clf, "nn", clf.close
    raise SystemExit(f"classificatore sconosciuto: {kind}")


def add_arguments(p) -> None:
    g = p.add_argument_group("classificatore del piezo")
    g.add_argument("--classifier", choices=["rules", "nn"], default="rules",
                   help="rules = regole a soglia (piano B); nn = rete Edge Impulse (.eim)")
    g.add_argument("--model", help="modello Edge Impulse esportato per Linux AARCH64 (.eim)")
    g.add_argument("--min-conf", type=float, default=0.6,
                   help="(nn) sotto questa probabilita' la classe e' 'incerto', mai allarme")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("uso: python nn.py modello.eim")
    clf = NNClassifier(argv[0])
    try:
        p = clf.runner.info.get("model_parameters", {})
        print(f"progetto: {clf.runner.info.get('project', {}).get('name', '?')}")
        print(f"classi: {clf.labels}")
        print(f"assi: {clf.axes}, campioni: {clf.samples} (dal {clf.start} del blocco), "
              f"frequenza: {p.get('frequency')} Hz")
        missing = sorted(ALARM_CLASSES - set(clf.labels))
        if missing:
            print(f"ATTENZIONE: classi di allarme assenti dal modello: {missing}")
    finally:
        clf.close()


if __name__ == "__main__":
    main()
