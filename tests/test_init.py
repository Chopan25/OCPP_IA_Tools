import json
import os
import tempfile
import unittest

from evsim.config import load_config
from evsim.initcfg import InitError, build_config, mask, split_link, write_config

TOKEN = "Zk3Qw9Lm2Xv7Bn4Rt8Yp5Hc1Df6G"
L1 = "wss://ocpp.example.com:1403/ocpp/160/%s/DLB_01" % TOKEN
L2 = "wss://ocpp.example.com:1403/ocpp/160/%s/DLB_02" % TOKEN


class InitTests(unittest.TestCase):
    def test_split_link(self):
        self.assertEqual(split_link(L1), ("wss://ocpp.example.com:1403/ocpp/160/" + TOKEN, "DLB_01", False))
        self.assertEqual(split_link(" '" + L2 + "/' ")[1], "DLB_02")

    def test_bad_links(self):
        for bad in ("http://x/y", "wss://host", "hola", "wss://host/"):
            with self.assertRaises(InitError):
                split_link(bad)

    def test_mask_hides_token_but_keeps_serial(self):
        m = mask(L1)
        self.assertNotIn(TOKEN, m)
        self.assertTrue(m.endswith("/DLB_01"))
        self.assertIn("ocpp.example.com:1403", m)

    def test_error_messages_do_not_leak_token(self):
        with self.assertRaises(InitError) as cm:
            split_link("http://h/%s/x" % TOKEN)
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_build_config_roundtrips_through_loader(self):
        cfg = build_config([L1, L2], limit_a=40, max_a=32)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.json")
        write_config(cfg, p)
        loaded = load_config(p)
        self.assertEqual(list(loaded.chargers), ["A", "B"])
        self.assertEqual(loaded.chargers["A"].url, L1)
        self.assertEqual(loaded.chargers["B"].url, L2)
        self.assertEqual(loaded.chargers["A"].id_tag, "plug_and_play")
        self.assertEqual(loaded.site_limit_a, 40)

    def test_different_bases_use_per_charger_urls(self):
        other = "wss://otro.example.com/ocpp/xyz/DLB_03"
        cfg = build_config([L1, other])
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.json")
        write_config(cfg, p)
        loaded = load_config(p)
        self.assertEqual(loaded.chargers["B"].url, other)
        self.assertEqual(loaded.chargers["A"].url, L1)

    def test_duplicate_serials_rejected(self):
        with self.assertRaises(InitError):
            build_config([L1, L1])

    def test_no_overwrite_without_force(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.json")
        write_config(build_config([L1]), p)
        with self.assertRaises(InitError):
            write_config(build_config([L1]), p)
        write_config(build_config([L1, L2]), p, force=True)
        self.assertEqual(len(json.load(open(p, encoding="utf-8"))["chargers"]), 2)


if __name__ == "__main__":
    unittest.main()
