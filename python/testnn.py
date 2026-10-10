"""Test di nn.py (con un finto modello .eim), della raccolta e di capture.Session.
Esegui con:  python3 -m unittest -v testnn"""
import json
import logging
import os
import random
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from capture import Session
from fakemcu import gen_block
from nn import NNClassifier
from protocol import END, SAMPLE_PERIOD_MS, START, Block
from raccolta import Collector

logging.getLogger("raccolta").propagate = False

# Finto runner .eim: stesso protocollo di Edge Impulse (JSON su socket Unix,
# risposta terminata da \x00). Classifica "colpo" se il picco supera 150.
FAKE_EIM = r'''#!PYTHON
import json, os, socket, sys
path = sys.argv[1]
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path); srv.listen(1)
conn, _ = srv.accept()
dec = json.JSONDecoder(); buf = ""
labels = ["calma", "colpo", "grandine", "vento"]
while True:
    data = conn.recv(65536)
    if not data:
        break
    buf += data.decode()
    while buf:
        try:
            msg, end = dec.raw_decode(buf)
        except ValueError:
            break
        buf = buf[end:].lstrip()
        if "hello" in msg:
            resp = {"model_parameters": {"labels": labels, "axis_count": 1,
                    "input_features_count": NFEAT, "frequency": 2000}, "project": {"name": "finto"}}
        else:
            x = msg["classify"]
            base = sorted(x)[len(x) // 2]
            peak = max(abs(v - base) for v in x)
            hit = peak > 150
            resp = {"result": {"classification": {
                "calma": 0.05 if hit else PCALMA, "colpo": 0.9 if hit else 0.05,
                "grandine": 0.03, "vento": 0.02}}, "len": len(x)}
        resp.update(id=msg["id"], success=True)
        conn.sendall(json.dumps(resp).encode() + b"\x00")
'''


def make_eim(folder, nfeat=500, pcalma=0.9):
    p = Path(folder) / "finto.eim"
    p.write_text(FAKE_EIM.replace("PYTHON", sys.executable).replace("NFEAT", str(nfeat))
                 .replace("PCALMA", str(pcalma)))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def block(cls, seed=1):
    rows = gen_block(cls, random.Random(seed))
    return Block([i * SAMPLE_PERIOD_MS for i in range(len(rows))], [r[1] for r in rows], [r[2] for r in rows])


def lines(cls, seed=1, forced=False):
    rows = gen_block(cls, random.Random(seed))
    return (["#FORCED"] if forced else []) + [START] + [f"{i},{p},{l}" for i, p, l in rows] + [END]


class NNTest(unittest.TestCase):
    def test_classifica_come_le_regole(self):
        with tempfile.TemporaryDirectory() as d:
            clf = NNClassifier(make_eim(d))
            try:
                self.assertEqual(clf.labels, ["calma", "colpo", "grandine", "vento"])
                det = clf(block("colpo"))
                self.assertEqual((det.cls, det.alarm), ("colpo", True))
                self.assertAlmostEqual(det.confidence, 0.9)
                self.assertEqual(clf(block("calma")).cls, "calma")
            finally:
                clf.close()

    def test_incerto_sotto_soglia(self):
        with tempfile.TemporaryDirectory() as d:
            clf = NNClassifier(make_eim(d, pcalma=0.4), min_confidence=0.6)
            try:
                det = clf(block("calma"))
                self.assertEqual(det.cls, "incerto")
                self.assertFalse(det.alarm)
            finally:
                clf.close()

    def test_finestra_piu_corta_mantiene_il_trigger(self):
        with tempfile.TemporaryDirectory() as d:
            clf = NNClassifier(make_eim(d, nfeat=400))
            try:
                self.assertEqual(clf.samples, 400)
                self.assertTrue(clf.start <= 100 < clf.start + 400)
                self.assertEqual(len(clf.features(block("colpo"))), 400)
            finally:
                clf.close()

    def test_modello_non_eseguibile(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.eim"
            p.write_text("x")
            with self.assertRaises(PermissionError):
                NNClassifier(str(p))

    def test_demone_con_la_rete(self):
        from daemon import Pipeline
        from storage import AlarmStore
        with tempfile.TemporaryDirectory() as d:
            clf = NNClassifier(make_eim(d))
            try:
                store = AlarmStore(":memory:")
                p = Pipeline(store, classifier=clf, source_name="nn")
                for l in lines("colpo"):
                    p.handle_line(l)
                row = store.last(1)[0]
                self.assertEqual((row["cls"], row["source"]), ("colpo", "nn"))
                self.assertAlmostEqual(row["confidence"], 0.9)
            finally:
                clf.close()


class SessionTest(unittest.TestCase):
    def test_force_tiene_solo_i_forzati(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session("calma", out=d, n=2, forced_only=True, log=lambda *_: None)
            for l in lines("calma", 1, forced=True) + lines("colpo", 2) + lines("calma", 3, forced=True):
                s.feed(l)
            self.assertEqual((s.saved, s.skipped, s.done), (2, 1, True))

    def test_trigger_scarta_i_forzati(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session("colpo", out=d, n=0, forced_only=False, log=lambda *_: None)
            for l in lines("colpo", 1) + lines("calma", 2, forced=True):
                s.feed(l)
            self.assertEqual((s.saved, s.skipped), (1, 1))


class CollectorTest(unittest.TestCase):
    def test_sessioni_da_raccolta_json(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            forced = []
            c = Collector(config=d / "raccolta.json", out=d / "data", status=d / "stato.txt",
                          request_force=forced.append, adc_bits=10)
            for l in lines("colpo", 1):             # nessuna configurazione: ignorato
                c.feed(l)
            self.assertFalse((d / "data").exists())

            (d / "raccolta.json").write_text(json.dumps({"label": "colpo", "n": 2}))
            c._last_check = 0
            for k in range(3):
                for l in lines("colpo", k):
                    c.feed(l)
            self.assertEqual(len(list((d / "data" / "training").glob("colpo.*.csv"))), 2)
            self.assertIn("completata", (d / "stato.txt").read_text())

            time.sleep(0.01)
            (d / "raccolta.json").write_text(json.dumps({"label": "calma", "n": 1, "force": True,
                                                         "category": "testing"}))
            os.utime(d / "raccolta.json", (time.time() + 5, time.time() + 5))
            c._last_check = 0
            for l in lines("calma", 7, forced=True):
                c.feed(l)
            self.assertEqual(forced, [1])
            self.assertEqual(len(list((d / "data" / "testing").glob("calma.*.csv"))), 1)

    def test_configurazione_sbagliata(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "raccolta.json").write_text('{"label": "con.punto"}')
            c = Collector(config=d / "raccolta.json", out=d / "data", status=d / "stato.txt")
            c.check_config()
            self.assertIsNone(c.session)
            self.assertIn("rifiutata", (d / "stato.txt").read_text())


if __name__ == "__main__":
    unittest.main()
