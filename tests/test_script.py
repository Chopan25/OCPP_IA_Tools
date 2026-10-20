import os
import tempfile
import unittest

from evsim.script import ScriptError, parse_file, walk
from evsim.util import parse_duration, safe_eval, to_number
from evsim.vehicle import Vehicle
import random


def write(text, name="t.evs", d=None):
    d = d or tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


class ParserTests(unittest.TestCase):
    def parse(self, text, chargers=("A", "B")):
        return parse_file(write(text), known_chargers=chargers)

    def err(self, text, chargers=("A", "B")):
        with self.assertRaises(ScriptError) as cm:
            self.parse(text, chargers)
        return cm.exception

    def test_ok_and_comments_and_quotes(self):
        prog = self.parse('; hola\nSCENARIO "Mi prueba ; no es comentario"\nPLUG A ; comentario\nDEMAND A 32 over=10s\n')
        self.assertEqual([s.verb for s in prog], ["SCENARIO", "PLUG", "DEMAND"])
        self.assertEqual(prog[0].args[0], "Mi prueba ; no es comentario")
        self.assertEqual(prog[2].kwargs, {"over": "10s"})

    def test_unknown_verb_reports_line(self):
        e = self.err("PLUG A\n\nPLUGG B\n")
        self.assertEqual(e.line, 3)
        self.assertIn("verbo desconocido", e.msg)

    def test_unknown_charger(self):
        e = self.err("PLUG A\nDEMAND Z 10\n")
        self.assertEqual(e.line, 2)
        self.assertIn("cargador desconocido", e.msg)

    def test_connector_suffix_ok(self):
        self.parse("PLUG A.2\nEXPECT_LIMIT B.1 == 10\n")

    def test_arity(self):
        self.assertIn("argumento", self.err("DEMAND A\n").msg)
        self.assertIn("argumento", self.err("WAIT\n").msg)

    def test_unknown_param(self):
        self.assertIn("desconocido", self.err("DEMAND A 10 speed=3\n").msg)

    def test_bad_operator(self):
        self.assertIn("operador", self.err("EXPECT_LIMIT A = 10\n").msg)

    def test_undefined_variable(self):
        e = self.err("DEMAND A $x\n")
        self.assertIn("$x", e.msg)

    def test_set_defines_variable(self):
        self.parse("SET x 10\nDEMAND A $x\nSET y=3\nDEMAND B $y\n")

    def test_blocks(self):
        prog = self.parse("FOR c IN A B\n  PLUG $c\n  REPEAT 2 var=i\n    DEMAND $c $i\n  END\nEND\n")
        self.assertEqual(len(prog), 1)
        self.assertEqual([s.verb for s in walk(prog)], ["FOR", "PLUG", "REPEAT", "DEMAND"])

    def test_unclosed_block_points_to_opening(self):
        e = self.err("PLUG A\nREPEAT 3\n  PLUG B\n")
        self.assertEqual(e.line, 2)
        self.assertIn("END", e.msg)

    def test_stray_end(self):
        self.assertIn("END sin", self.err("PLUG A\nEND\n").msg)

    def test_unclosed_quote(self):
        self.assertIn("comilla", self.err('PRINT "hola\n').msg)

    def test_include(self):
        d = tempfile.mkdtemp()
        write("PLUG A\nDEMAND A 10\n", "sub.evs", d)
        p = write("INCLUDE sub.evs\nSTART A\n", "main.evs", d)
        prog = parse_file(p, known_chargers=("A",))
        self.assertEqual([s.verb for s in prog], ["PLUG", "DEMAND", "START"])
        self.assertEqual(prog[0].file.endswith("sub.evs"), True)

    def test_include_cycle(self):
        d = tempfile.mkdtemp()
        write("INCLUDE b.evs\n", "a.evs", d)
        write("INCLUDE a.evs\n", "b.evs", d)
        with self.assertRaises(ScriptError) as cm:
            parse_file(os.path.join(d, "a.evs"))
        self.assertIn("circular", cm.exception.msg)


class UtilTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration("20s"), 20)
        self.assertEqual(parse_duration("500ms"), 0.5)
        self.assertEqual(parse_duration("2m"), 120)
        self.assertEqual(parse_duration(7), 7)
        with self.assertRaises(ValueError):
            parse_duration("abc")

    def test_safe_eval(self):
        self.assertEqual(safe_eval("40/2+1"), 21)
        self.assertEqual(safe_eval("max(3, 4*2)"), 8)
        self.assertEqual(to_number("x*2", {"x": 5}), 10)
        for bad in ("__import__('os')", "open('x')", "a.b", "[1]"):
            with self.assertRaises(ValueError):
                safe_eval(bad)


class VehicleTests(unittest.TestCase):
    def mk(self, **dev):
        d = {"type": "none", "abs_a": 0, "pct": 0, "bias_a": 0}
        d.update(dev)
        return Vehicle({"min_a": 6, "ramp_a_per_s": 5}, d, random.Random("x:A"))

    def run_for(self, v, secs, offered, allowed=True):
        for _ in range(int(secs * 4)):
            v.step(0.25, offered, allowed, 3, 230)

    def test_follows_min_of_offered_and_demand(self):
        v = self.mk()
        v.plugged = True
        v.set_demand(20)
        self.run_for(v, 10, 32)
        self.assertAlmostEqual(v.current_a, 20)
        self.run_for(v, 10, 12)
        self.assertAlmostEqual(v.current_a, 12)
        self.assertEqual(v.state, "charging")

    def test_below_min_suspends(self):
        v = self.mk()
        v.plugged = True
        v.set_demand(20)
        self.run_for(v, 10, 5.9)
        self.assertEqual(v.current_a, 0)
        self.assertEqual(v.state, "suspended_evse")

    def test_ramp_is_gradual(self):
        v = self.mk()
        v.plugged = True
        v.set_demand(32)
        self.run_for(v, 2, 32)
        self.assertAlmostEqual(v.current_a, 10, places=1)

    def test_demand_ramp_over(self):
        v = self.mk()
        v.plugged = True
        v.set_demand(10)
        self.run_for(v, 5, 32)
        v.set_demand(30, over_s=10)
        self.run_for(v, 5, 32)
        self.assertAlmostEqual(v.demand_a, 20, places=1)

    def test_deviation_is_seeded_and_bounded(self):
        a, b = self.mk(type="uniform", abs_a=0.5), self.mk(type="uniform", abs_a=0.5)
        for v in (a, b):
            v.plugged = True
            v.set_demand(16)
            self.run_for(v, 8, 32)
        xs = [a.measure() for _ in range(50)]
        ys = [b.measure() for _ in range(50)]
        self.assertEqual(xs, ys)                       # misma semilla => mismo ruido
        self.assertTrue(all(15.5 <= x <= 16.5 for x in xs))
        self.assertGreater(len(set(xs)), 10)

    def test_bias(self):
        v = self.mk(bias_a=0.3)
        v.plugged = True
        v.set_demand(10)
        self.run_for(v, 5, 32)
        self.assertAlmostEqual(v.measure(), 10.3)


if __name__ == "__main__":
    unittest.main()
