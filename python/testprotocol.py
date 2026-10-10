"""Test del parser. Esegui con:  python -m unittest -v test_protocol"""
import unittest

from protocol import END, START, Block, BlockParser, Rejected


def block_lines(n=500, first=lambda i: i, piezo=lambda i: 512, extra=None):
    lines = [START] + [f"{first(i)},{piezo(i)},108" for i in range(n)]
    if extra:
        lines.insert(10, extra)
    return lines + [END]


def run(parser, lines):
    return [r for r in (parser.feed(l) for l in lines) if r is not None]


class ParserTest(unittest.TestCase):
    def test_blocco_valido(self):
        out = run(BlockParser(), block_lines())
        self.assertEqual(len(out), 1)
        b = out[0]
        self.assertIsInstance(b, Block)
        self.assertEqual(len(b.piezo), 500)
        self.assertEqual(b.t_ms[:3], [0.0, 0.5, 1.0])  # 2000 Hz

    def test_debug_fuori_blocco_ignorato(self):
        out = run(BlockParser(), ["boot ok", "temp=31"] + block_lines() + ["fine"])
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], Block)

    def test_righe_mancanti(self):
        out = run(BlockParser(), block_lines(480))
        self.assertIsInstance(out[0], Rejected)

    def test_riga_malformata(self):
        out = run(BlockParser(), block_lines(extra="xx,??"))
        self.assertIsInstance(out[0], Rejected)

    def test_indici_non_progressivi(self):
        out = run(BlockParser(), block_lines(first=lambda i: i if i != 7 else 8))
        self.assertIn("indici", out[0].reason)

    def test_end_perso_poi_nuovo_blocco(self):
        lines = block_lines()[:-1] + block_lines()
        out = run(BlockParser(), lines)
        self.assertIsInstance(out[0], Rejected)
        self.assertIsInstance(out[1], Block)

    def test_range_adc(self):
        out = run(BlockParser(adc_bits=10), block_lines(piezo=lambda i: 2000))
        self.assertIn("range", out[0].reason)

    def test_micros(self):
        out = run(BlockParser(time_col="micros"), block_lines(first=lambda i: 5000 + 500 * i))
        self.assertEqual(out[0].t_ms[:3], [0.0, 0.5, 1.0])

    def test_timeout(self):
        now = [0.0]
        p = BlockParser(block_timeout=5.0, clock=lambda: now[0])
        p.feed(START)
        p.feed("1,2,3")
        now[0] = 6.0
        self.assertIsInstance(p.feed(""), Rejected)


if __name__ == "__main__":
    unittest.main()