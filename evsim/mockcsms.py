"""CSMS de juguete con DLB (balanceo dinamico) para probar evsim sin depender de un backend real.

NO es un CSMS de produccion: es lo minimo para validar el simulador y tener una demo.
Algoritmo (a proposito simple y determinista):
  * reparte el limite del sitio en partes iguales entre las transacciones activas, con tope por cargador;
  * si un cargador consume claramente menos de lo que se le ofrecio (medido < ofrecido - umbral) se lo
    considera "limitado por el auto": se le asigna lo que consume y el sobrante se reparte entre los demas;
  * ese estado se reinicia en cada StartTransaction / StopTransaction (se vuelve a aprender);
  * manda el limite como SetChargingProfile TxProfile (Absolute, un periodo, stackLevel 1).
Uso:  python -m evsim mock-csms --limit 40
"""
import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timezone

import websockets


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (datetime.now(timezone.utc).microsecond // 1000)


class Tx:
    def __init__(self, tx_id, session, connector):
        self.id, self.session, self.connector = tx_id, session, connector
        self.offered = None
        self.measured = None
        self.prev = None
        self.offered_at = 0.0
        self.limited = False      # limitado por el auto
        self.last_sent = None


class Session:
    def __init__(self, cbid, ws):
        self.cbid, self.ws = cbid, ws
        self.txs = {}              # connector -> Tx


class MockCSMS:
    def __init__(self, limit_a=40.0, cap_a=32.0, host="127.0.0.1", port=9000, meter_interval=2,
                 threshold=1.5, headroom=0.0, verbose=True, min_change=1.0, settle_s=7.0, min_a=6.0):
        self.limit_a, self.cap_a = limit_a, cap_a
        self.host, self.port = host, port
        self.meter_interval = meter_interval
        self.threshold, self.headroom, self.min_change = threshold, headroom, min_change
        self.verbose = verbose
        self.settle_s = settle_s
        self.min_a = min_a
        self.sessions = {}
        self._tx_counter = 0
        self.server = None
        self.log_lines = []
        self.sent_profiles = []    # (t, cbid, connector, limit)

    def log(self, text):
        line = "[csms %s] %s" % (time.strftime("%H:%M:%S"), text)
        self.log_lines.append(line)
        if self.verbose:
            print(line, flush=True)

    async def start(self):
        self.server = await websockets.serve(self._handler, self.host, self.port, subprotocols=["ocpp1.6"],
                                             ping_interval=None)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    @property
    def url(self):
        return "ws://%s:%d/ocpp" % (self.host, self.port)

    # ------------------------------------------------------------------
    async def _handler(self, ws):
        path = ws.request.path if getattr(ws, "request", None) else ""
        cbid = path.rstrip("/").split("/")[-1] or "unknown"
        sess = self.sessions.get(cbid)
        if sess is None:
            sess = self.sessions[cbid] = Session(cbid, ws)   # las sesiones (y sus transacciones) sobreviven a la desconexion
        sess.ws = ws
        self.log("%s conectado (%s)" % (cbid, path))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] == 2:
                    await self._on_call(sess, msg)
                elif msg[0] == 3:
                    pass
                elif msg[0] == 4:
                    self.log("%s CALLERROR %s" % (cbid, msg[1:]))
        except websockets.ConnectionClosed:
            pass
        finally:
            if sess.ws is ws:
                sess.ws = None
            self.log("%s desconectado%s" % (cbid, " (transacciones activas: %d, se reserva su cupo)" % len(sess.txs) if sess.txs else ""))
            await self.rebalance(reset=False)

    async def _reply(self, sess, mid, payload):
        await sess.ws.send(json.dumps([3, mid, payload]))

    async def _call(self, sess, action, payload):
        if sess.ws is None:
            return
        try:
            await sess.ws.send(json.dumps([2, uuid.uuid4().hex[:16], action, payload]))
        except websockets.ConnectionClosed:
            pass

    async def _on_call(self, sess, msg):
        _, mid, action, p = msg
        if action == "BootNotification":
            await self._reply(sess, mid, {"status": "Accepted", "currentTime": now_iso(), "interval": 300})
            for tx in sess.txs.values():      # reconexion con transaccion viva: volver a mandarle su perfil
                tx.last_sent = None
            if sess.txs:
                await self.rebalance(reset=False)
            if self.meter_interval:
                await self._call(sess, "ChangeConfiguration", {"key": "MeterValueSampleInterval", "value": str(self.meter_interval)})
        elif action == "Heartbeat":
            await self._reply(sess, mid, {"currentTime": now_iso()})
        elif action == "Authorize":
            await self._reply(sess, mid, {"idTagInfo": {"status": "Accepted"}})
        elif action == "StartTransaction":
            self._tx_counter += 1
            tx = Tx(self._tx_counter, sess, p["connectorId"])
            sess.txs[p["connectorId"]] = tx
            await self._reply(sess, mid, {"transactionId": tx.id, "idTagInfo": {"status": "Accepted"}})
            self.log("%s.%d StartTransaction -> tx %d" % (sess.cbid, tx.connector, tx.id))
            await self.rebalance(reset=True)
        elif action == "StopTransaction":
            for k, tx in list(sess.txs.items()):
                if tx.id == p.get("transactionId"):
                    del sess.txs[k]
            await self._reply(sess, mid, {"idTagInfo": {"status": "Accepted"}})
            self.log("%s StopTransaction tx %s" % (sess.cbid, p.get("transactionId")))
            await self.rebalance(reset=True)
        elif action == "MeterValues":
            await self._reply(sess, mid, {})
            tx = sess.txs.get(p.get("connectorId"))
            if tx is not None:
                cur = []
                for mv in p.get("meterValue", []):
                    for sv in mv.get("sampledValue", []):
                        if sv.get("measurand") == "Current.Import":
                            cur.append(float(sv["value"]))
                if cur:
                    tx.prev, tx.measured = tx.measured, max(cur)
                    await self.rebalance(reset=False)
        else:  # StatusNotification, DataTransfer, DiagnosticsStatusNotification...
            await self._reply(sess, mid, {})

    # ------------------------------------------------------------------ DLB
    def _allocate(self, txs):
        cap = self.cap_a
        alloc = {}
        offline = [t for t in txs if t.session.ws is None]
        txs = [t for t in txs if t.session.ws is not None]
        for t in offline:                     # no se les puede cambiar el perfil: se reserva lo ultimo que se les ofrecio
            alloc[t.id] = t.offered if t.offered is not None else cap
        fixed = [t for t in txs if t.limited]
        free = [t for t in txs if not t.limited]
        for t in fixed:
            alloc[t.id] = min(cap, max(self.min_a, (t.measured or 0.0) + self.headroom))   # nunca por debajo del minimo del EVSE
        remaining = self.limit_a - sum(alloc.values())
        if remaining < 0:                 # lo fijo ya no entra: volver a repartir parejo entre los que se pueden cambiar
            reserved = {t.id: alloc[t.id] for t in offline}
            free, fixed, alloc = list(txs), [], dict(reserved)
            remaining = self.limit_a - sum(reserved.values())
        pool = list(free)
        while pool:
            share = remaining / len(pool)
            capped = [t for t in pool if cap < share]
            if not capped:
                for t in pool:
                    alloc[t.id] = share
                break
            for t in capped:
                alloc[t.id] = cap
                remaining -= cap
                pool.remove(t)
        return alloc

    async def rebalance(self, reset):
        txs = [tx for s in self.sessions.values() for tx in s.txs.values()]
        if not txs:
            return
        for tx in txs:
            if reset:
                tx.limited = False
            elif tx.measured is not None and tx.offered is not None:
                stable = tx.prev is not None and abs(tx.measured - tx.prev) < 0.3   # no tomar la rampa como demanda
                settled = time.time() - tx.offered_at >= self.settle_s   # dejar que el auto siga el cambio
                if stable and settled and tx.measured < tx.offered - self.threshold:
                    tx.limited = True
                elif tx.limited and tx.measured >= tx.offered - 0.5 and self.headroom > 0:
                    tx.limited = False
        alloc = self._allocate(txs)
        for tx in txs:
            if tx.session.ws is None:
                continue                      # offline: conserva su 'offered'
            lim = math.floor(alloc[tx.id] * 10 + 1e-6) / 10.0
            tx.offered = lim
            if tx.last_sent is None or abs(tx.last_sent - lim) >= self.min_change:
                tx.last_sent = lim
                tx.offered_at = time.time()
                self.sent_profiles.append((time.time(), tx.session.cbid, tx.connector, lim))
                self.log("%s.%d -> SetChargingProfile %.1f A%s" % (
                    tx.session.cbid, tx.connector, lim, " (limitado por el auto)" if tx.limited else ""))
                await self._call(tx.session, "SetChargingProfile", {
                    "connectorId": tx.connector,
                    "csChargingProfiles": {
                        "chargingProfileId": tx.id, "transactionId": tx.id, "stackLevel": 1,
                        "chargingProfilePurpose": "TxProfile", "chargingProfileKind": "Absolute",
                        "chargingSchedule": {
                            "startSchedule": now_iso(), "chargingRateUnit": "A",
                            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": lim}]}}})


async def serve_forever(limit, cap, host, port, meter_interval, headroom):
    srv = MockCSMS(limit_a=limit, cap_a=cap, host=host, port=port, meter_interval=meter_interval, headroom=headroom)
    await srv.start()
    print("Mock CSMS escuchando en %s/<chargeBoxId>  (limite del sitio %g A, tope por cargador %g A)" % (srv.url, limit, cap))
    print("Ctrl+C para salir.")
    await asyncio.Future()
