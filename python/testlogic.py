"""Test di logic.py, delle novita' della Pipeline e di evaluate.py.
Esegui con:  python3 -m unittest -v testlogic"""
import logging
import random
import tempfile
import unittest
from pathlib import Path

from daemon import Pipeline
from detector import classify, light_flags
from evaluate import evaluate, from_csv
from fakemcu import gen_block
from logic import Logic, alarm_table, minimal_sop, sop_to_text
from outputs import MediaCarrierOutput, make_siren_wav
from protocol import END, SAMPLE_PERIOD_MS, START, Block, BlockParser
from storage import AlarmStore

logging.getLogger("daemon").addHandler(logging.NullHandler())
logging.getLogger("daemon").propagate = False
logging.getLogger("outputs").propagate = False


def block(cls, seed=1, shadow=False):
    rows = gen_block(cls, random.Random(seed), shadow=shadow)
    return Block([i * SAMPLE_PERIOD_MS for i in range(len(rows))], [r[1] for r in rows], [r[2] for r in rows])


def lines(cls, seed=1, shadow=False):
    rows = gen_block(cls, random.Random(seed), shadow=shadow)
    return [START] + [f"{i},{p},{l}" for i, p, l in rows] + [END]


class LogicTest(unittest.TestCase):
    def test_decisioni_del_team(self):
        L = Logic()
        self.assertTrue(L.decide(True, False, False).alarm)     # colpo senza ombra: allarme
        self.assertTrue(L.decide(True, True, False).alarm)      # mano e colpo: allarme
        d = L.decide(False, False, True)                         # neve/sporco: solo avviso
        self.assertFalse(d.alarm)
        self.assertTrue(d.warning)
        self.assertFalse(L.decide(True, False, True).warning)   # con allarme niente avviso

    def test_forma_minima(self):
        self.assertEqual(sop_to_text(minimal_sop(alarm_table(False))), "A")
        self.assertEqual(sop_to_text(minimal_sop(alarm_table(True))), "A + F")
        w = {k: int(Logic().decide(*k).warning) for k in alarm_table()}
        self.assertEqual(sop_to_text(minimal_sop(w)), "A'S")

    def test_forma_minima_equivale_alla_tabella(self):
        from logic import _covers
        for shadow in (False, True):
            t = alarm_table(shadow)
            sop = minimal_sop(t)
            for m, v in t.items():
                self.assertEqual(any(_covers(c, m) for c in sop), bool(v))

    def test_regole_solo_piezo(self):
        # la luce non cambia la classe del piezo: un colpo con la mano resta colpo
        self.assertEqual(classify(block("colpo", 2, shadow=True)).cls, "colpo")
        self.assertEqual(light_flags(block("colpo", 2, shadow=True))[0], True)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    store = AlarmStore(":memory:")
    p = Pipeline(store, BlockParser(clock=clock), clock=clock, **kw)
    return p, store, clock


def feed(p, ls):
    return [d for d in (p.handle_line(l) for l in ls) if d is not None]


class PipelineLogicTest(unittest.TestCase):
    def test_mano_e_colpo_allarme(self):
        p, store, _ = make()
        feed(p, lines("colpo", 2, shadow=True))
        self.assertEqual([r["cls"] for r in store.last(5)], ["colpo"])
        self.assertIn("F=1", store.last(1)[0]["detail"])

    def test_avviso_non_allarme(self):
        got = []
        p, store, _ = make(on_warning=lambda *a: got.append(1))
        feed(p, lines("pioggia"))                 # i blocchi finti di pioggia hanno il calo lento
        self.assertEqual(store.last(5), [])
        self.assertEqual(p.stats.warnings, 1)
        self.assertEqual(got, [1])
        self.assertEqual(store.events_since(60)[0]["outcome"], "avviso")

    def test_ombra_senza_urto(self):
        p, store, _ = make()
        feed(p, lines("vento", 1, shadow=True))
        self.assertEqual(store.last(5), [])
        p, store, _ = make(logic=Logic(shadow_alarm=True))
        feed(p, lines("vento", 1, shadow=True))
        self.assertEqual(store.last(1)[0]["cls"], "ombra")

    def test_eventi_registrati(self):
        p, store, _ = make()
        for c in ("calma", "vento", "colpo"):
            feed(p, lines(c))
        ev = store.events_since(60)
        self.assertEqual([e["cls"] for e in ev], ["calma", "vento", "colpo"])
        self.assertEqual([e["outcome"] for e in ev], ["-", "-", "allarme"])

    def test_sirena_sospende_il_rilevamento(self):
        p, store, clock = make(on_alarm=lambda *a: 3.0, guard_s=1.0, cooldown_s=0)
        feed(p, lines("colpo", 1))                # allarme: sirena 3 s + guardia 1 s
        clock.t += 2.0
        feed(p, lines("grandine", 2))             # la sirena fa vibrare il vetro: ignorato
        clock.t += 1.5
        feed(p, lines("colpo", 3))                # ancora nella guardia: ignorato
        clock.t += 1.0
        feed(p, lines("colpo", 4))                # 4,5 s dopo: si torna ad ascoltare
        self.assertEqual(p.stats.blocks_muted, 2)
        self.assertEqual(len(store.last(10)), 2)
        self.assertEqual(len(store.events_since(60)), 2)

    def test_blocco_iniziato_durante_la_sirena(self):
        # START dentro la sospensione, END dopo: il blocco contiene la sirena
        p, store, clock = make(on_alarm=lambda *a: 1.0, guard_s=0.5, cooldown_s=0)
        feed(p, lines("colpo", 1))
        ls = lines("colpo", 2)
        clock.t += 1.4
        feed(p, ls[:10])
        clock.t += 1.0
        feed(p, ls[10:])
        self.assertEqual(p.stats.blocks_muted, 1)

    def test_uscita_senza_durata_non_sospende(self):
        p, store, _ = make(on_alarm=lambda *a: None, cooldown_s=0)
        feed(p, lines("colpo", 1) + lines("colpo", 2))
        self.assertEqual(p.stats.blocks_muted, 0)
        self.assertEqual(len(store.last(5)), 2)


class OutputTest(unittest.TestCase):
    def test_sirena_simulata_e_led(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for led in ("red:user", "green:user"):
                (root / led).mkdir()
                (root / led / "brightness").write_text("0")
            out = MediaCarrierOutput(siren_s=0.2, sound=True, siren_cmd="true {wav}",
                                     led_root=root, wav_dir=root)
            det = classify(block("colpo"))
            self.assertEqual(out.alarm(det, None, {"name": "colpo"}), 0.2)
            self.assertEqual((root / "red:user" / "brightness").read_text(), "1")
            out.stop()
            self.assertEqual((root / "red:user" / "brightness").read_text(), "0")
            self.assertTrue((root / "solar_acoustics_siren.wav").stat().st_size > 1000)

    def test_wav(self):
        import wave
        with tempfile.TemporaryDirectory() as d:
            p = make_siren_wav(Path(d) / "s.wav", seconds=0.5)
            with wave.open(str(p)) as w:
                self.assertAlmostEqual(w.getnframes() / w.getframerate(), 0.5, places=2)


class EvaluateTest(unittest.TestCase):
    def test_csv_etichettati(self):
        with tempfile.TemporaryDirectory() as d:
            for cls in ("colpo", "vento"):
                rows = gen_block(cls, random.Random(5))
                with open(Path(d) / f"{cls}.01.csv", "w") as f:
                    f.write("timestamp,piezo,luce\n")
                    f.writelines(f"{i * 0.5:g},{p},{l}\n" for i, p, l in rows)
            (Path(d) / "corto.01.csv").write_text("timestamp,piezo,luce\n0,512,100\n")
            samples = list(from_csv([d], adc_bits=12))
            self.assertEqual(sorted(s[0] for s in samples), ["colpo", "vento"])  # il corto e' scartato
            conf, alarm, *_ = evaluate(iter(samples), classify, light_flags, Logic())
            self.assertEqual((alarm["tp"], alarm["tn"]), (1, 1))

    def test_evaluate_non_importa_il_generatore(self):
        import ast
        src = Path(__file__).with_name("evaluate.py").read_text()
        top = [n for n in ast.parse(src).body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertNotIn("fakemcu", [getattr(n, "module", None) for n in top])


if __name__ == "__main__":
    unittest.main()
