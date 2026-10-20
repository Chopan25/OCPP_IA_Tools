"""Cargador virtual OCPP 1.6J: conexion WebSocket, mensajes, transacciones, MeterValues y smart charging.

Un ChargePoint = un chargeBoxId = una conexion WebSocket. Cada conector tiene su Vehicle.
Todo corre en el event loop de asyncio (un solo hilo); la fisica avanza con tick(dt) desde el engine.
"""
import asyncio
import base64
import json
import random
import ssl as ssl_mod
import uuid
from collections import deque

import websockets

from .config import SUPPORTED_MEASURANDS
from .initcfg import safe_url
from .smartcharging import ProfileStore
from .util import iso, utcnow
from .vehicle import Vehicle

PHASES = ("L1", "L2", "L3")


class OcppCallError(Exception):
    def __init__(self, code, desc="", details=None):
        super().__init__("%s: %s" % (code, desc))
        self.code = code
        self.desc = desc


class ScriptRuntimeError(Exception):
    """Error al ejecutar un comando del script (ej: START sin auto enchufado)."""


class Transaction:
    def __init__(self, id_tag, meter_start, connector_id):
        self.id = None
        self.id_tag = id_tag
        self.meter_start = meter_start
        self.connector_id = connector_id
        self.start_time = utcnow()
        self.started = asyncio.Event()
        self.denied = False


class Connector:
    def __init__(self, cid, cfg, rng):
        self.id = cid
        self.vehicle = Vehicle(cfg.vehicle, cfg.deviation, rng)
        self.status = "Available"
        self.tx = None
        self.fault = None            # codigo de error o None
        self.inoperative = False
        self.pending_inoperative = False
        self.pending_remote = None   # {"id_tag":..., "profile":...} esperando que enchufen
        self.last_status_sent = None
        self.meter_wh = float(cfg.meter.get("start_wh", 0))
        self.offered_a = 0.0
        self.limit_a = cfg.max_a     # limite efectivo resuelto (con tope fisico)
        self.limit_src = None
        self.phases_used = cfg.phases
        self.measured = {p: 0.0 for p in PHASES}   # A por fase, con ruido
        self.next_sample = None

    @property
    def tx_active(self):
        return self.tx is not None


class Queued:
    def __init__(self, action, build, on_result=None):
        self.action = action
        self.build = build
        self.on_result = on_result
        self.attempts = 0


class ChargePoint:
    def __init__(self, cfg, cs_cfg, rec, seed=0):
        self.cfg = cfg
        self.cs = cs_cfg
        self.rec = rec
        self.id = cfg.id
        self.rng = random.Random("%s:%s" % (seed, cfg.id))
        self.store = ProfileStore(cfg.smart)
        self.connectors = {i: Connector(i, cfg, self.rng) for i in range(1, cfg.connectors + 1)}
        self.ws = None
        self.boot_ok = asyncio.Event()
        self.connected = False
        self.queue = deque()
        self._qevt = asyncio.Event()
        self._pending = {}
        self._task = None
        self._meter_task = None
        self._closing = False
        self._block_until = 0.0
        self._drop_now = None
        self.config = self._make_config()
        self.last_error = None

    # ------------------------------------------------------------------ config OCPP
    def _make_config(self):
        c = self.cfg
        measurands = ",".join(c.meter.get("measurands") or [])
        # clave -> [valor, solo_lectura]
        return {
            "AllowOfflineTxForUnknownId": ["false", False],
            "AuthorizationCacheEnabled": ["false", False],
            "AuthorizeRemoteTxRequests": ["false", False],
            "ChargeProfileMaxStackLevel": [str(c.smart.get("max_stack_level", 8)), True],
            "ChargingScheduleAllowedChargingRateUnit": [str(c.smart.get("allowed_units", "Current,Power")), True],
            "ChargingScheduleMaxPeriods": [str(c.smart.get("max_periods", 24)), True],
            "ClockAlignedDataInterval": ["0", False],
            "ConnectionTimeOut": ["60", False],
            "GetConfigurationMaxKeys": ["50", True],
            "HeartbeatInterval": ["60", False],
            "LocalAuthorizeOffline": ["false", False],
            "LocalPreAuthorize": ["false", False],
            "MaxChargingProfilesInstalled": [str(c.smart.get("max_profiles", 20)), True],
            "MeterValuesAlignedData": ["", False],
            "MeterValuesSampledData": [measurands, False],
            "MeterValueSampleInterval": [str(int(c.meter.get("sample_interval_s", 10))), False],
            "NumberOfConnectors": [str(c.connectors), True],
            "ResetRetries": ["0", False],
            "StopTransactionOnEVSideDisconnect": ["true", False],
            "StopTransactionOnInvalidId": ["true", False],
            "SupportedFeatureProfiles": ["Core,SmartCharging,RemoteTrigger", True],
            "TransactionMessageAttempts": ["3", False],
            "TransactionMessageRetryInterval": ["10", False],
            "UnlockConnectorOnEVSideDisconnect": ["true", False],
        }

    def cfg_int(self, key, default=0):
        try:
            return int(float(self.config[key][0]))
        except (KeyError, ValueError):
            return default

    # ------------------------------------------------------------------ ciclo de vida
    async def start(self, timeout=15.0):
        """Abre la conexion y espera BootNotification Accepted."""
        self._closing = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="cp-" + self.id)
        if self._meter_task is None or self._meter_task.done():
            self._meter_task = asyncio.create_task(self.meter_loop(), name="mv-" + self.id)
        try:
            await asyncio.wait_for(self.boot_ok.wait(), timeout)
        except asyncio.TimeoutError:
            raise ScriptRuntimeError("%s: no se acepto el BootNotification en %.0fs%s" % (
                self.id, timeout, " (ultimo error: %s)" % self.last_error if self.last_error else ""))

    async def drain(self, timeout=3.0):
        """Espera a que se envie la cola (StopTransaction, MeterValues...) antes de cerrar."""
        end = asyncio.get_running_loop().time() + timeout
        while self.queue and self.connected and self.boot_ok.is_set() and asyncio.get_running_loop().time() < end:
            await asyncio.sleep(0.05)

    async def close(self):
        await self.drain()
        self._closing = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        if self._meter_task:
            self._meter_task.cancel()
            self._meter_task = None

    def drop_link(self, for_s=0.0):
        """Corta el WebSocket de golpe y no reconecta durante for_s segundos."""
        self._block_until = asyncio.get_running_loop().time() + for_s
        if self.ws is not None:
            asyncio.ensure_future(self._abort_ws(self.ws))
        self.rec.log("link", self.id, "enlace cortado%s" % (" por %.0fs" % for_s if for_s else ""))

    async def _abort_ws(self, ws):
        try:
            tr = ws.transport
            if tr is not None:
                tr.abort()
            else:
                await ws.close()
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    async def _run(self):
        loop = asyncio.get_running_loop()
        while not self._closing:
            wait = self._block_until - loop.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 0.5))
                continue
            try:
                await self._session()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - cualquier error de red => reconectar
                self.last_error = ("%s: %s" % (type(e).__name__, e)).replace(self.cfg.url, safe_url(self.cfg.url))
            finally:
                self.connected = False
                self.boot_ok.clear()
                self.ws = None
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("conexion perdida"))
                self._pending.clear()
            if self._closing or not self.cfg.auto_reconnect:
                break
            self.rec.log("link", self.id, "desconectado (%s); reintento en %.1fs" % (self.last_error, self.cfg.reconnect_s))
            await asyncio.sleep(self.cfg.reconnect_s)

    def _ssl(self):
        if not self.cfg.url.startswith("wss://"):
            return None
        ctx = ssl_mod.create_default_context()
        if not self.cs.tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl_mod.CERT_NONE
        return ctx

    async def _session(self):
        headers = {}
        if self.cfg.username or self.cfg.password:
            tok = base64.b64encode(("%s:%s" % (self.cfg.username, self.cfg.password)).encode()).decode()
            headers["Authorization"] = "Basic " + tok
        kw = dict(subprotocols=[self.cs.subprotocol], open_timeout=self.cs.connect_timeout_s,
                  ping_interval=None, max_size=2 ** 22)
        if headers:
            kw["additional_headers"] = headers
        ctx = self._ssl()
        if ctx is not None:
            kw["ssl"] = ctx
        async with websockets.connect(self.cfg.url, **kw) as ws:
            self.ws = ws
            self.connected = True
            self.rec.log("link", self.id, "conectado a %s" % safe_url(self.cfg.url))
            reader = asyncio.create_task(self._reader(ws))
            tasks = [reader]
            try:
                await self._boot()
                self._qevt.set()
                tasks += [asyncio.create_task(self._sender()), asyncio.create_task(self._heartbeat())]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in tasks:
                    t.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception) and not isinstance(r, (asyncio.CancelledError, websockets.ConnectionClosed)):
                        raise r
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _boot(self):
        while True:
            res = await self.call("BootNotification", {
                "chargePointVendor": self.cfg.vendor,
                "chargePointModel": self.cfg.model,
                "chargePointSerialNumber": self.cfg.serial,
                "chargeBoxSerialNumber": self.cfg.serial,
                "firmwareVersion": self.cfg.firmware,
            })
            status = res.get("status")
            interval = res.get("interval", 0)
            if status == "Accepted":
                if interval:
                    self.config["HeartbeatInterval"][0] = str(int(interval))
                self.boot_ok.set()
                self.rec.log("link", self.id, "BootNotification Accepted")
                # tras el boot: estado de todos los conectores (0 incluido); los StatusNotification que
                # quedaron en cola de antes quedan reemplazados por estos
                self.queue = deque(q for q in self.queue if q.action != "StatusNotification")
                self._enqueue_status(0, "Available", "NoError", force=True)
                for c in self.connectors.values():
                    c.last_status_sent = None
                    self._refresh_status(c, force=True)
                return
            self.rec.log("link", self.id, "BootNotification %s (reintento en %ss)" % (status, interval or 10))
            await asyncio.sleep(max(1, interval or 10))

    async def _heartbeat(self):
        while True:
            iv = self.cfg_int("HeartbeatInterval", 60)
            await asyncio.sleep(iv if iv > 0 else 5)
            if iv > 0:
                await self.call("Heartbeat", {})

    # ------------------------------------------------------------------ transporte
    async def _send(self, frame):
        text = json.dumps(frame, separators=(",", ":"))
        action = frame[2] if frame[0] == 2 else ("CALLRESULT" if frame[0] == 3 else "CALLERROR")
        self.rec.msg(self.id, "->", action, text)
        await self.ws.send(text)

    async def call(self, action, payload, timeout=30.0):
        if self.ws is None:
            raise ConnectionError("sin conexion")
        mid = uuid.uuid4().hex[:24]
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._send([2, mid, action, payload])
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def _reader(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if not isinstance(msg, list) or len(msg) < 3:
                    raise ValueError("frame invalido")
            except ValueError:
                self.rec.msg(self.id, "<-", "INVALID", str(raw)[:300])
                continue
            kind = msg[0]
            if kind == 2:
                self.rec.msg(self.id, "<-", msg[2], raw if isinstance(raw, str) else raw.decode(errors="replace"))
                await self._on_call(msg)
            elif kind == 3:
                self.rec.msg(self.id, "<-", "CALLRESULT", raw)
                fut = self._pending.get(msg[1])
                if fut and not fut.done():
                    fut.set_result(msg[2] if isinstance(msg[2], dict) else {})
            elif kind == 4:
                self.rec.msg(self.id, "<-", "CALLERROR", raw)
                fut = self._pending.get(msg[1])
                if fut and not fut.done():
                    fut.set_exception(OcppCallError(msg[2], msg[3] if len(msg) > 3 else ""))

    async def _on_call(self, msg):
        mid, action = msg[1], msg[2]
        payload = msg[3] if len(msg) > 3 and isinstance(msg[3], dict) else {}
        handler = getattr(self, "h_" + action, None)
        if handler is None:
            await self._send([4, mid, "NotImplemented", "Action %s no implementada" % action, {}])
            return
        try:
            out = handler(payload)
        except Exception as e:  # noqa: BLE001
            await self._send([4, mid, "InternalError", str(e), {}])
            return
        after = None
        if isinstance(out, tuple):
            out, after = out
        await self._send([3, mid, out])
        if after is not None:
            asyncio.ensure_future(self._run_after(after))

    async def _run_after(self, fn):
        try:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
        except Exception as e:  # noqa: BLE001
            self.rec.log("error", self.id, "accion diferida fallo: %s" % e)

    async def _sender(self):
        while True:
            await self._qevt.wait()
            self._qevt.clear()
            while self.queue:
                item = self.queue[0]
                try:
                    res = await self.call(item.action, item.build())
                except asyncio.TimeoutError:
                    item.attempts += 1
                    if item.attempts >= self.cfg_int("TransactionMessageAttempts", 3):
                        self.queue.popleft()
                        self.rec.log("error", self.id, "%s sin respuesta, descartado" % item.action)
                    continue
                except OcppCallError as e:
                    self.queue.popleft()
                    self.rec.log("error", self.id, "%s -> CALLERROR %s" % (item.action, e))
                    continue
                self.queue.popleft()
                if item.on_result:
                    item.on_result(res)

    def enqueue(self, action, build, on_result=None):
        self.queue.append(Queued(action, build, on_result))
        self._qevt.set()

    # ------------------------------------------------------------------ estado de conectores
    def _compute_status(self, c):
        if c.fault:
            return "Faulted"
        if c.inoperative:
            return "Unavailable"
        v = c.vehicle
        if c.tx is not None:
            return {"charging": "Charging", "suspended_evse": "SuspendedEVSE",
                    "suspended_ev": "SuspendedEV"}.get(v.state, "Preparing")
        if v.plugged:
            return "Finishing" if c.status in ("Finishing", "Charging", "SuspendedEV", "SuspendedEVSE") else "Preparing"
        return "Available"

    def _refresh_status(self, c, force=False):
        st = self._compute_status(c)
        c.status = st
        if force or st != c.last_status_sent:
            c.last_status_sent = st
            err = c.fault or "NoError"
            self.rec.log("status", self.id, "conector %d -> %s" % (c.id, st))
            self._enqueue_status(c.id, st, err)

    def _enqueue_status(self, cid, status, error, force=False):
        ts = iso()
        self.enqueue("StatusNotification", lambda: {
            "connectorId": cid, "errorCode": error, "status": status, "timestamp": ts})

    # ------------------------------------------------------------------ fisica / medicion
    def _phase_map(self, nph):
        if self.cfg.phases == 1:
            return (self.cfg.phase,)
        return PHASES[:max(1, min(3, nph))]

    def tick(self, dt):
        now = utcnow()
        for c in self.connectors.values():
            tx = c.tx
            eff = self.store.effective(c.id, now, tx is not None, tx.id if tx else None,
                                       tx.start_time if tx else None, self.cfg.voltage, self.cfg.phases)
            c.limit_a = self.cfg.max_a if eff.limit_a is None else min(self.cfg.max_a, eff.limit_a)
            c.limit_src = eff.source
            nph = min(self.cfg.phases, eff.phases or self.cfg.phases)
            c.phases_used = nph
            allowed = tx is not None and not c.fault and not c.inoperative and not tx.denied
            c.offered_a = c.limit_a
            c.vehicle.step(dt, c.offered_a, allowed, nph, self.cfg.voltage)
            meas = c.vehicle.measure()
            c.measured = {p: 0.0 for p in PHASES}
            for p in self._phase_map(nph):
                c.measured[p] = meas if c.vehicle.current_a > 0 else 0.0
            power = sum(c.measured.values()) * self.cfg.voltage
            c.meter_wh += power * dt / 3600.0
            self._refresh_status(c)

    def phase_load(self, c, nominal=False):
        """Corriente por fase del sitio que consume este conector."""
        if nominal:
            i = c.vehicle.current_a
            out = {p: 0.0 for p in PHASES}
            for p in self._phase_map(c.phases_used):
                out[p] = i
            return out
        return dict(c.measured)

    # ------------------------------------------------------------------ MeterValues
    def _sampled(self, c, context="Sample.Periodic"):
        wanted = [m.strip() for m in self.config["MeterValuesSampledData"][0].split(",") if m.strip()]
        sv = []

        def add(meas, value, unit, phase=None, loc="Outlet"):
            d = {"value": value, "context": context, "format": "Raw", "measurand": meas, "location": loc, "unit": unit}
            if phase:
                d["phase"] = phase
            sv.append(d)

        for m in wanted:
            if m not in SUPPORTED_MEASURANDS:
                continue
            nph = c.phases_used
            phases = self._phase_map(nph)
            if m == "Energy.Active.Import.Register":
                add(m, str(int(round(c.meter_wh))), "Wh")
            elif m == "Current.Import":
                for p in phases:
                    add(m, "%.2f" % c.measured[p], "A", p)
            elif m == "Current.Offered":
                add(m, "%.2f" % c.offered_a, "A")
            elif m == "Power.Active.Import":
                add(m, "%.1f" % (sum(c.measured.values()) * self.cfg.voltage), "W")
            elif m == "Power.Offered":
                add(m, "%.1f" % (c.offered_a * self.cfg.voltage * len(phases)), "W")
            elif m == "Voltage":
                for p in phases:
                    add(m, "%.1f" % self.cfg.voltage, "V", p + "-N")
            elif m == "SoC":
                if c.vehicle.battery_kwh:
                    add(m, "%.0f" % c.vehicle.soc, "Percent", loc="EV")
        return sv

    def _meter_payload(self, c, context="Sample.Periodic"):
        p = {"connectorId": c.id,
             "meterValue": [{"timestamp": iso(), "sampledValue": self._sampled(c, context)}]}
        if c.tx is not None and c.tx.id is not None:
            p["transactionId"] = c.tx.id
        return p

    def send_meter_values(self, c):
        snap = self._meter_payload(c)
        tx = c.tx
        if tx is not None:
            def build(snap=snap, tx=tx):
                if tx.id is not None:
                    snap["transactionId"] = tx.id
                return snap
            self.enqueue("MeterValues", build)
        else:
            self.enqueue("MeterValues", lambda: snap)

    async def meter_loop(self):
        """Dispara los MeterValues periodicos segun MeterValueSampleInterval (lo puede cambiar el CSMS)."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.5)
            iv = self.cfg_int("MeterValueSampleInterval", 0)
            if iv <= 0 or not self.boot_ok.is_set():
                continue
            t = loop.time()
            for c in self.connectors.values():
                if c.tx is None:
                    c.next_sample = None
                    continue
                if c.next_sample is None:
                    c.next_sample = t + iv
                if t >= c.next_sample:
                    c.next_sample = t + iv
                    self.send_meter_values(c)

    # ------------------------------------------------------------------ acciones de script
    def conn(self, cid):
        if cid not in self.connectors:
            raise ScriptRuntimeError("%s no tiene el conector %s" % (self.id, cid))
        return self.connectors[cid]

    def plug(self, cid=1):
        c = self.conn(cid)
        if c.vehicle.plugged:
            return
        c.vehicle.plugged = True
        self._refresh_status(c)
        if c.pending_remote and c.tx is None:
            pr, c.pending_remote = c.pending_remote, None
            asyncio.ensure_future(self._start_tx(c, pr["id_tag"], authorize=False, profile=pr.get("profile")))

    def unplug(self, cid=1):
        c = self.conn(cid)
        if not c.vehicle.plugged:
            return
        c.vehicle.plugged = False
        if c.tx is not None and self.config["StopTransactionOnEVSideDisconnect"][0] == "true":
            self._stop_tx(c, "EVDisconnected")
        c.vehicle.current_a = 0.0
        self._refresh_status(c)

    async def start_tx(self, cid=1, id_tag=None, wait=10.0):
        c = self.conn(cid)
        if not c.vehicle.plugged:
            raise ScriptRuntimeError("%s.%d: START sin auto enchufado (falta PLUG)" % (self.id, cid))
        if c.tx is not None:
            raise ScriptRuntimeError("%s.%d: ya hay una transaccion activa" % (self.id, cid))
        if c.fault or c.inoperative:
            raise ScriptRuntimeError("%s.%d: conector en falla/inoperativo" % (self.id, cid))
        return await self._start_tx(c, id_tag or self.cfg.id_tag,
                                    authorize=self.cfg.authorize_before_start, wait=wait)

    async def _start_tx(self, c, id_tag, authorize, profile=None, wait=10.0):
        if authorize and self.connected and self.boot_ok.is_set():
            try:
                res = await self.call("Authorize", {"idTag": id_tag})
            except (OcppCallError, asyncio.TimeoutError, ConnectionError) as e:
                raise ScriptRuntimeError("%s: Authorize fallo: %s" % (self.id, e))
            st = (res.get("idTagInfo") or {}).get("status")
            if st != "Accepted":
                self.rec.log("error", self.id, "Authorize rechazado: %s" % st)
                return "AuthorizeRejected:%s" % st
        if c.tx is not None:
            return "AlreadyActive"
        tx = Transaction(id_tag, int(round(c.meter_wh)), c.id)
        c.tx = tx
        c.next_sample = None
        ts = iso(tx.start_time)

        def on_conf(res):
            info = res.get("idTagInfo") or {}
            tx.id = res.get("transactionId")
            tx.started.set()
            if info.get("status") != "Accepted":
                tx.denied = True
                self.rec.log("error", self.id, "StartTransaction no aceptada: %s" % info.get("status"))
                if self.config["StopTransactionOnInvalidId"][0] == "true":
                    self._stop_tx(c, "DeAuthorized")
            else:
                self.rec.log("tx", self.id, "conector %d: transaccion %s iniciada" % (c.id, tx.id))

        self.enqueue("StartTransaction", lambda: {
            "connectorId": c.id, "idTag": id_tag, "meterStart": tx.meter_start, "timestamp": ts}, on_conf)
        self._refresh_status(c)
        if profile:
            self.store.set(c.id, profile, utcnow(), tx_id=None, tx_active=True)
        if self.connected and self.boot_ok.is_set() and wait:
            try:
                await asyncio.wait_for(tx.started.wait(), wait)
            except asyncio.TimeoutError:
                raise ScriptRuntimeError("%s: StartTransaction sin respuesta en %.0fs" % (self.id, wait))
        return "Started"

    def stop_tx(self, cid=1, reason="Local"):
        c = self.conn(cid)
        if c.tx is None:
            raise ScriptRuntimeError("%s.%d: no hay transaccion activa" % (self.id, cid))
        self._stop_tx(c, reason)

    def _stop_tx(self, c, reason):
        tx = c.tx
        if tx is None:
            return
        c.tx = None
        stop_wh = int(round(c.meter_wh))
        ts = iso()
        final = {"timestamp": ts, "sampledValue": [
            {"value": str(stop_wh), "context": "Transaction.End", "format": "Raw",
             "measurand": "Energy.Active.Import.Register", "location": "Outlet", "unit": "Wh"}]}

        def build():
            p = {"transactionId": tx.id if tx.id is not None else 0, "meterStop": stop_wh,
                 "timestamp": ts, "reason": reason, "idTag": tx.id_tag, "transactionData": [final]}
            return p

        self.enqueue("StopTransaction", build)
        self.store.clear_tx_profiles(c.id)
        c.pending_remote = None
        if c.pending_inoperative:
            c.inoperative = True
            c.pending_inoperative = False
        c.vehicle.state = "idle"
        c.status = "Finishing" if c.vehicle.plugged else c.status
        self.rec.log("tx", self.id, "conector %d: transaccion %s detenida (%s)" % (c.id, tx.id, reason))
        self._refresh_status(c)

    def set_fault(self, cid=1, code="OtherError"):
        c = self.conn(cid)
        c.fault = code
        if c.tx is not None:
            self._stop_tx(c, "Other")
        self._refresh_status(c)

    def clear_fault(self, cid=1):
        c = self.conn(cid)
        c.fault = None
        self._refresh_status(c)

    def set_demand(self, cid, amps, over_s=0.0):
        self.conn(cid).vehicle.set_demand(amps, over_s)

    # ------------------------------------------------------------------ handlers CSMS -> cargador
    def _valid_conn(self, p, key="connectorId"):
        cid = p.get(key)
        return cid if isinstance(cid, int) and 0 <= cid <= self.cfg.connectors else None

    def h_SetChargingProfile(self, p):
        cid = self._valid_conn(p)
        if cid is None or "csChargingProfiles" not in p:
            return {"status": "Rejected"}
        tx_id, tx_active = None, False
        if cid > 0:
            tx = self.connectors[cid].tx
            tx_active = tx is not None
            tx_id = tx.id if tx else None
        status = self.store.set(cid, p["csChargingProfiles"], utcnow(), tx_id=tx_id, tx_active=tx_active)
        prof = p["csChargingProfiles"]
        self.rec.log("csms", self.id, "SetChargingProfile conector %s %s/stack %s -> %s%s" % (
            cid, prof.get("chargingProfilePurpose"), prof.get("stackLevel"), status,
            " (%s)" % self.store.last_reason if self.store.last_reason else ""))
        return {"status": status}

    def h_ClearChargingProfile(self, p):
        n = self.store.clear(p.get("id"), p.get("connectorId"), p.get("chargingProfilePurpose"), p.get("stackLevel"))
        self.rec.log("csms", self.id, "ClearChargingProfile -> %d borrado(s)" % n)
        return {"status": "Accepted" if n else "Unknown"}

    def h_GetCompositeSchedule(self, p):
        cid = self._valid_conn(p)
        dur = p.get("duration")
        if cid is None or not isinstance(dur, int) or dur < 0:
            return {"status": "Rejected"}
        unit = p.get("chargingRateUnit", "A")
        if unit not in ("A", "W"):
            return {"status": "Rejected"}
        tx = self.connectors[cid].tx if cid > 0 else None
        now = utcnow()
        periods = self.store.composite(cid, now, dur, tx is not None, tx.id if tx else None,
                                       tx.start_time if tx else None, self.cfg.voltage, self.cfg.phases)
        out = []
        for off, lim, nph in periods:
            a = self.cfg.max_a if lim is None else min(lim, self.cfg.max_a)
            val = a if unit == "A" else a * self.cfg.voltage * (nph or self.cfg.phases)
            per = {"startPeriod": off, "limit": round(val, 2)}
            if nph:
                per["numberPhases"] = nph
            out.append(per)
        return {"status": "Accepted", "connectorId": cid, "scheduleStart": iso(now),
                "chargingSchedule": {"duration": dur, "startSchedule": iso(now), "chargingRateUnit": unit,
                                     "chargingSchedulePeriod": out}}

    def h_RemoteStartTransaction(self, p):
        id_tag = p.get("idTag")
        if not id_tag:
            return {"status": "Rejected"}
        cid = p.get("connectorId")
        if cid is None:
            free = [c for c in self.connectors.values() if c.tx is None and not c.fault and not c.inoperative]
            free.sort(key=lambda c: (not c.vehicle.plugged, c.id))
            c = free[0] if free else None
        else:
            c = self.connectors.get(cid)
        if c is None or c.tx is not None or c.fault or c.inoperative:
            return {"status": "Rejected"}
        prof = p.get("chargingProfile")
        if prof and prof.get("chargingProfilePurpose") != "TxProfile":
            return {"status": "Rejected"}
        need_auth = self.config["AuthorizeRemoteTxRequests"][0] == "true"
        self.rec.log("csms", self.id, "RemoteStartTransaction conector %d idTag %s" % (c.id, id_tag))
        if c.vehicle.plugged:
            return {"status": "Accepted"}, lambda: self._start_tx(c, id_tag, authorize=need_auth, profile=prof)
        c.pending_remote = {"id_tag": id_tag, "profile": prof}
        return {"status": "Accepted"}

    def h_RemoteStopTransaction(self, p):
        tid = p.get("transactionId")
        for c in self.connectors.values():
            if c.tx is not None and c.tx.id == tid:
                self.rec.log("csms", self.id, "RemoteStopTransaction %s" % tid)
                return {"status": "Accepted"}, lambda c=c: self._stop_tx(c, "Remote")
        return {"status": "Rejected"}

    def h_Reset(self, p):
        kind = p.get("type", "Soft")
        self.rec.log("csms", self.id, "Reset %s" % kind)

        async def do_reset():
            for c in self.connectors.values():
                if c.tx is not None:
                    self._stop_tx(c, "HardReset" if kind == "Hard" else "SoftReset")
            await asyncio.sleep(0.5)
            self._block_until = asyncio.get_running_loop().time() + 2.0
            if self.ws is not None:
                await self.ws.close()

        return {"status": "Accepted"}, do_reset

    def h_ChangeConfiguration(self, p):
        key, val = p.get("key"), p.get("value")
        if key not in self.config:
            return {"status": "NotSupported"}
        if self.config[key][1]:
            return {"status": "Rejected"}
        if key == "MeterValuesSampledData":
            bad = [m for m in str(val).split(",") if m and m not in SUPPORTED_MEASURANDS]
            if bad:
                return {"status": "Rejected"}
        self.config[key][0] = str(val)
        self.rec.log("csms", self.id, "ChangeConfiguration %s=%s" % (key, val))
        return {"status": "Accepted"}

    def h_GetConfiguration(self, p):
        keys = p.get("key")
        known, unknown = [], []
        for k in (keys or list(self.config)):
            if k in self.config:
                known.append({"key": k, "readonly": self.config[k][1], "value": self.config[k][0]})
            else:
                unknown.append(k)
        out = {"configurationKey": known}
        if unknown:
            out["unknownKey"] = unknown
        return out

    def h_TriggerMessage(self, p):
        msg = p.get("requestedMessage")
        cid = p.get("connectorId")
        if cid is not None and self._valid_conn(p) is None:
            return {"status": "Rejected"}
        if msg == "Heartbeat":
            return {"status": "Accepted"}, lambda: self.call("Heartbeat", {})
        if msg == "BootNotification":
            return {"status": "Accepted"}, lambda: self.call("BootNotification", {
                "chargePointVendor": self.cfg.vendor, "chargePointModel": self.cfg.model,
                "chargePointSerialNumber": self.cfg.serial})
        if msg == "StatusNotification":
            def st():
                for c in self.connectors.values():
                    if cid in (None, c.id):
                        self._refresh_status(c, force=True)
            return {"status": "Accepted"}, st
        if msg == "MeterValues":
            def mv():
                for c in self.connectors.values():
                    if cid in (None, c.id):
                        self.send_meter_values(c)
            return {"status": "Accepted"}, mv
        return {"status": "NotImplemented"}

    def h_ChangeAvailability(self, p):
        cid = self._valid_conn(p)
        if cid is None:
            return {"status": "Rejected"}
        inop = p.get("type") == "Inoperative"
        targets = list(self.connectors.values()) if cid == 0 else [self.connectors[cid]]
        scheduled = False
        for c in targets:
            if inop and c.tx is not None:
                c.pending_inoperative = True
                scheduled = True
            else:
                c.inoperative = inop
                self._refresh_status(c)
        return {"status": "Scheduled" if scheduled else "Accepted"}

    def h_UnlockConnector(self, p):
        return {"status": "NotSupported"}

    def h_ClearCache(self, p):
        return {"status": "Accepted"}

    def h_DataTransfer(self, p):
        return {"status": "UnknownVendorId"}

    def h_GetLocalListVersion(self, p):
        return {"listVersion": -1}

    def h_SendLocalList(self, p):
        return {"status": "NotSupported"}

    def h_ReserveNow(self, p):
        return {"status": "Rejected"}

    def h_CancelReservation(self, p):
        return {"status": "Rejected"}

    # ------------------------------------------------------------------ vistas para reportes
    def snapshot(self):
        out = {}
        for c in self.connectors.values():
            out[c.id] = {
                "status": c.status, "plugged": c.vehicle.plugged, "demand_a": round(c.vehicle.demand_a, 2),
                "limit_a": round(c.limit_a, 2), "limit_src": c.limit_src,
                "nominal_a": round(c.vehicle.current_a, 2),
                "measured_a": {p: round(v, 2) for p, v in c.measured.items() if v},
                "energy_wh": round(c.meter_wh, 1), "tx": c.tx.id if c.tx else None,
                "soc": round(c.vehicle.soc, 1) if c.vehicle.battery_kwh else None,
                "profiles": self.store.describe() if c.id == 1 else None,
            }
        return out
