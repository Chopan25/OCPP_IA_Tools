"""Carga y validacion del archivo de config JSON (sitio + cargadores)."""
import copy
import json
import os
from dataclasses import dataclass, field

DEFAULT_MEASURANDS = [
    "Energy.Active.Import.Register",
    "Current.Import",
    "Current.Offered",
    "Power.Active.Import",
    "Voltage",
]
SUPPORTED_MEASURANDS = DEFAULT_MEASURANDS + ["Power.Offered", "SoC"]


class ConfigError(Exception):
    pass


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _strip_comments(obj):
    """Las claves que empiezan con '_' son comentarios (JSON no tiene comentarios)."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


CHARGER_DEFAULTS = {
    "vendor": "evsim",
    "model": "SimCharger",
    "firmware": "evsim-0.1.0",
    "connectors": 1,
    "max_a": 32,
    "phases": 3,
    "phase": "L1",            # solo para cargadores monofasicos: a que fase del sitio cuelgan
    "voltage": None,          # None -> site.voltage
    "id_tag": "SIMTAG",
    "authorize_before_start": True,
    "auto_reconnect": True,
    "reconnect_s": 3.0,
    "append_serial": True,
    "meter": {
        "sample_interval_s": 10,
        "start_wh": 0,
        "measurands": DEFAULT_MEASURANDS,
    },
    "deviation": {            # ruido sobre la corriente medida
        "type": "none",       # none | gaussian | uniform
        "abs_a": 0.0,         # amplitud fija en A (sigma si gaussian, +-abs si uniform)
        "pct": 0.0,           # amplitud proporcional a la corriente, en % (se suma a abs_a)
        "bias_a": 0.0,        # error sistematico del medidor en A
    },
    "vehicle": {
        "min_a": 6.0,         # debajo de esto el auto no carga (SuspendedEVSE)
        "ramp_a_per_s": 5.0,  # que tan rapido sigue al limite/demanda
        "battery_kwh": None,  # si se define, se simula SoC y el auto se llena
        "soc_percent": 20.0,
    },
    "smart": {
        "max_stack_level": 8,
        "max_periods": 24,
        "max_profiles": 20,
        "allowed_units": "Current,Power",
    },
}


@dataclass
class ChargerCfg:
    id: str
    serial: str
    url: str
    username: str
    password: str
    vendor: str
    model: str
    firmware: str
    connectors: int
    max_a: float
    phases: int
    phase: str
    voltage: float
    id_tag: str
    authorize_before_start: bool
    auto_reconnect: bool
    reconnect_s: float
    meter: dict
    deviation: dict
    vehicle: dict
    smart: dict
    raw: dict = field(default_factory=dict)


@dataclass
class Config:
    url: str
    subprotocol: str
    tls_verify: bool
    connect_timeout_s: float
    site_limit_a: float
    site_voltage: float
    site_phases: int
    chargers: dict            # id -> ChargerCfg (orden de declaracion)
    path: str = ""
    raw: dict = field(default_factory=dict)


def _req(cond, path, msg):
    if not cond:
        raise ConfigError("%s: %s" % (path, msg))


def _num(v, path, lo=None, hi=None):
    _req(isinstance(v, (int, float)) and not isinstance(v, bool), path, "debe ser un numero (es %r)" % (v,))
    if lo is not None:
        _req(v >= lo, path, "debe ser >= %s (es %s)" % (lo, v))
    if hi is not None:
        _req(v <= hi, path, "debe ser <= %s (es %s)" % (hi, v))
    return v


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError("no existe el archivo de config: %s" % path)
    except json.JSONDecodeError as e:
        raise ConfigError("%s: JSON invalido (linea %d, col %d): %s" % (path, e.lineno, e.colno, e.msg))
    raw = _strip_comments(raw)
    _req(isinstance(raw, dict), "$", "la raiz debe ser un objeto")

    csms = raw.get("csms") or {}
    site = raw.get("site") or {}
    _req(isinstance(csms.get("url"), str) and csms["url"].startswith(("ws://", "wss://")),
         "csms.url", "debe ser una URL ws:// o wss://")
    auth = csms.get("auth") or {"type": "none"}
    _req(auth.get("type", "none") in ("none", "basic"), "csms.auth.type", "debe ser none o basic")

    site_limit = _num(site.get("limit_a", 40), "site.limit_a", 0)
    site_voltage = _num(site.get("voltage", 230), "site.voltage", 1)
    site_phases = int(_num(site.get("phases", 3), "site.phases", 1, 3))

    defaults = _deep_merge(CHARGER_DEFAULTS, raw.get("defaults"))
    clist = raw.get("chargers")
    _req(isinstance(clist, list) and clist, "chargers", "debe ser una lista con al menos un cargador")

    chargers = {}
    for i, entry in enumerate(clist):
        p = "chargers[%d]" % i
        _req(isinstance(entry, dict), p, "debe ser un objeto")
        cid = entry.get("id")
        _req(isinstance(cid, str) and cid and cid.replace("_", "").isalnum(), p + ".id",
             "id obligatorio (letras/numeros/_), es el nombre que se usa en los scripts")
        _req(cid.upper() != "ALL", p + ".id", "ALL esta reservado")
        _req(cid not in chargers, p + ".id", "id duplicado: %s" % cid)
        serial = entry.get("serial")
        _req(isinstance(serial, str) and serial, p + ".serial", "serial obligatorio (es el chargeBoxId)")
        e = _deep_merge(defaults, entry)

        base = e.get("url") or csms["url"]
        _req(str(base).startswith(("ws://", "wss://")), p + ".url", "debe ser ws:// o wss://")
        url = base.rstrip("/") + "/" + serial if e["append_serial"] else base

        a = _deep_merge(auth, e.get("auth"))
        username = password = ""
        if a.get("type", "none") == "basic":
            username = a.get("username") or serial
            password = a.get("password") or ""
            env = a.get("password_env")
            if env:
                password = os.environ.get(env, "")
                _req(password, p + ".auth.password_env", "la variable de entorno %s no esta definida" % env)

        connectors = int(_num(e["connectors"], p + ".connectors", 1, 8))
        max_a = float(_num(e["max_a"], p + ".max_a", 1))
        phases = int(_num(e["phases"], p + ".phases", 1, 3))
        _req(e["phase"] in ("L1", "L2", "L3"), p + ".phase", "debe ser L1, L2 o L3")
        voltage = float(_num(e["voltage"] if e["voltage"] is not None else site_voltage, p + ".voltage", 1))

        dev = e["deviation"]
        _req(dev.get("type", "none") in ("none", "gaussian", "uniform"), p + ".deviation.type",
             "debe ser none, gaussian o uniform")
        for k in ("abs_a", "pct", "bias_a"):
            _num(dev.get(k, 0), p + ".deviation." + k)
        meas = e["meter"].get("measurands", [])
        for m in meas:
            _req(m in SUPPORTED_MEASURANDS, p + ".meter.measurands",
                 "measurand no soportado: %s (soportados: %s)" % (m, ", ".join(SUPPORTED_MEASURANDS)))
        _num(e["meter"].get("sample_interval_s", 10), p + ".meter.sample_interval_s", 0)
        veh = e["vehicle"]
        _num(veh.get("min_a", 6), p + ".vehicle.min_a", 0)
        _num(veh.get("ramp_a_per_s", 5), p + ".vehicle.ramp_a_per_s", 0.01)

        chargers[cid] = ChargerCfg(
            id=cid, serial=serial, url=url, username=username, password=password,
            vendor=e["vendor"], model=e["model"], firmware=e["firmware"],
            connectors=connectors, max_a=max_a, phases=phases, phase=e["phase"], voltage=voltage,
            id_tag=e["id_tag"], authorize_before_start=bool(e["authorize_before_start"]),
            auto_reconnect=bool(e["auto_reconnect"]), reconnect_s=float(e["reconnect_s"]),
            meter=e["meter"], deviation=dev, vehicle=veh, smart=e["smart"], raw=entry,
        )

    return Config(
        url=csms["url"], subprotocol=csms.get("subprotocol", "ocpp1.6"),
        tls_verify=bool(csms.get("tls_verify", True)),
        connect_timeout_s=float(csms.get("connect_timeout_s", 10)),
        site_limit_a=float(site_limit), site_voltage=float(site_voltage), site_phases=site_phases,
        chargers=chargers, path=os.path.abspath(path), raw=raw,
    )
