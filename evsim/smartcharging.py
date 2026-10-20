"""Perfiles de carga OCPP 1.6 (Smart Charging): validacion, almacenamiento y resolucion del limite efectivo.

Es la parte que decide "cuantos amperes puede tomar este conector ahora" a partir de los
SetChargingProfile que manda el CSMS. Reglas implementadas (OCPP 1.6 edicion 2, sec. 3.13 y 7.x):

* Proposito: ChargePointMaxProfile (solo connectorId 0), TxDefaultProfile (0 o conector),
  TxProfile (conector > 0, requiere transaccion activa).
* Dentro de un proposito gana el stackLevel mas alto que este activo en ese instante.
* TxProfile pisa a TxDefaultProfile; el resultado se topa con ChargePointMaxProfile (minimo).
* Kinds: Absolute, Recurring (Daily/Weekly) y Relative (desde el inicio de la transaccion).
* Unidades A y W (W se convierte con voltaje x fases del periodo activo).
* El limite se interpreta POR FASE, como define OCPP 1.6 para la unidad A.
"""
import math
from datetime import timedelta

from .util import parse_iso

PURPOSES = ("ChargePointMaxProfile", "TxDefaultProfile", "TxProfile")
KINDS = ("Absolute", "Recurring", "Relative")
RECURRENCY = {"Daily": 86400, "Weekly": 604800}


class ProfileError(Exception):
    pass


class ChargingProfile:
    def __init__(self, d, installed_at):
        try:
            self.raw = d
            self.id = int(d["chargingProfileId"])
            self.stack = int(d["stackLevel"])
            self.purpose = d["chargingProfilePurpose"]
            self.kind = d["chargingProfileKind"]
            sched = d["chargingSchedule"]
            self.unit = sched["chargingRateUnit"]
            periods = sched["chargingSchedulePeriod"]
        except (KeyError, TypeError, ValueError) as e:
            raise ProfileError("campo obligatorio faltante o invalido: %s" % e)
        if self.stack < 0:
            raise ProfileError("stackLevel negativo")
        if self.purpose not in PURPOSES:
            raise ProfileError("chargingProfilePurpose invalido: %s" % self.purpose)
        if self.kind not in KINDS:
            raise ProfileError("chargingProfileKind invalido: %s" % self.kind)
        if self.unit not in ("A", "W"):
            raise ProfileError("chargingRateUnit invalido: %s" % self.unit)
        self.transaction_id = d.get("transactionId")
        self.recurrency = d.get("recurrencyKind")
        if self.kind == "Recurring" and self.recurrency not in RECURRENCY:
            raise ProfileError("Recurring requiere recurrencyKind Daily|Weekly")
        self.valid_from = parse_iso(d.get("validFrom"))
        self.valid_to = parse_iso(d.get("validTo"))
        self.duration = sched.get("duration")
        self.duration = float(self.duration) if self.duration is not None else None
        ss = sched.get("startSchedule")
        self.start_schedule = parse_iso(ss) if ss else installed_at
        self.min_rate = sched.get("minChargingRate")
        if not isinstance(periods, list) or not periods:
            raise ProfileError("chargingSchedulePeriod debe tener al menos un periodo")
        self.periods = []
        last = -1
        for p in periods:
            try:
                sp = int(p["startPeriod"])
                lim = float(p["limit"])
            except (KeyError, TypeError, ValueError) as e:
                raise ProfileError("periodo invalido: %s" % e)
            nph = p.get("numberPhases")
            if sp <= last:
                raise ProfileError("startPeriod debe ser estrictamente creciente")
            if lim < 0:
                raise ProfileError("limit negativo")
            last = sp
            self.periods.append((sp, lim, int(nph) if nph is not None else None))
        if self.periods[0][0] != 0:
            raise ProfileError("el primer startPeriod debe ser 0")

    def schedule_start(self, now, tx_start):
        """Inicio de la instancia del schedule vigente en 'now' (None si no aplica)."""
        if self.kind == "Absolute":
            return self.start_schedule
        if self.kind == "Relative":
            return tx_start
        period = RECURRENCY[self.recurrency]
        if now < self.start_schedule:
            return None
        n = math.floor((now - self.start_schedule).total_seconds() / period)
        return self.start_schedule + timedelta(seconds=n * period)

    def period_at(self, now, tx_start):
        """-> (limit, numberPhases) en la unidad nativa del perfil, o None si no esta activo."""
        if self.valid_from and now < self.valid_from:
            return None
        if self.valid_to and now >= self.valid_to:
            return None
        start = self.schedule_start(now, tx_start)
        if start is None:
            return None
        elapsed = (now - start).total_seconds()
        if elapsed < 0:
            return None
        if self.duration is not None and elapsed >= self.duration:
            return None
        cur = self.periods[0]
        for p in self.periods:
            if p[0] <= elapsed:
                cur = p
            else:
                break
        return cur[1], cur[2]

    def boundaries(self, ws, we, tx_start):
        """Instantes dentro de (ws, we) donde el limite de este perfil puede cambiar."""
        out = set()
        for t in (self.valid_from, self.valid_to):
            if t:
                out.add(t)
        starts = []
        if self.kind == "Recurring":
            period = RECURRENCY[self.recurrency]
            n0 = math.floor((ws - self.start_schedule).total_seconds() / period) - 1
            n1 = math.floor((we - self.start_schedule).total_seconds() / period) + 1
            starts = [self.start_schedule + timedelta(seconds=n * period) for n in range(max(n0, 0), max(n1, 0) + 1)]
        elif self.kind == "Absolute":
            starts = [self.start_schedule]
        elif tx_start is not None:
            starts = [tx_start]
        for s in starts:
            out.add(s)
            for sp, _, _ in self.periods:
                out.add(s + timedelta(seconds=sp))
            if self.duration is not None:
                out.add(s + timedelta(seconds=self.duration))
        return [t for t in out if ws < t < we]


class Effective:
    """Resultado de resolver los perfiles: limite en A por fase (None = sin limite)."""

    def __init__(self, limit_a=None, phases=None, source=None):
        self.limit_a = limit_a
        self.phases = phases
        self.source = source

    def __repr__(self):
        return "Effective(%s A, phases=%s, src=%s)" % (self.limit_a, self.phases, self.source)


class ProfileStore:
    def __init__(self, smart_cfg):
        self.max_stack = int(smart_cfg.get("max_stack_level", 8))
        self.max_periods = int(smart_cfg.get("max_periods", 24))
        self.max_profiles = int(smart_cfg.get("max_profiles", 20))
        self.allowed_units = str(smart_cfg.get("allowed_units", "Current,Power"))
        self.items = []  # [(connector_id, ChargingProfile)]

    # ---- alta / baja -----------------------------------------------------------
    def set(self, connector_id, d, now, tx_id=None, tx_active=False):
        """Devuelve 'Accepted' | 'Rejected' | 'NotSupported' (+ motivo en self.last_reason)."""
        self.last_reason = ""
        try:
            prof = ChargingProfile(d, now)
        except ProfileError as e:
            self.last_reason = str(e)
            return "Rejected"
        if prof.stack > self.max_stack:
            self.last_reason = "stackLevel %d > ChargeProfileMaxStackLevel %d" % (prof.stack, self.max_stack)
            return "Rejected"
        allowed = {"A": "Current", "W": "Power"}[prof.unit]
        if allowed not in self.allowed_units:
            self.last_reason = "unidad %s no permitida" % prof.unit
            return "Rejected"
        if len(prof.periods) > self.max_periods:
            self.last_reason = "mas de ChargingScheduleMaxPeriods (%d)" % self.max_periods
            return "Rejected"
        if prof.purpose == "ChargePointMaxProfile" and connector_id != 0:
            self.last_reason = "ChargePointMaxProfile solo en connectorId 0"
            return "Rejected"
        if prof.purpose == "TxProfile":
            if connector_id == 0:
                self.last_reason = "TxProfile requiere connectorId > 0"
                return "Rejected"
            if not tx_active:
                self.last_reason = "TxProfile sin transaccion activa"
                return "Rejected"
            if prof.transaction_id is not None and tx_id is not None and int(prof.transaction_id) != int(tx_id):
                self.last_reason = "transactionId no coincide con la transaccion activa"
                return "Rejected"
        # reemplazo: mismo id, o mismo stackLevel + proposito en el mismo conector
        self.items = [
            (c, p) for (c, p) in self.items
            if p.id != prof.id and not (c == connector_id and p.stack == prof.stack and p.purpose == prof.purpose)
        ]
        if len(self.items) >= self.max_profiles:
            self.last_reason = "MaxChargingProfilesInstalled alcanzado"
            return "Rejected"
        self.items.append((connector_id, prof))
        return "Accepted"

    def clear(self, profile_id=None, connector_id=None, purpose=None, stack=None):
        before = len(self.items)
        keep = []
        for c, p in self.items:
            match = True
            if profile_id is not None and p.id != profile_id:
                match = False
            if connector_id is not None and c != connector_id:
                match = False
            if purpose is not None and p.purpose != purpose:
                match = False
            if stack is not None and p.stack != stack:
                match = False
            if not match:
                keep.append((c, p))
        self.items = keep
        return before - len(keep)

    def clear_tx_profiles(self, connector_id):
        self.items = [(c, p) for (c, p) in self.items if not (c == connector_id and p.purpose == "TxProfile")]

    def describe(self):
        return [
            {"connector": c, "id": p.id, "stack": p.stack, "purpose": p.purpose, "kind": p.kind,
             "unit": p.unit, "periods": [{"start": s, "limit": l, "phases": n} for s, l, n in p.periods]}
            for c, p in sorted(self.items, key=lambda x: (x[1].purpose, -x[1].stack))
        ]

    # ---- resolucion ------------------------------------------------------------
    def _best(self, cands, now, tx_start, voltage, default_phases):
        for p in sorted(cands, key=lambda p: -p.stack):
            r = p.period_at(now, tx_start)
            if r is None:
                continue
            limit, nph = r
            phases = nph or default_phases
            if p.unit == "W":
                limit = limit / (voltage * max(phases, 1))
            return limit, nph, "%s#%d(stack %d)" % (p.purpose, p.id, p.stack)
        return None

    def effective(self, connector_id, now, tx_active, tx_id, tx_start, voltage, default_phases):
        txp = [p for c, p in self.items
               if p.purpose == "TxProfile" and c == connector_id and tx_active
               and (p.transaction_id is None or tx_id is None or int(p.transaction_id) == int(tx_id))]
        txd_specific = [p for c, p in self.items if p.purpose == "TxDefaultProfile" and c == connector_id]
        txd_all = [p for c, p in self.items if p.purpose == "TxDefaultProfile" and c == 0]
        cpmax = [p for c, p in self.items if p.purpose == "ChargePointMaxProfile"]

        base = self._best(txp, now, tx_start, voltage, default_phases)
        if base is None:
            base = self._best(txd_specific + txd_all, now, tx_start, voltage, default_phases)
        cap = self._best(cpmax, now, tx_start, voltage, default_phases)

        if base is None and cap is None:
            return Effective()
        if base is None:
            return Effective(cap[0], cap[1], cap[2])
        if cap is None or base[0] <= cap[0]:
            return Effective(base[0], base[1], base[2])
        return Effective(cap[0], cap[1], cap[2])

    def composite(self, connector_id, now, duration_s, tx_active, tx_id, tx_start, voltage, default_phases):
        """Lista [(offset_s, limit_a, phases)] compactada; limit None = sin limite."""
        we = now + timedelta(seconds=duration_s)
        cps = {now}
        for c, p in self.items:
            if c in (0, connector_id):
                cps.update(p.boundaries(now, we, tx_start))
        out = []
        for t in sorted(cps):
            e = self.effective(connector_id, t, tx_active, tx_id, tx_start, voltage, default_phases)
            off = int(round((t - now).total_seconds()))
            if out and out[-1][1] == e.limit_a and out[-1][2] == e.phases:
                continue
            out.append((off, e.limit_a, e.phases))
        return out
