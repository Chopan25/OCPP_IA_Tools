"""Corre escenarios completos contra un MockCSMS en proceso (puerto efimero)."""
import json
import os
import tempfile
import unittest

from evsim.config import load_config
from evsim.engine import Engine
from evsim.mockcsms import MockCSMS
from evsim.report import write_reports
from evsim.script import parse_file


def make_cfg(d, url, chargers=None, site_limit=40, deviation=None):
    chargers = chargers or [{"id": "A", "serial": "T-A"}, {"id": "B", "serial": "T-B"}]
    cfg = {"csms": {"url": url}, "site": {"limit_a": site_limit},
           "defaults": {"meter": {"sample_interval_s": 1}, "deviation": deviation or {"type": "none"}},
           "chargers": chargers}
    p = os.path.join(d, "sitio.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return load_config(p)


def make_script(d, text):
    p = os.path.join(d, "t.evs")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.csms = await MockCSMS(limit_a=40, port=0, meter_interval=1, settle_s=1.5, verbose=False).start()

    async def asyncTearDown(self):
        await self.csms.stop()

    async def run_script(self, text, mode="soft", url_suffix="", **cfg_kw):
        cfg = make_cfg(self.dir, self.csms.url + url_suffix, **cfg_kw)
        path = make_script(self.dir, text)
        prog = parse_file(path, known_chargers=cfg.chargers.keys())
        out = os.path.join(self.dir, "out")
        eng = Engine(cfg, prog, out, path, mode=mode, verbose=False)
        res = await eng.run()
        data = write_reports(eng, res, out)
        return res, data, out

    async def test_basic_dlb_scenario_passes_and_writes_reports(self):
        res, data, out = await self.run_script("""
SCENARIO "integracion"
DEFAULT_WITHIN 15s
TOLERANCE 1
ASSERT_ALWAYS SUM_MEASURED <= 40 id=sitio tol=1 grace=4s
CONNECT A B
PLUG A
DEMAND A 32
START A
EXPECT_LIMIT A == 32
WAIT_UNTIL_STABLE A for=2s timeout=20s
PLUG B
DEMAND B 32
START B
EXPECT_LIMIT A == 20
EXPECT_LIMIT B == 20
DEMAND A 10
EXPECT_LIMIT A == 10
EXPECT_LIMIT B == 30
STOP ALL
UNPLUG ALL
DISCONNECT ALL
""")
        self.assertEqual(res["status"], "passed", [e for e in res["expectations"] if not e["ok"]])
        self.assertEqual(res["checks"]["failed"], 0)
        for name in ("report.html", "report.json", "samples.csv", "site.csv", "messages.log"):
            self.assertTrue(os.path.getsize(os.path.join(out, name)) > 0, name)
        self.assertGreaterEqual(len(data["event_windows"]), 3)
        from evsim.summary import summarize
        text = summarize(out)
        self.assertIn("RESULTADO PASSED", text)
        self.assertIn("Perfiles de carga recibidos", text)
        self.assertIn("TxProfile stack 1", text)
        # el CSMS recibio MeterValues con corriente y energia
        with open(os.path.join(out, "messages.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("Current.Import", log)
        self.assertIn("Energy.Active.Import.Register", log)

    async def test_failed_expectation_in_soft_mode_continues_and_reports(self):
        res, _, _ = await self.run_script("""
DEFAULT_WITHIN 2s
CONNECT A
PLUG A
DEMAND A 32
START A
EXPECT_LIMIT A == 5
EXPECT_STATE A Charging
STOP ALL
DISCONNECT ALL
""")
        self.assertEqual(res["status"], "failed")
        oks = [e["ok"] for e in res["expectations"]]
        self.assertEqual(oks, [False, True])           # siguio despues del fallo

    async def test_strict_mode_aborts_on_first_failure(self):
        res, _, _ = await self.run_script("""
DEFAULT_WITHIN 1s
CONNECT A
EXPECT_LIMIT A == 5
EXPECT_STATE A Charging
""", mode="strict")
        self.assertEqual(res["status"], "failed")
        self.assertTrue(res["aborted"])
        self.assertEqual(len(res["expectations"]), 1)

    async def test_invariant_violation_is_reported_with_grace(self):
        res, _, _ = await self.run_script("""
DEFAULT_WITHIN 10s
ASSERT_ALWAYS SUM_MEASURED <= 10 id=bajo grace=1s
CONNECT A
PLUG A
DEMAND A 32
START A
WAIT 8s
STOP ALL
DISCONNECT ALL
""")
        inv = res["invariants"][0]
        self.assertTrue(inv["failed"])
        self.assertTrue(inv["violations"][0]["exceeds_grace"])
        self.assertEqual(res["status"], "failed")

    async def test_start_without_plug_is_a_script_error_with_line(self):
        res, _, _ = await self.run_script("CONNECT A\nSTART A\n")
        self.assertEqual(res["status"], "error")
        self.assertIn("t.evs:2", res["error"])

    async def test_random_arrivals_are_reproducible(self):
        def plan(res_events):
            return [e["text"] for e in res_events if e["kind"] == "plan"]
        txt = "CONNECT A B\nRANDOM_ARRIVALS n=3 over=3s duration=2s..3s demand=10..20 seed=11\nWAIT_ARRIVALS\nDISCONNECT ALL\n"
        r1, d1, _ = await self.run_script(txt)
        r2, d2, _ = await self.run_script(txt)
        self.assertEqual(r1["status"], "passed", r1)
        self.assertEqual(plan(d1["events"]), plan(d2["events"]))
        self.assertEqual(len(plan(d1["events"])), 3)

    async def test_noisy_meter_reports_deviation(self):
        res, _, out = await self.run_script("""
CONNECT A
PLUG A
DEMAND A 16
START A
WAIT 8s
EXPECT_MEASURED A == 16 tol=3
EXPECT_ACTUAL A == 16 tol=0.01
STOP ALL
DISCONNECT ALL
""", deviation={"type": "gaussian", "abs_a": 0.5})
        self.assertEqual(res["status"], "passed", [e for e in res["expectations"] if not e["ok"]])
        vals = set()
        with open(os.path.join(out, "messages.log"), encoding="utf-8") as f:
            for line in f:
                if '"Current.Import"' in line and '"phase":"L1"' in line:
                    i = line.index('"value":"', line.index('"measurand":"Current.Import"') - 80)
                    vals.add(line[i + 9:i + 14])
        self.assertGreater(len(vals), 1)               # hay ruido: no todas las muestras son iguales

    def _all_text(self, out):
        txt = ""
        for name in os.listdir(out):
            with open(os.path.join(out, name), encoding="utf-8", errors="replace") as f:
                txt += f.read()
        return txt

    async def test_reports_never_contain_the_account_key(self):
        token = "Zk3Qw9Lm2Xv7Bn4Rt8Yp5Hc1Df6G"
        res, _, out = await self.run_script("""
CONNECT A
PLUG A
DEMAND A 16
START A
WAIT 3s
STOP ALL
DISCONNECT ALL
""", url_suffix="/" + token)
        self.assertEqual(res["status"], "passed")
        text = self._all_text(out)
        self.assertNotIn(token, text)
        self.assertIn("T-A", text)                      # el serial si se muestra

    async def test_connection_error_does_not_leak_the_account_key(self):
        token = "Zk3Qw9Lm2Xv7Bn4Rt8Yp5Hc1Df6G"
        cfg = make_cfg(self.dir, "ws://127.0.0.1:1/ocpp/" + token)
        path = make_script(self.dir, "CONNECT A timeout=3s\n")
        prog = parse_file(path, known_chargers=cfg.chargers.keys())
        out = os.path.join(self.dir, "out_err")
        eng = Engine(cfg, prog, out, path, mode="soft", verbose=False)
        res = await eng.run()
        write_reports(eng, res, out)
        self.assertEqual(res["status"], "error")
        self.assertNotIn(token, self._all_text(out))


if __name__ == "__main__":
    unittest.main()
