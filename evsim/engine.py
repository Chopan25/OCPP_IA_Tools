"""Engine: ejecuta un programa .evs contra los cargadores virtuales y arma el resultado de la corrida.

Responsabilidades:
  * crear los ChargePoint, correr el ticker de fisica (0.25 s) y medir el sitio (suma por fase),
  * interpretar cada verbo del script,
  * evaluar EXPECT_* (con plazo) y ASSERT_ALWAYS (cada tick, con gracia),
  * registrar todo en el Recorder.
"""
import asyncio
import json
import random
import re

from .charger import ChargePoint, OcppCallError, PHASES, ScriptRuntimeError
from .recorder import Recorder
from .script import OPS, ScriptError, Step, interpolate, parse_target
from .util import bold, dim, green, parse_duration, red, safe_eval, to_number, yellow

TICK = 0.25


class AbortScript(Exception):
    """Corta la ejecucion del script (modo strict ante un fallo, o error fatal)."""


def compare(op, got, want, tol=0.0):
    if got is None:
        return False
    if op == "==":
        return abs(got - want) <= tol
    if op == "!=":
        return abs(got - want) > tol
    if op == "<=":
        return got <= want + tol
    if op == ">=":
        return got >= want - tol
    if op == "<":
        return got < want + tol
    if op == ">":
        return got > want - tol
    raise ValueError("operador " + op)


def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return "%.2f" % v
    return str(v)


class Invariant:
    def __init__(self, id, metric, op, value, tol, grace, step):
        self.id, self.metric, self.op, self.value, self.tol, self.grace = id, metric, op, value, tol, grace
        self.step = step
        self.cur = None            # violacion abierta
        self.done = []             # violaciones cerradas
        self.active = True
        self.evals = 0

    def text(self):
        return "%s %s %s (tol %s, gracia %ss)" % (self.metric, self.op, fmt(self.value), fmt(self.tol), fmt(self.grace))


class Engine:
    def __init__(self, cfg, program, out_dir, script_path, mode="strict", seed=None, verbose=True):
        self.cfg = cfg
        self.program = program
        self.script_path = script_path
        self.mode = mode
        self.seed = 1 if seed is None else seed
        self.seed_forced = seed is not None
        self.verbose = verbose
        self.rec = Recorder(out_dir)
        self.out_dir = out_dir
        self.name = None
        self.description = None
        self.site_limit = cfg.site_limit_a
        self.tol = 0.5
        self.default_within = 10.0
        self.vars = {}
        self.cps = {}
        self.invariants = {}
        self.bg = []
        self._abort = asyncio.Event()
        self._abort_reason = None
        self._ticker = None
        self.fatal = None
        self.last_run_step = None
        # estado del sitio (actualizado por el ticker)
        self.site = {"measured": 0.0, "actual": 0.0, "limit": 0.0}
        self._site_peak_over = 0.0
        self._site_over_s = 0.0
        self.ideal_wh = 0.0
        self.delivered_wh = 0.0
        self.site_samples = []   # (t, measured, actual, limit)
        self._next_sample = 0.0
        self._ticks = 0
        self.checks_total = 0
        self.checks_failed = 0
        self.t_start_wall = None

    # ------------------------------------------------------------------ consola
    def say(self, text):
        if self.verbose:
            print(text, flush=True)

    def stamp(self):
        return dim("[%7.2fs]" % self.rec.now())

    # ------------------------------------------------------------------ util de ejecucion
    async def sleep(self, seconds):
        if seconds <= 0:
            if self._abort.is_set():
                raise AbortScript(self._abort_reason)
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._abort.wait(), seconds)
        except asyncio.TimeoutError:
            return
        raise AbortScript(self._abort_reason)

    def abort(self, reason):
        if not self._abort.is_set():
            self._abort_reason = reason
            self._abort.set()

    def num(self, tok, step, what="valor"):
        try:
            v = to_number(tok, {k: v for k, v in self.vars.items() if isinstance(v, (int, float))})
        except ValueError as e:
            raise ScriptError("%s: %s numerico invalido %r (%s)" % (step.verb, what, tok, e), step.file, step.line)
        return float(v)

    def dur(self, tok, step):
        return float(self.num(tok, step, "duracion"))

    def targets(self, toks, step, connectors=True):
        """['A', 'B.2', 'ALL'] -> [(cp, connector_id)]; con connectors=False -> [(cp, None)] sin duplicados."""
        out, seen = [], set()
        for t in toks:
            try:
                cid, n = parse_target(t)
            except ValueError as e:
                raise ScriptError("%s: %s" % (step.verb, e), step.file, step.line)
            if cid == "ALL":
                lst = [(cp, None) for cp in self.cps.values()]
                if connectors:
                    lst = [(cp, k) for cp in self.cps.values() for k in cp.connectors]
            else:
                cp = self.cps.get(cid)
                if cp is None:
                    raise ScriptError("%s: cargador desconocido %r" % (step.verb, cid), step.file, step.line)
                lst = [(cp, n if connectors else None)]
                if connectors and n not in cp.connectors:
                    raise ScriptError("%s: %s no tiene el conector %d" % (step.verb, cid, n), step.file, step.line)
            for cp, k in lst:
                if (cp.id, k) not in seen:
                    seen.add((cp.id, k))
                    out.append((cp, k))
        return out

    def one(self, tok, step):
        t = self.targets([tok], step)
        if len(t) != 1:
            raise ScriptError("%s: se esperaba un solo cargador/conector, no %s" % (step.verb, tok), step.file, step.line)
        return t[0]

    def stim(self, step, text, charger=None):
        self.rec.log("script", charger, text, stimulus=True)
        self.say("%s %s %s" % (self.stamp(), dim(step.where.ljust(14)), text))

    # ------------------------------------------------------------------ ticker y medicion del sitio
    async def _tick_loop(self):
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(TICK)
            now = loop.time()
            dt = now - last
            last = now
            for cp in self.cps.values():
                cp.tick(dt)
            self._site_update(dt)

    def _site_update(self, dt):
        meas = {p: 0.0 for p in PHASES}
        nom = {p: 0.0 for p in PHASES}
        lim = {p: 0.0 for p in PHASES}
        ideal = {p: 0.0 for p in PHASES}
        ideal_power = 0.0
        delivered_power = 0.0
        for cp in self.cps.values():
            for c in cp.connectors.values():
                m = cp.phase_load(c)
                n = cp.phase_load(c, nominal=True)
                for p in PHASES:
                    meas[p] += m[p]
                    nom[p] += n[p]
                delivered_power += c.vehicle.current_a * cp.cfg.voltage * len(cp._phase_map(c.phases_used))
                if c.tx is not None and not c.fault and not c.inoperative:
                    for p in cp._phase_map(c.phases_used):
                        lim[p] += c.limit_a
                    want = min(c.vehicle.wants_a(), cp.cfg.max_a) if c.vehicle.plugged else 0.0
                    if want >= c.vehicle.min_a:
                        for p in cp._phase_map(cp.cfg.phases):
                            ideal[p] += want
                        ideal_power += want * cp.cfg.voltage * len(cp._phase_map(cp.cfg.phases))
        top = max(ideal.values())
        f = min(1.0, self.site_limit / top) if top > 0 else 1.0
        self.ideal_wh += ideal_power * f * dt / 3600.0
        self.delivered_wh += delivered_power * dt / 3600.0
        s = self.site
        s["measured"], s["actual"], s["limit"] = max(meas.values()), max(nom.values()), max(lim.values())
        over = s["measured"] - self.site_limit
        if over > 0:
            self._site_over_s += dt
            self._site_peak_over = max(self._site_peak_over, over)
        self._ticks += 1
        t = self.rec.now()
        self._eval_invariants(t)
        if t >= self._next_sample:
            self._next_sample = t + 1.0
            self.site_samples.append((round(t, 2), round(s["measured"], 3), round(s["actual"], 3), round(s["limit"], 3)))
            for cp in self.cps.values():
                for c in cp.connectors.values():
                    ph = [v for v in c.measured.values() if v]
                    self.rec.samples.append({
                        "t": round(t, 2), "charger": cp.id, "connector": c.id, "status": c.status,
                        "plugged": int(c.vehicle.plugged), "tx": c.tx.id if c.tx else "",
                        "demand_a": round(c.vehicle.demand_a, 2), "limit_a": round(c.limit_a, 2),
                        "actual_a": round(c.vehicle.current_a, 2),
                        "measured_a": round(sum(ph) / len(ph), 2) if ph else 0.0,
                        "energy_wh": round(c.meter_wh, 1),
                        "connected": int(cp.boot_ok.is_set()),
                    })

    # ---- metricas y invariantes
    def metric(self, name, step=None):
        n = name.upper()
        if n == "SUM_ACTUAL":
            return self.site["actual"]
        if n == "SUM_MEASURED":
            return self.site["measured"]
        if n == "SUM_LIMIT":
            return self.site["limit"]
        m = re.match(r"^(ACTUAL|MEASURED|LIMIT)\.(.+)$", name, re.I)
        if m:
            kind = m.group(1).upper()
            cp, k = self.one(m.group(2), step or Step("", 0, "ASSERT_ALWAYS", [], {}, ""))
            return self.conn_value(kind, cp, k)
        raise ScriptError("metrica desconocida: %s (SUM_ACTUAL, SUM_MEASURED, SUM_LIMIT, ACTUAL.x, MEASURED.x, LIMIT.x)" % name,
                          getattr(step, "file", None), getattr(step, "line", None))

    def conn_value(self, kind, cp, k):
        c = cp.connectors[k]
        if kind == "LIMIT":
            return c.limit_a
        if kind == "ACTUAL":
            return c.vehicle.current_a
        ph = [v for v in c.measured.values() if v]
        return sum(ph) / len(ph) if ph else 0.0

    def _eval_invariants(self, t):
        for inv in self.invariants.values():
            if not inv.active:
                continue
            val = self.metric(inv.metric, inv.step)
            ok = compare(inv.op, val, inv.value, inv.tol)
            inv.evals += 1
            if not ok:
                if inv.cur is None:
                    inv.cur = {"start": round(t, 2), "end": round(t, 2), "peak": val, "worst_gap": 0.0}
                inv.cur["end"] = round(t, 2)
                gap = abs(val - inv.value)
                if gap >= inv.cur["worst_gap"]:
                    inv.cur["worst_gap"], inv.cur["peak"] = gap, val
                dur = inv.cur["end"] - inv.cur["start"] + TICK
                if dur > inv.grace and not inv.cur.get("flagged"):
                    inv.cur["flagged"] = True
                    self.checks_failed += 1
                    self.rec.log("fail", None, "ASSERT_ALWAYS %s violada: %s=%s (esperado %s %s)" % (
                        inv.id, inv.metric, fmt(val), inv.op, fmt(inv.value)))
                    self.say("%s %s %s" % (self.stamp(), red("FAIL"), "invariante %s violada mas alla de la gracia: %s = %s" % (inv.id, inv.metric, fmt(val))))
                    if self.mode == "strict":
                        self.abort("invariante %s violada (%s = %s)" % (inv.id, inv.metric, fmt(val)))
            elif inv.cur is not None:
                inv.done.append(inv.cur)
                inv.cur = None

    def _close_invariants(self):
        for inv in self.invariants.values():
            if inv.cur is not None:
                inv.done.append(inv.cur)
                inv.cur = None

    # ------------------------------------------------------------------ ejecucion
    async def run(self):
        import time
        from .util import utcnow
        self.t_start_wall = utcnow()
        for s in self.program:           # SEED se aplica antes de crear los cargadores
            if s.verb == "SEED" and not self.seed_forced:
                try:
                    self.seed = int(float(s.args[0]))
                except ValueError:
                    pass
        for cid, ccfg in self.cfg.chargers.items():
            self.cps[cid] = ChargePoint(ccfg, self.cfg, self.rec, seed=self.seed)
        self._ticker = asyncio.create_task(self._tick_loop())
        self.say(bold("evsim") + " | script: %s | config: %s | modo: %s | seed: %s" % (
            self.script_path, self.cfg.path, self.mode, self.seed))
        try:
            await self.exec_steps(self.program)
            await self._finish_bg()
        except AbortScript as e:
            self.say("%s %s %s" % (self.stamp(), red("ABORTADO"), e))
            self.rec.log("abort", None, str(e))
        except ScriptError as e:
            self.fatal = str(e)
            self.say("%s %s %s" % (self.stamp(), red("ERROR DE SCRIPT"), e))
            self.rec.log("error", None, str(e))
        except ScriptRuntimeError as e:
            self.fatal = "%s: %s" % (self.last_run_step.where if self.last_run_step else "?", e)
            self.say("%s %s %s" % (self.stamp(), red("ERROR"), self.fatal))
            self.rec.log("error", None, self.fatal)
        except asyncio.CancelledError:
            raise
        finally:
            await self._cleanup()
        return self.result()

    async def _finish_bg(self):
        pending = [t for t in self.bg if not t.done()]
        if pending:
            self.rec.notes.append("el script termino con %d llegada(s) aleatoria(s) sin completar (se cancelaron); usa WAIT_ARRIVALS" % len(pending))
            self.say("%s %s" % (self.stamp(), yellow("aviso: llegadas aleatorias sin terminar al final del script (usar WAIT_ARRIVALS)")))

    async def _cleanup(self):
        for t in self.bg:
            t.cancel()
        self._close_invariants()
        for cp in self.cps.values():
            try:
                await asyncio.wait_for(cp.close(), 5)
            except Exception:
                pass
        if self._ticker:
            self._ticker.cancel()
            try:
                await self._ticker
            except (asyncio.CancelledError, Exception):
                pass
        self.rec.close()

    async def exec_steps(self, steps):
        for st in steps:
            if self._abort.is_set():
                raise AbortScript(self._abort_reason)
            await self.exec_step(st)

    def _resolve(self, st):
        where = (st.file, st.line)
        args = [interpolate(a, self.vars, where) for a in st.args]
        kw = {k: interpolate(v, self.vars, where) for k, v in st.kwargs.items()}
        return args, kw

    async def exec_step(self, st):
        self.last_run_step = st
        verb = st.verb
        if verb == "REPEAT":
            args, kw = self._resolve(st)
            n = int(self.num(args[0], st, "cantidad"))
            for i in range(1, n + 1):
                if "var" in kw:
                    self.vars[kw["var"].lower()] = i
                await self.exec_steps(st.body)
            return
        if verb == "FOR":
            args, _ = self._resolve(st)
            items = []
            for tok in args[2:]:
                m = re.match(r"^(-?\d+)\.\.(-?\d+)$", tok)
                if m:
                    a, b = int(m.group(1)), int(m.group(2))
                    items.extend(range(a, b + 1) if a <= b else range(a, b - 1, -1))
                else:
                    items.append(tok)
            for it in items:
                self.vars[args[0].lower()] = it
                await self.exec_steps(st.body)
            return
        args, kw = self._resolve(st)
        fn = getattr(self, "v_" + verb.lower(), None)
        if fn is None:
            raise ScriptError("verbo sin implementar: %s" % verb, st.file, st.line)
        try:
            await fn(st, args, kw)
        except (OcppCallError, ConnectionError, asyncio.TimeoutError) as e:
            raise ScriptRuntimeError("%s fallo: %s" % (verb, e))

    # ------------------------------------------------------------------ verbos: declaraciones
    async def v_scenario(self, st, a, kw):
        self.name = a[0]
        self.say(bold("Escenario: %s" % self.name))

    async def v_description(self, st, a, kw):
        self.description = a[0]

    async def v_seed(self, st, a, kw):
        pass  # aplicado antes de crear los cargadores

    async def v_mode(self, st, a, kw):
        self.mode = a[0].lower()

    async def v_set_site_limit(self, st, a, kw):
        self.site_limit = self.num(a[0], st, "limite")
        self.rec.log("script", None, "limite del sitio = %s A" % fmt(self.site_limit))

    async def v_tolerance(self, st, a, kw):
        self.tol = self.num(a[0], st, "tolerancia")

    async def v_default_within(self, st, a, kw):
        self.default_within = self.dur(a[0], st)

    async def v_set(self, st, a, kw):
        items = kw.items() if kw else [(a[0].lower(), a[1])]
        for k, v in items:
            try:
                self.vars[k.lower()] = to_number(v, {n: x for n, x in self.vars.items() if isinstance(x, (int, float))})
            except ValueError:
                self.vars[k.lower()] = v

    async def v_print(self, st, a, kw):
        txt = a[0] if a else ""
        self.rec.log("print", None, txt)
        self.say("%s %s %s" % (self.stamp(), dim(st.where.ljust(14)), txt))

    async def v_mark(self, st, a, kw):
        self.rec.log("mark", None, a[0])
        self.say("%s %s %s" % (self.stamp(), dim(st.where.ljust(14)), bold("# " + a[0])))

    async def v_snapshot(self, st, a, kw):
        self.rec.snapshots.append({
            "t": round(self.rec.now(), 2), "name": a[0] if a else "snapshot%d" % (len(self.rec.snapshots) + 1),
            "site": {k: round(v, 3) for k, v in self.site.items()},
            "chargers": {cid: cp.snapshot() for cid, cp in self.cps.items()}})
        self.rec.log("snapshot", None, a[0] if a else "snapshot")

    # ------------------------------------------------------------------ verbos: ciclo de vida
    async def v_connect(self, st, a, kw):
        tmo = self.dur(kw["timeout"], st) if "timeout" in kw else 15.0
        cps = [cp for cp, _ in self.targets(a, st, connectors=False)]
        self.stim(st, "CONNECT %s" % " ".join(cp.id for cp in cps))
        await asyncio.gather(*[cp.start(tmo) for cp in cps])
        self.say("%s %s %s" % (self.stamp(), dim(st.where.ljust(14)), green("conectado(s): " + " ".join(cp.id for cp in cps))))

    async def v_disconnect(self, st, a, kw):
        cps = [cp for cp, _ in self.targets(a, st, connectors=False)]
        self.stim(st, "DISCONNECT %s" % " ".join(cp.id for cp in cps))
        await asyncio.gather(*[cp.close() for cp in cps])

    async def v_drop_link(self, st, a, kw):
        f = self.dur(kw["for"], st) if "for" in kw else 0.0
        for cp, _ in self.targets(a, st, connectors=False):
            self.stim(st, "DROP_LINK %s%s" % (cp.id, " for=%gs" % f if f else ""), cp.id)
            cp.drop_link(f)

    # ------------------------------------------------------------------ verbos: vehiculo
    async def v_plug(self, st, a, kw):
        for cp, k in self.targets(a, st):
            self.stim(st, "PLUG %s.%d" % (cp.id, k), cp.id)
            cp.plug(k)

    async def v_unplug(self, st, a, kw):
        for cp, k in self.targets(a, st):
            self.stim(st, "UNPLUG %s.%d" % (cp.id, k), cp.id)
            cp.unplug(k)

    async def v_start(self, st, a, kw):
        cp, k = self.one(a[0], st)
        self.stim(st, "START %s.%d idtag=%s" % (cp.id, k, kw.get("idtag", cp.cfg.id_tag)), cp.id)
        r = await cp.start_tx(k, kw.get("idtag"))
        if r != "Started":
            self.rec.notes.append("%s START -> %s" % (st.where, r))
            self.say("%s %s %s" % (self.stamp(), yellow("aviso"), "START %s.%d -> %s" % (cp.id, k, r)))

    async def v_stop(self, st, a, kw):
        has_all = any(t.upper() == "ALL" for t in a)
        for cp, k in self.targets(a, st):
            if has_all and cp.connectors[k].tx is None:
                continue          # STOP ALL solo toca lo que esta cargando
            self.stim(st, "STOP %s.%d" % (cp.id, k), cp.id)
            cp.stop_tx(k, kw.get("reason", "Local"))

    async def v_demand(self, st, a, kw):
        cp, k = self.one(a[0], st)
        amps = self.num(a[1], st, "amperes")
        over = self.dur(kw["over"], st) if "over" in kw else 0.0
        self.stim(st, "DEMAND %s.%d %gA%s" % (cp.id, k, amps, " over %gs" % over if over else ""), cp.id)
        cp.set_demand(k, amps, over)

    async def v_fault(self, st, a, kw):
        cp, k = self.one(a[0], st)
        self.stim(st, "FAULT %s.%d" % (cp.id, k), cp.id)
        cp.set_fault(k, kw.get("code", "OtherError"))

    async def v_clear_fault(self, st, a, kw):
        for cp, k in self.targets(a, st):
            self.stim(st, "CLEAR_FAULT %s.%d" % (cp.id, k), cp.id)
            cp.clear_fault(k)

    async def v_send(self, st, a, kw):
        cp, _ = self.one(a[0], st)
        payload = {}
        if len(a) > 2:
            try:
                payload = json.loads(a[2])
            except ValueError as e:
                raise ScriptError("SEND: JSON invalido: %s" % e, st.file, st.line)
        self.stim(st, "SEND %s %s" % (cp.id, a[1]), cp.id)
        res = await cp.call(a[1], payload)
        self.say("%s %s respuesta: %s" % (self.stamp(), dim(st.where.ljust(14)), json.dumps(res)[:200]))

    # ------------------------------------------------------------------ verbos: tiempo
    async def v_wait(self, st, a, kw):
        d = self.dur(a[0], st)
        self.say("%s %s WAIT %gs" % (self.stamp(), dim(st.where.ljust(14)), d))
        await self.sleep(d)

    async def v_wait_until_stable(self, st, a, kw):
        quiet = self.dur(kw["for"], st) if "for" in kw else 5.0
        tmo = self.dur(kw["timeout"], st) if "timeout" in kw else 60.0
        tg = self.targets(a, st)
        t0 = self.rec.now()

        def sig():
            return [(round(c.limit_a, 2), round(c.vehicle.current_a, 1)) for cp, k in tg for c in [cp.connectors[k]]]
        last, last_change = sig(), self.rec.now()
        while True:
            await self.sleep(0.2)
            cur = sig()
            if cur != last:
                last, last_change = cur, self.rec.now()
            if self.rec.now() - last_change >= quiet:
                break
            if self.rec.now() - t0 >= tmo:
                self.record_check(st, "WAIT_UNTIL_STABLE %s" % " ".join(a), False, "estable %gs" % quiet,
                                  "sigue cambiando tras %gs" % tmo, tmo, None)
                return
        el = self.rec.now() - t0
        self.record_check(st, "WAIT_UNTIL_STABLE %s" % " ".join(a), True, "estable %gs" % quiet, "estable", el, None)

    async def v_wait_msg(self, st, a, kw):
        cp, _ = self.one(a[0], st)
        n = int(self.num(kw.get("count", 1), st))
        tmo = self.dur(kw["timeout"], st) if "timeout" in kw else self.default_within
        base = self.rec.count(cp.id, a[1])
        t0 = self.rec.now()
        while self.rec.count(cp.id, a[1]) - base < n:
            if self.rec.now() - t0 >= tmo:
                self.record_check(st, "WAIT_MSG %s %s" % (cp.id, a[1]), False, "%d mensaje(s)" % n,
                                  "%d" % (self.rec.count(cp.id, a[1]) - base), tmo, None)
                return
            await self.sleep(0.1)
        self.say("%s %s WAIT_MSG %s %s recibido (%.1fs)" % (self.stamp(), dim(st.where.ljust(14)), cp.id, a[1], self.rec.now() - t0))

    async def v_wait_arrivals(self, st, a, kw):
        tmo = self.dur(kw["timeout"], st) if "timeout" in kw else 3600.0
        pend = [t for t in self.bg if not t.done()]
        if not pend:
            return
        self.say("%s %s esperando %d llegada(s) pendiente(s)..." % (self.stamp(), dim(st.where.ljust(14)), len(pend)))
        done, still = await asyncio.wait(pend, timeout=tmo)
        for t in done:
            if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
                raise t.exception()
        if still:
            raise ScriptRuntimeError("WAIT_ARRIVALS: %d llegada(s) sin terminar en %gs" % (len(still), tmo))

    # ------------------------------------------------------------------ verificaciones
    def record_check(self, st, text, ok, expected, got, elapsed, since):
        self.checks_total += 1
        if not ok:
            self.checks_failed += 1
        rec = {"file": st.file, "line": st.line, "where": st.where, "text": text, "ok": bool(ok),
               "expected": expected, "got": got, "waited_s": round(elapsed, 2),
               "since_stimulus_s": None if since is None else round(since, 2), "t": round(self.rec.now(), 2)}
        self.rec.expectations.append(rec)
        mark = green("PASS") if ok else red("FAIL")
        extra = ""
        if ok and since is not None:
            extra = dim(" (%.1fs tras el ultimo evento)" % since)
        if not ok:
            extra = " esperado %s, obtenido %s" % (expected, got)
        self.say("%s %s %s %s%s" % (self.stamp(), dim(st.where.ljust(14)), mark, text, extra))
        if not ok:
            self.rec.log("fail", None, "%s: %s (esperado %s, obtenido %s)" % (st.where, text, expected, got))
            if self.mode == "strict":
                self.abort("%s: %s" % (st.where, text))
                raise AbortScript(self._abort_reason)

    async def check(self, st, text, getter, op, want, within, tol):
        """Espera hasta 'within' s a que compare(op, getter(), want, tol) sea verdadero."""
        t0 = self.rec.now()
        stim = self.rec.last_stimulus_t
        got = None
        while True:
            got = getter()
            ok = compare(op, got, want, tol)
            if ok or self.rec.now() - t0 >= within:
                break
            await self.sleep(0.1)
        now = self.rec.now()
        since = (now - stim) if (ok and stim is not None) else None
        self.record_check(st, text, ok, "%s %s (tol %s)" % (op, fmt(want), fmt(tol)), fmt(got), now - t0, since)

    async def check_eq(self, st, text, getter, want, within):
        t0 = self.rec.now()
        stim = self.rec.last_stimulus_t
        while True:
            got = getter()
            ok = got == want
            if ok or self.rec.now() - t0 >= within:
                break
            await self.sleep(0.1)
        now = self.rec.now()
        self.record_check(st, text, ok, str(want), str(got), now - t0, (now - stim) if (ok and stim is not None) else None)

    def _within(self, kw, st):
        return self.dur(kw["within"], st) if "within" in kw else self.default_within

    def _tol(self, kw, st):
        return self.num(kw["tol"], st, "tol") if "tol" in kw else self.tol

    async def _exp_conn(self, st, a, kw, kind):
        cp, k = self.one(a[0], st)
        want = self.num(a[2], st)
        await self.check(st, "%s %s.%d %s %s" % (st.verb, cp.id, k, a[1], fmt(want)),
                         lambda: self.conn_value(kind, cp, k), a[1], want, self._within(kw, st), self._tol(kw, st))

    async def v_expect_limit(self, st, a, kw):
        await self._exp_conn(st, a, kw, "LIMIT")

    async def v_expect_actual(self, st, a, kw):
        await self._exp_conn(st, a, kw, "ACTUAL")

    async def v_expect_measured(self, st, a, kw):
        await self._exp_conn(st, a, kw, "MEASURED")

    def _sum(self, kind, of):
        cps = [cp for cp, _ in of] if of else list(self.cps.values())
        tot = {p: 0.0 for p in PHASES}
        for cp in cps:
            for c in cp.connectors.values():
                if kind == "LIMIT":
                    if c.tx is not None:
                        for p in cp._phase_map(c.phases_used):
                            tot[p] += c.limit_a
                else:
                    ld = cp.phase_load(c, nominal=True)
                    for p in PHASES:
                        tot[p] += ld[p]
        return max(tot.values())

    async def _exp_sum(self, st, op, val, kw, kind):
        of = self.targets(kw["of"].split(","), st, connectors=False) if "of" in kw else None
        want = self.num(val, st)
        await self.check(st, "%s %s %s%s" % (st.verb, op, fmt(want), " of=" + kw["of"] if of else ""),
                         lambda: self._sum(kind, of), op, want, self._within(kw, st), self._tol(kw, st))

    async def v_expect_sum_actual(self, st, a, kw):
        await self._exp_sum(st, a[0], a[1], kw, "ACTUAL")

    async def v_expect_sum_limit(self, st, a, kw):
        await self._exp_sum(st, a[0], a[1], kw, "LIMIT")

    async def v_expect_sum_le(self, st, a, kw):
        await self._exp_sum(st, "<=", a[0], kw, "ACTUAL")

    async def v_expect_state(self, st, a, kw):
        cp, k = self.one(a[0], st)
        await self.check_eq(st, "EXPECT_STATE %s.%d == %s" % (cp.id, k, a[1]),
                            lambda: cp.connectors[k].status, a[1], self._within(kw, st))

    async def v_expect_tx(self, st, a, kw):
        cp, k = self.one(a[0], st)
        want = a[1].lower()
        if want not in ("active", "none"):
            raise ScriptError("EXPECT_TX: usar active o none", st.file, st.line)
        await self.check_eq(st, "EXPECT_TX %s.%d %s" % (cp.id, k, want),
                            lambda: "active" if cp.connectors[k].tx is not None else "none", want, self._within(kw, st))

    async def v_expect_connected(self, st, a, kw):
        cp, _ = self.one(a[0], st)
        await self.check_eq(st, "EXPECT_CONNECTED %s" % cp.id, lambda: cp.boot_ok.is_set(), True, self._within(kw, st))

    async def v_expect_disconnected(self, st, a, kw):
        cp, _ = self.one(a[0], st)
        await self.check_eq(st, "EXPECT_DISCONNECTED %s" % cp.id, lambda: not cp.boot_ok.is_set(), True, self._within(kw, st))

    async def v_expect_msg(self, st, a, kw):
        cp, _ = self.one(a[0], st)
        n = int(self.num(kw.get("count", 1), st))
        base = self.rec.count(cp.id, a[1])
        await self.check(st, "EXPECT_MSG %s %s x%d" % (cp.id, a[1], n),
                         lambda: float(self.rec.count(cp.id, a[1]) - base), ">=", float(n), self._within(kw, st), 0.0)

    async def v_expect_no_msg(self, st, a, kw):
        cp, _ = self.one(a[0], st)
        d = self.dur(kw["for"], st) if "for" in kw else self.default_within
        base = self.rec.count(cp.id, a[1])
        await self.sleep(d)
        got = self.rec.count(cp.id, a[1]) - base
        self.record_check(st, "EXPECT_NO_MSG %s %s durante %gs" % (cp.id, a[1], d), got == 0, "0 mensajes", "%d" % got, d, None)

    async def v_expect_no_profile(self, st, a, kw):
        cp, _ = self.one(a[0], st)
        await self.check_eq(st, "EXPECT_NO_PROFILE %s" % cp.id, lambda: len(cp.store.items), 0, self._within(kw, st))

    # ------------------------------------------------------------------ invariantes
    async def v_assert_always(self, st, a, kw):
        metric, op, val = a[0], a[1], self.num(a[2], st)
        self.metric(metric, st)  # valida el nombre de la metrica
        iid = kw.get("id") or "inv%d" % (len(self.invariants) + 1)
        grace = self.dur(kw["grace"], st) if "grace" in kw else 0.0
        tol = self.num(kw["tol"], st) if "tol" in kw else 0.0
        self.invariants[iid] = Invariant(iid, metric.upper() if "." not in metric else metric, op, val, tol, grace, st)
        self.say("%s %s ASSERT_ALWAYS %s: %s" % (self.stamp(), dim(st.where.ljust(14)), iid, self.invariants[iid].text()))

    async def v_assert_stop(self, st, a, kw):
        inv = self.invariants.get(a[0])
        if inv is None:
            raise ScriptError("ASSERT_STOP: no existe la invariante %r" % a[0], st.file, st.line)
        if inv.cur is not None:
            inv.done.append(inv.cur)
            inv.cur = None
        inv.active = False

    # ------------------------------------------------------------------ llegadas aleatorias
    async def v_random_arrivals(self, st, a, kw):
        n = int(self.num(kw.get("n", 5), st))
        over = self.dur(kw["over"], st) if "over" in kw else 60.0
        seed = int(self.num(kw["seed"], st)) if "seed" in kw else self.seed
        names = [x.strip() for x in kw["chargers"].split(",")] if "chargers" in kw else list(self.cps)
        cps = [cp for cp, _ in self.targets(names, st, connectors=False)]

        def rng_range(txt, default, parse):
            lo_hi = txt.split("..") if txt else default
            lo, hi = parse(lo_hi[0]), parse(lo_hi[-1])
            return min(lo, hi), max(lo, hi)
        dlo, dhi = rng_range(kw.get("demand"), ["10", "32"], lambda x: self.num(x, st))
        tlo, thi = rng_range(kw.get("duration"), ["30s", "120s"], lambda x: self.dur(x, st))
        rng = random.Random("arrivals:%s" % seed)
        plan = []
        for t in sorted(rng.uniform(0, over) for _ in range(n)):
            cp = rng.choice(cps)
            k = rng.choice(list(cp.connectors))
            plan.append((round(t, 1), cp, k, round(rng.uniform(dlo, dhi) * 2) / 2.0, round(rng.uniform(tlo, thi))))
        self.stim(st, "RANDOM_ARRIVALS n=%d over=%gs seed=%s" % (n, over, seed))
        for t, cp, k, d, dur in plan:
            self.rec.log("plan", cp.id, "llegada t+%gs %s.%d demanda %gA duracion %gs" % (t, cp.id, k, d, dur))
        idtag = kw.get("idtag")
        t_base = self.rec.now()

        async def arrival(t, cp, k, d, dur):
            await asyncio.sleep(max(0.0, t_base + t - self.rec.now()))
            c = cp.connectors[k]
            if c.vehicle.plugged or c.tx is not None or c.fault or not cp.boot_ok.is_set():
                self.rec.notes.append("llegada en %s.%d a t+%gs omitida (conector ocupado o cargador sin conexion)" % (cp.id, k, t))
                self.rec.log("script", cp.id, "llegada omitida %s.%d (ocupado)" % (cp.id, k))
                return
            self.rec.log("script", cp.id, "llegada %s.%d demanda %gA por %gs" % (cp.id, k, d, dur), stimulus=True)
            self.say("%s %s llegada %s.%d demanda %gA por %gs" % (self.stamp(), dim(st.where.ljust(14)), cp.id, k, d, dur))
            c.vehicle.set_demand(d)
            cp.plug(k)
            try:
                await cp.start_tx(k, idtag)
            except ScriptRuntimeError as e:
                self.rec.notes.append("llegada %s.%d: %s" % (cp.id, k, e))
                return
            await asyncio.sleep(dur)
            if c.tx is not None:
                self.rec.log("script", cp.id, "fin de carga %s.%d" % (cp.id, k), stimulus=True)
                cp.stop_tx(k, "Local")
            await asyncio.sleep(1.0)
            cp.unplug(k)
            c.vehicle.set_demand(0.0)

        for item in plan:
            self.bg.append(asyncio.create_task(arrival(*item)))

    # ------------------------------------------------------------------ resultado
    def result(self):
        from .util import iso
        inv_out = []
        inv_failed = 0
        for inv in self.invariants.values():
            vio = [dict(v, duration=round(v["end"] - v["start"] + TICK, 2),
                        exceeds_grace=(v["end"] - v["start"] + TICK) > inv.grace) for v in inv.done]
            failed = any(v["exceeds_grace"] for v in vio)
            inv_failed += 1 if failed else 0
            inv_out.append({"id": inv.id, "metric": inv.metric, "op": inv.op, "value": inv.value, "tol": inv.tol,
                            "grace_s": inv.grace, "where": inv.step.where if inv.step.file else "", "violations": vio,
                            "failed": failed, "active_at_end": inv.active})
        exp_failed = sum(1 for e in self.rec.expectations if not e["ok"])
        total = len(self.rec.expectations) + len(self.invariants)
        failed = exp_failed + inv_failed
        status = "passed"
        if self.fatal:
            status = "error"
        elif failed:
            status = "failed"
        elif self._abort.is_set():
            status = "aborted"
        eff = (self.delivered_wh / self.ideal_wh) if self.ideal_wh > 0.01 else None
        return {
            "scenario": self.name or "(sin nombre)", "description": self.description,
            "script": self.script_path, "config": self.cfg.path, "mode": self.mode, "seed": self.seed,
            "started": iso(self.t_start_wall), "duration_s": round(self.rec.now(), 2),
            "site_limit_a": self.site_limit, "status": status, "error": self.fatal,
            "aborted": self._abort.is_set(), "abort_reason": self._abort_reason,
            "checks": {"total": total, "failed": failed, "passed": total - failed,
                       "expectations": len(self.rec.expectations), "invariants": len(self.invariants)},
            "invariants": inv_out,
            "expectations": self.rec.expectations,
            "metrics": {
                "peak_overload_a": round(self._site_peak_over, 2),
                "time_over_limit_s": round(self._site_over_s, 2),
                "delivered_wh": round(self.delivered_wh, 1),
                "ideal_wh": round(self.ideal_wh, 1),
                "efficiency": None if eff is None else round(eff, 3),
            },
            "notes": self.rec.notes,
            "snapshots": self.rec.snapshots,
        }
