"""Modelo fisico simple de un auto enchufado a un conector.

El auto toma  min(limite ofrecido, su demanda)  y no carga si lo ofrecido es menor a min_a
(mismo comportamiento que IEC 61851: debajo de 6 A el EVSE no puede ofrecer carga).
La corriente sigue al objetivo con una rampa (A/s). La medicion reportada puede llevar ruido
(ver config `deviation`).
"""


class Vehicle:
    def __init__(self, vcfg, deviation, rng):
        self.min_a = float(vcfg.get("min_a", 6.0))
        self.ramp = float(vcfg.get("ramp_a_per_s", 5.0))
        self.battery_kwh = vcfg.get("battery_kwh")
        self.soc = float(vcfg.get("soc_percent", 20.0))
        self.dev = deviation
        self.rng = rng
        self.plugged = False
        self.demand_a = 0.0           # lo maximo que el auto acepta por fase
        self._demand_to = None        # (objetivo, A/s) cuando hay DEMAND ... over=
        self.current_a = 0.0          # corriente real nominal por fase (sin ruido)
        self.state = "idle"           # idle | charging | suspended_evse | suspended_ev

    # ---- demanda ---------------------------------------------------------------
    def set_demand(self, amps, over_s=0.0):
        amps = max(0.0, float(amps))
        if over_s and over_s > 0:
            self._demand_to = (amps, abs(amps - self.demand_a) / over_s)
        else:
            self.demand_a = amps
            self._demand_to = None

    def _advance_demand(self, dt):
        if self._demand_to is None:
            return
        target, rate = self._demand_to
        step = rate * dt
        if abs(target - self.demand_a) <= step:
            self.demand_a = target
            self._demand_to = None
        else:
            self.demand_a += step if target > self.demand_a else -step

    # ---- fisica ----------------------------------------------------------------
    def wants_a(self):
        if self.battery_kwh and self.soc >= 100.0:
            return 0.0
        return self.demand_a

    def step(self, dt, offered_a, charging_allowed, phases_used, voltage):
        """Avanza dt segundos. offered_a ya incluye el tope fisico del cargador."""
        self._advance_demand(dt)
        wants = self.wants_a()
        if not self.plugged:
            target, self.state = 0.0, "idle"
        elif not charging_allowed:
            target, self.state = 0.0, "idle"
        elif offered_a < self.min_a:
            target, self.state = 0.0, "suspended_evse"
        elif wants <= 0.05:
            target, self.state = 0.0, "suspended_ev"
        else:
            target, self.state = min(offered_a, wants), "charging"
        if self.current_a < target:
            self.current_a = min(target, self.current_a + self.ramp * dt)
        else:
            self.current_a = max(target, self.current_a - self.ramp * dt)
        if self.current_a < 0.01:
            self.current_a = 0.0
        if self.battery_kwh and self.current_a > 0:
            wh = self.current_a * voltage * phases_used * dt / 3600.0
            self.soc = min(100.0, self.soc + 100.0 * wh / (float(self.battery_kwh) * 1000.0))

    def measure(self):
        """Corriente por fase con ruido de medicion (A)."""
        i = self.current_a
        if i <= 0:
            return 0.0
        t = self.dev.get("type", "none")
        v = i + float(self.dev.get("bias_a", 0.0))
        amp = float(self.dev.get("abs_a", 0.0)) + float(self.dev.get("pct", 0.0)) / 100.0 * i
        if t == "gaussian" and amp > 0:
            v += self.rng.gauss(0.0, amp)
        elif t == "uniform" and amp > 0:
            v += self.rng.uniform(-amp, amp)
        return max(0.0, v)
