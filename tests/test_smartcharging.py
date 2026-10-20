import unittest
from datetime import datetime, timedelta, timezone

from evsim.smartcharging import ProfileStore
from evsim.util import iso

T0 = datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)   # lunes
SMART = {"max_stack_level": 8, "max_periods": 5, "max_profiles": 4, "allowed_units": "Current,Power"}


def prof(pid, stack, purpose="TxProfile", kind="Absolute", periods=None, unit="A", start=T0, **extra):
    d = {
        "chargingProfileId": pid, "stackLevel": stack, "chargingProfilePurpose": purpose, "chargingProfileKind": kind,
        "chargingSchedule": {"chargingRateUnit": unit, "chargingSchedulePeriod": periods or [{"startPeriod": 0, "limit": 16}]},
    }
    if kind != "Relative":
        d["chargingSchedule"]["startSchedule"] = iso(start)
    for k, v in extra.items():
        if k == "duration":
            d["chargingSchedule"]["duration"] = v
        else:
            d[k] = v
    return d


def eff(store, t=T0, cid=1, tx=True, v=230, ph=3, tx_start=None):
    return store.effective(cid, t, tx, 7 if tx else None, tx_start or T0, v, ph)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.s = ProfileStore(SMART)

    def test_no_profile_means_no_limit(self):
        self.assertIsNone(eff(self.s).limit_a)

    def test_absolute_single_period(self):
        self.assertEqual(self.s.set(1, prof(1, 0), T0, tx_id=7, tx_active=True), "Accepted")
        self.assertEqual(eff(self.s).limit_a, 16)

    def test_periods_switch_over_time(self):
        p = prof(1, 0, periods=[{"startPeriod": 0, "limit": 32}, {"startPeriod": 60, "limit": 10}])
        self.s.set(1, p, T0, tx_id=7, tx_active=True)
        self.assertEqual(eff(self.s, T0 + timedelta(seconds=59)).limit_a, 32)
        self.assertEqual(eff(self.s, T0 + timedelta(seconds=60)).limit_a, 10)

    def test_highest_stack_wins(self):
        self.s.set(1, prof(1, 0, periods=[{"startPeriod": 0, "limit": 32}]), T0, 7, True)
        self.s.set(1, prof(2, 3, periods=[{"startPeriod": 0, "limit": 12}]), T0, 7, True)
        self.assertEqual(eff(self.s).limit_a, 12)

    def test_txprofile_overrides_txdefault_even_with_lower_stack(self):
        self.s.set(1, prof(1, 5, purpose="TxDefaultProfile", periods=[{"startPeriod": 0, "limit": 8}]), T0)
        self.s.set(1, prof(2, 0, periods=[{"startPeriod": 0, "limit": 20}]), T0, 7, True)
        self.assertEqual(eff(self.s).limit_a, 20)

    def test_txdefault_applies_when_no_tx(self):
        self.s.set(0, prof(1, 0, purpose="TxDefaultProfile", periods=[{"startPeriod": 0, "limit": 18}]), T0)
        self.assertEqual(eff(self.s, tx=False).limit_a, 18)

    def test_chargepointmax_caps_everything(self):
        self.s.set(0, prof(1, 0, purpose="ChargePointMaxProfile", periods=[{"startPeriod": 0, "limit": 25}]), T0)
        self.s.set(1, prof(2, 0, periods=[{"startPeriod": 0, "limit": 32}]), T0, 7, True)
        self.assertEqual(eff(self.s).limit_a, 25)
        self.s.set(1, prof(3, 1, periods=[{"startPeriod": 0, "limit": 10}]), T0, 7, True)
        self.assertEqual(eff(self.s).limit_a, 10)

    def test_power_unit_converted_with_phases(self):
        self.s.set(1, prof(1, 0, unit="W", periods=[{"startPeriod": 0, "limit": 6900}]), T0, 7, True)
        self.assertAlmostEqual(eff(self.s, v=230, ph=3).limit_a, 10.0, places=3)
        self.s.set(1, prof(1, 0, unit="W", periods=[{"startPeriod": 0, "limit": 6900, "numberPhases": 1}]), T0, 7, True)
        self.assertAlmostEqual(eff(self.s, v=230, ph=3).limit_a, 30.0, places=3)

    def test_duration_expiry_falls_back_to_lower_stack(self):
        self.s.set(1, prof(1, 0, periods=[{"startPeriod": 0, "limit": 32}]), T0, 7, True)
        self.s.set(1, prof(2, 5, periods=[{"startPeriod": 0, "limit": 6}], duration=30), T0, 7, True)
        self.assertEqual(eff(self.s, T0 + timedelta(seconds=10)).limit_a, 6)
        self.assertEqual(eff(self.s, T0 + timedelta(seconds=31)).limit_a, 32)

    def test_valid_from_to(self):
        self.s.set(1, prof(1, 0, validFrom=iso(T0 + timedelta(seconds=10)), validTo=iso(T0 + timedelta(seconds=20))), T0, 7, True)
        self.assertIsNone(eff(self.s, T0 + timedelta(seconds=5)).limit_a)
        self.assertEqual(eff(self.s, T0 + timedelta(seconds=15)).limit_a, 16)
        self.assertIsNone(eff(self.s, T0 + timedelta(seconds=20)).limit_a)

    def test_relative_starts_with_transaction(self):
        p = prof(1, 0, kind="Relative", periods=[{"startPeriod": 0, "limit": 32}, {"startPeriod": 30, "limit": 8}])
        self.s.set(1, p, T0, 7, True)
        tx0 = T0 + timedelta(seconds=100)
        self.assertEqual(eff(self.s, tx0 + timedelta(seconds=10), tx_start=tx0).limit_a, 32)
        self.assertEqual(eff(self.s, tx0 + timedelta(seconds=31), tx_start=tx0).limit_a, 8)

    def test_daily_recurring(self):
        p = prof(1, 0, purpose="TxDefaultProfile", kind="Recurring", recurrencyKind="Daily",
                 periods=[{"startPeriod": 0, "limit": 32}, {"startPeriod": 3600, "limit": 6}], duration=7200)
        self.s.set(0, p, T0)
        day2 = T0 + timedelta(days=1)
        self.assertEqual(eff(self.s, day2 + timedelta(minutes=30), tx=False).limit_a, 32)
        self.assertEqual(eff(self.s, day2 + timedelta(minutes=90), tx=False).limit_a, 6)
        self.assertIsNone(eff(self.s, day2 + timedelta(minutes=150), tx=False).limit_a)

    def test_replacement_same_stack_and_purpose(self):
        self.s.set(1, prof(1, 2, periods=[{"startPeriod": 0, "limit": 32}]), T0, 7, True)
        self.s.set(1, prof(9, 2, periods=[{"startPeriod": 0, "limit": 11}]), T0, 7, True)
        self.assertEqual(len(self.s.items), 1)
        self.assertEqual(eff(self.s).limit_a, 11)

    def test_replacement_same_id(self):
        self.s.set(1, prof(1, 2, periods=[{"startPeriod": 0, "limit": 32}]), T0, 7, True)
        self.s.set(1, prof(1, 4, periods=[{"startPeriod": 0, "limit": 9}]), T0, 7, True)
        self.assertEqual(len(self.s.items), 1)
        self.assertEqual(eff(self.s).limit_a, 9)

    def test_rejections(self):
        self.assertEqual(self.s.set(1, prof(1, 99), T0, 7, True), "Rejected")                          # stack
        self.assertEqual(self.s.set(1, prof(1, 0), T0, None, False), "Rejected")                       # TxProfile sin tx
        self.assertEqual(self.s.set(0, prof(1, 0), T0, None, False), "Rejected")                       # TxProfile en conector 0
        self.assertEqual(self.s.set(1, prof(1, 0, purpose="ChargePointMaxProfile"), T0), "Rejected")   # CPMax en conector 1
        p = prof(1, 0, periods=[{"startPeriod": 5, "limit": 10}])
        self.assertEqual(self.s.set(1, p, T0, 7, True), "Rejected")                                    # primer periodo != 0
        p = prof(1, 0, periods=[{"startPeriod": 0, "limit": 10}] * 2)
        self.assertEqual(self.s.set(1, p, T0, 7, True), "Rejected")                                    # no creciente
        p = prof(1, 0, periods=[{"startPeriod": i * 10, "limit": 10} for i in range(6)])
        self.assertEqual(self.s.set(1, p, T0, 7, True), "Rejected")                                    # demasiados periodos
        self.assertEqual(self.s.set(1, prof(1, 0, transactionId=99), T0, 7, True), "Rejected")         # otra transaccion
        self.assertEqual(self.s.set(1, {"junk": 1}, T0, 7, True), "Rejected")

    def test_max_profiles(self):
        for i in range(4):
            self.assertEqual(self.s.set(1, prof(i, i), T0, 7, True), "Accepted")
        self.assertEqual(self.s.set(1, prof(10, 6), T0, 7, True), "Rejected")

    def test_clear_filters(self):
        self.s.set(1, prof(1, 1), T0, 7, True)
        self.s.set(0, prof(2, 1, purpose="TxDefaultProfile"), T0)
        self.assertEqual(self.s.clear(profile_id=1), 1)
        self.assertEqual(self.s.clear(profile_id=1), 0)
        self.assertEqual(self.s.clear(purpose="TxDefaultProfile", stack=1), 1)
        self.assertEqual(self.s.items, [])

    def test_clear_tx_profiles_on_stop(self):
        self.s.set(1, prof(1, 1), T0, 7, True)
        self.s.set(0, prof(2, 1, purpose="TxDefaultProfile"), T0)
        self.s.clear_tx_profiles(1)
        self.assertEqual([p.purpose for _, p in self.s.items], ["TxDefaultProfile"])

    def test_composite_merges_and_finds_boundaries(self):
        p = prof(1, 0, periods=[{"startPeriod": 0, "limit": 32}, {"startPeriod": 30, "limit": 10}], duration=60)
        self.s.set(1, p, T0, 7, True)
        comp = self.s.composite(1, T0, 120, True, 7, T0, 230, 3)
        self.assertEqual([(o, l) for o, l, _ in comp], [(0, 32), (30, 10), (60, None)])


if __name__ == "__main__":
    unittest.main()
