"""Test di detector.py e storage.py.  Esegui con:  python3 -m unittest -v testpersona3"""
import random
import time
import unittest

from detector import ALARM_CLASSES, classify
from fakemcu import CLASSES, gen_block
from protocol import SAMPLE_PERIOD_MS, Block
from storage import AlarmStore


def make_block(cls, seed, adc_bits=12):
    rows = gen_block(cls, random.Random(seed), adc_bits)
    return Block([i * SAMPLE_PERIOD_MS for i in range(len(rows))], [r[1] for r in rows], [r[2] for r in rows])


class DetectorTest(unittest.TestCase):
    def test_classi_riconosciute(self):
        # soglie tarate sui blocchi finti: tollera qualche errore occasionale
        for cls in CLASSES:
            ok = sum(classify(make_block(cls, s)).cls == cls for s in range(50))
            self.assertGreaterEqual(ok, 45, f"{cls}: {ok}/50")

    def test_flag_allarme(self):
        for cls in CLASSES:
            self.assertEqual(classify(make_block(cls, 3)).alarm, cls in ALARM_CLASSES)

    def test_feature_luce_esposte(self):
        f = classify(make_block("disturbo", 1)).features
        self.assertTrue(f["light_fast"])
        self.assertFalse(classify(make_block("colpo", 1)).features["light_fast"])


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.db = AlarmStore(":memory:")

    def test_inserimento_e_ultimi(self):
        self.db.add("grandine", "rules", peak=800, latency_ms=700, detail="urti multipli")
        self.db.add("colpo", "nn", confidence=0.9)
        last = self.db.last(5)
        self.assertEqual([r["cls"] for r in last], ["colpo", "grandine"])  # dal piu' recente
        self.assertEqual(last[1]["peak"], 800)

    def test_finestra_temporale_e_conteggi(self):
        self.db.add("colpo", "rules", ts=time.time() - 7200)   # 2 ore fa
        self.db.add("grandine", "rules")
        self.db.add("grandine", "rules")
        self.assertEqual(len(self.db.since(3600)), 2)
        self.assertEqual(self.db.count_by_class(), {"colpo": 1, "grandine": 2})
        self.assertEqual(self.db.count_by_class(since_seconds=3600), {"grandine": 2})


if __name__ == "__main__":
    unittest.main()