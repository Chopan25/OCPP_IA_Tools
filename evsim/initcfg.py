"""`evsim init`: arma el archivo de config a partir de los links de conexion de los cargadores.

Cada link es la URL OCPP de un cargador de la plataforma, por ejemplo
    wss://servidor:1403/ocpp/160/<token-de-la-cuenta>/<serial-del-cargador>
El ultimo tramo del path es el serial (chargeBoxId); lo anterior es la URL base.
"""
import json
import os
import re
import string
from urllib.parse import urlparse

TOKEN_LIKE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")


class InitError(Exception):
    pass


def split_link(link):
    """-> (url_base, serial, tiene_extras). tiene_extras: el link trae query o fragmento."""
    raw = link.strip().strip("\"'")
    u = urlparse(raw)
    if u.scheme not in ("ws", "wss") or not u.netloc:
        raise InitError("%r no parece un link de conexion (debe empezar con ws:// o wss://)" % mask(raw))
    base_path, _, serial = u.path.rstrip("/").rpartition("/")
    if not serial:
        raise InitError("%r no tiene el serial del cargador al final de la URL" % mask(raw))
    base = "%s://%s%s" % (u.scheme, u.netloc, base_path)
    return base, serial, bool(u.query or u.fragment)


def mask(url):
    """Oculta los tramos del path que parecen un token/clave, para poder mostrar la URL sin filtrarla."""
    if "://" not in url:
        return url
    u = urlparse(url)
    parts = [("***" if TOKEN_LIKE.match(p) and re.search(r"\d", p) and re.search(r"[A-Za-z]", p) else p)
             for p in u.path.split("/")]
    return "%s://%s%s%s" % (u.scheme, u.netloc, "/".join(parts), "?***" if u.query else "")


def safe_url(url):
    """URL para mostrar/guardar en logs y reportes: host y serial, sin el resto del path (que puede llevar la clave)."""
    u = urlparse(url)
    serial = u.path.rstrip("/").rpartition("/")[2]
    return "%s://%s/.../%s" % (u.scheme, u.netloc, serial)


def charger_id(i):
    letters = string.ascii_uppercase
    return letters[i] if i < len(letters) else "C%d" % (i + 1)


def build_config(links, limit_a=40, max_a=32, phases=3, voltage=230, id_tag="plug_and_play", sample_s=5):
    if not links:
        raise InitError("falta al menos un link de cargador")
    parsed = []
    for link in links:
        base, serial, extras = split_link(link)
        parsed.append((base, serial, extras, link.strip().strip("\"'")))
    serials = [p[1] for p in parsed]
    dup = sorted({s for s in serials if serials.count(s) > 1})
    if dup:
        raise InitError("serial repetido en los links: %s" % ", ".join(dup))

    bases = {p[0] for p in parsed}
    uniform = len(bases) == 1 and not any(p[2] for p in parsed)
    chargers = []
    for i, (base, serial, extras, full) in enumerate(parsed):
        c = {"id": charger_id(i), "serial": serial}
        if not uniform:
            c["url"] = full
            c["append_serial"] = False
        chargers.append(c)
    return {
        "_comment": "Generado por 'evsim init'. Contiene credenciales (token en la URL): no lo compartas ni lo subas a git.",
        "csms": {"url": parsed[0][0] if uniform else parsed[0][0], "subprotocol": "ocpp1.6",
                 "tls_verify": True, "connect_timeout_s": 15, "auth": {"type": "none"}},
        "site": {"limit_a": limit_a, "voltage": voltage, "phases": 3},
        "defaults": {"max_a": max_a, "phases": phases, "id_tag": id_tag,
                     "meter": {"sample_interval_s": sample_s}},
        "chargers": chargers,
    }


def write_config(cfg, path, force=False):
    if os.path.exists(path) and not force:
        raise InitError("ya existe %s (usa --force para reemplazarlo)" % path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
