"""Registro central de una corrida: eventos, mensajes OCPP, muestras 1 Hz, expectativas y violaciones."""
import json
import os
import time
from collections import defaultdict

from .util import iso, utcnow


class Recorder:
    def __init__(self, out_dir=None):
        self.t0 = time.monotonic()
        self.wall0 = utcnow()
        self.out_dir = out_dir
        self.events = []        # {t, kind, charger, text, stimulus}
        self.messages = []      # {t, charger, dir, action, frame}
        self.samples = []       # filas 1 Hz por conector
        self.expectations = []  # resultados de EXPECT_*
        self.violations = []    # ASSERT_ALWAYS
        self.snapshots = []
        self.notes = []         # advertencias de la corrida
        self.last_stimulus_t = None
        self.stimulus_t = []
        self._msg_log = None
        self._counts = defaultdict(int)   # (charger, action, dir) -> n
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            self._msg_log = open(os.path.join(out_dir, "messages.log"), "w", encoding="utf-8", buffering=1)

    def now(self):
        return time.monotonic() - self.t0

    # ---- eventos ---------------------------------------------------------------
    def log(self, kind, charger, text, stimulus=False):
        t = self.now()
        self.events.append({"t": round(t, 3), "kind": kind, "charger": charger, "text": text, "stimulus": stimulus})
        if stimulus:
            self.last_stimulus_t = t
            self.stimulus_t.append(t)

    # ---- mensajes OCPP ---------------------------------------------------------
    def msg(self, charger, direction, action, frame_text):
        t = self.now()
        self._counts[(charger, action, direction)] += 1
        self.messages.append({"t": round(t, 3), "charger": charger, "dir": direction, "action": action})
        if self._msg_log:
            self._msg_log.write("%s %9.3f %-10s %s %s\n" % (iso(), t, charger, direction, frame_text))

    def count(self, charger, action, direction="<-"):
        if charger is None:
            return sum(n for (c, a, d), n in self._counts.items() if a == action and d == direction)
        return self._counts[(charger, action, direction)]

    def count_between(self, action, t_from, t_to, direction="<-", charger=None):
        return sum(1 for m in self.messages
                   if m["action"] == action and m["dir"] == direction and t_from <= m["t"] < t_to
                   and (charger is None or m["charger"] == charger))

    # ---- cierre ----------------------------------------------------------------
    def close(self):
        if self._msg_log:
            self._msg_log.close()
            self._msg_log = None

    def dump_json(self, path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
