"""Test della sorgente Bridge con un router finto.  python3 -m unittest -v testbridge"""
import threading
import unittest

from protocol import Block, BlockParser
from source import BRIDGE_METHOD, FORCE_METHOD, bridge_lines


class FakeBridge:
    def __init__(self):
        self.handlers = {}
        self.calls = []

    def provide(self, name, fn):
        self.handlers[name] = fn

    def call(self, name, *args):
        self.calls.append((name, args))
        return args[0] if args else None


def protocol_text(forced=False):
    rows = "".join(f"{i},{2048 + (300 if i == 100 else 0)},800\n" for i in range(500))
    return ("#FORCED\n" if forced else "") + "---EVENT_START---\n" + rows + "---EVENT_END---\n"


def chunks(text, size=400):
    """Spezza come lo sketch: pezzi di circa `size` byte, a fine riga."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        cur += line
        if len(cur) >= size:
            out.append(cur)
            cur = ""
    return out + ([cur] if cur else [])


class BridgeSourceTest(unittest.TestCase):
    def test_blocco_a_pezzi(self):
        fb = FakeBridge()
        gen = bridge_lines(bridge=fb, timeout=0.05)
        first = next(gen)                      # avvia il generatore: provide() fatto
        self.assertEqual(first, "")            # nessun dato: riga vuota (serve al timeout)
        for c in chunks(protocol_text(forced=True)):
            fb.handlers[BRIDGE_METHOD](c)
        parser = BlockParser(adc_bits=12)
        blocks = []
        for _ in range(510):
            r = parser.feed(next(gen))
            if isinstance(r, Block):
                blocks.append(r)
                break
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].piezo[100], 2348)

    def test_force_chiede_blocchi_alla_mcu(self):
        fb = FakeBridge()
        gen = bridge_lines(bridge=fb, force=30, timeout=0.01)
        next(gen)
        self.assertEqual(fb.calls, [(FORCE_METHOD, (30,))])

    def test_force_senza_risposta_non_blocca(self):
        class Muta(FakeBridge):
            def call(self, name, *args):
                raise TimeoutError("la MCU non risponde")
        fb = Muta()
        gen = bridge_lines(bridge=fb, force=10, timeout=0.01)
        self.assertEqual(next(gen), "")       # la raccolta continua (ponticello D2-GND)

    def test_notifiche_da_un_altro_thread(self):
        fb = FakeBridge()
        gen = bridge_lines(bridge=fb, timeout=0.5)
        next(gen)
        t = threading.Thread(target=lambda: [fb.handlers[BRIDGE_METHOD](c) for c in chunks(protocol_text())])
        t.start()
        got = [next(gen) for _ in range(502)]
        t.join()
        self.assertEqual(got[0], "---EVENT_START---")
        self.assertEqual(got[-1], "---EVENT_END---")


if __name__ == "__main__":
    unittest.main()
