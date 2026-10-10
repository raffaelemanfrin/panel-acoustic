"""Test del demone (Pipeline), senza seriale.  Esegui con:  python3 -m unittest -v testdaemon"""
import logging
import random
import unittest

from daemon import POST_TRIGGER_MS, Pipeline
from fakemcu import corrupt, gen_block
from protocol import END, START, BlockParser
from storage import AlarmStore

# log del demone silenziati durante i test (assertLogs li cattura comunque)
logging.getLogger("daemon").addHandler(logging.NullHandler())
logging.getLogger("daemon").propagate = False


def lines_for(cls, seed=1):
    rows = gen_block(cls, random.Random(seed))
    return [START] + [f"{i},{p},{l}" for i, p, l in rows] + [END]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(cooldown=10.0, **kw):
    clock = Clock()
    store = AlarmStore(":memory:")
    p = Pipeline(store, BlockParser(clock=clock), cooldown_s=cooldown, clock=clock, **kw)
    return p, store, clock


def feed(p, lines, clock=None, dt=0.0):
    out = []
    for l in lines:
        if clock is not None:
            clock.t += dt
        d = p.handle_line(l)
        if d is not None:
            out.append(d)
    return out


class PipelineTest(unittest.TestCase):
    def test_allarme_registrato(self):
        p, store, _ = make()
        dets = feed(p, lines_for("colpo"))
        self.assertEqual([d.cls for d in dets], ["colpo"])
        rows = store.last(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["cls"], rows[0]["source"]), ("colpo", "rules"))
        self.assertIsNotNone(rows[0]["peak"])

    def test_non_allarme_non_registrato(self):
        p, store, _ = make()
        feed(p, lines_for("calma") + lines_for("vento"))
        self.assertEqual(store.last(5), [])
        self.assertEqual(p.stats.blocks_ok, 2)

    def test_blocchi_rovinati_contati_e_superati(self):
        p, store, _ = make(cooldown=0)
        rng = random.Random(4)
        lines = []
        for k in range(6):
            blk = lines_for("grandine", seed=k)
            lines += corrupt(blk, rng) if k % 2 else blk
        feed(p, lines)
        self.assertGreaterEqual(p.stats.blocks_rejected, 2)  # "no_end" viene scartato al START seguente
        self.assertEqual(p.stats.blocks_ok + p.stats.blocks_rejected, 6)
        self.assertEqual(len(store.last(10)), p.stats.alarms_stored)

    def test_cooldown(self):
        p, store, clock = make(cooldown=10.0)
        feed(p, lines_for("grandine", 1))
        clock.t += 3
        feed(p, lines_for("grandine", 2))       # entro 10 s: soppresso
        feed(p, lines_for("colpo", 3))          # altra classe: registrato
        clock.t += 20
        feed(p, lines_for("grandine", 4))       # cooldown scaduto
        self.assertEqual([r["cls"] for r in store.last(10)], ["grandine", "colpo", "grandine"])
        self.assertEqual(p.stats.alarms_suppressed, 1)

    def test_latenza(self):
        p, store, clock = make()
        feed(p, lines_for("colpo"), clock, dt=0.0005)  # 502 righe * 0.5 ms
        lat = store.last(1)[0]["latency_ms"]
        self.assertAlmostEqual(lat, POST_TRIGGER_MS + 501 * 0.5, delta=1.0)

    def test_on_alarm_rotto_non_ferma(self):
        def boom(*_):
            raise RuntimeError("buzzer scollegato")
        p, store, _ = make(cooldown=0, on_alarm=boom)
        with self.assertLogs("daemon", level="ERROR"):
            feed(p, lines_for("colpo", 1) + lines_for("colpo", 2))
        self.assertEqual(len(store.last(5)), 2)

    def test_on_alarm_riceve_dati(self):
        got = []
        p, _, _ = make(on_alarm=lambda det, block, info: got.append((det.cls, len(block.piezo), info["peak"])))
        feed(p, lines_for("colpo"))
        self.assertEqual(got[0][:2], ("colpo", 500))

    def test_timeout_a_flusso_fermo(self):
        p, _, clock = make()
        feed(p, lines_for("colpo")[:200])
        clock.t += 6
        p.handle_line("")                       # la seriale restituisce "" allo scadere del timeout
        self.assertEqual(p.stats.blocks_rejected, 1)


if __name__ == "__main__":
    unittest.main()
