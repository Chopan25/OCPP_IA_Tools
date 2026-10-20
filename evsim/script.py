"""Parser del lenguaje de scripts .evs (estilo gcode): una instruccion por linea.

    ; comentario            VERBO arg arg clave=valor        "texto con espacios"

Los bloques REPEAT / FOR se cierran con END. INCLUDE se expande al parsear.
El parser valida sintaxis, verbos, cantidad de argumentos, claves, bloques y (si se le pasa la
config) que los cargadores existan. Todos los errores llevan archivo:linea.
"""
import os
import re
from dataclasses import dataclass, field

_KW_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


class ScriptError(Exception):
    def __init__(self, msg, file=None, line=None):
        self.msg, self.file, self.line = msg, file, line
        loc = ""
        if file:
            loc = "%s:%s: " % (os.path.basename(file) if file else "?", line if line else "?")
        super().__init__(loc + msg)


@dataclass
class Step:
    file: str
    line: int
    verb: str
    args: list
    kwargs: dict
    raw: str
    body: list = field(default_factory=list)   # para REPEAT / FOR

    @property
    def where(self):
        return "%s:%d" % (os.path.basename(self.file), self.line)


# verbo -> (min_pos, max_pos|None, claves permitidas|None=cualquiera, descripcion corta)
VERBS = {
    # --- declaraciones
    "SCENARIO":        (1, None, set(), "nombre del escenario"),
    "DESCRIPTION":     (1, None, set(), "descripcion libre"),
    "SEED":            (1, 1, set(), "semilla del azar (ruido, llegadas)"),
    "MODE":            (1, 1, set(), "strict | soft"),
    "SET_SITE_LIMIT":  (1, 1, set(), "limite del sitio en A para el oraculo"),
    "TOLERANCE":       (1, 1, set(), "tolerancia por defecto de los EXPECT, en A"),
    "DEFAULT_WITHIN":  (1, 1, set(), "plazo por defecto de los EXPECT (ej: 15s)"),
    "SET":             (0, 2, None, "SET nombre valor | SET nombre=valor"),
    "PRINT":           (0, None, set(), "imprime texto en consola y reporte"),
    "MARK":            (1, None, set(), "hito en la linea de tiempo del reporte"),
    "SNAPSHOT":        (0, 1, set(), "guarda el estado de todos los cargadores"),
    # --- ciclo de vida
    "CONNECT":         (1, None, {"timeout"}, "abre WS y espera BootNotification Accepted"),
    "DISCONNECT":      (1, None, set(), "cierra el WS limpiamente"),
    "DROP_LINK":       (1, None, {"for"}, "corta el WS sin cierre limpio"),
    # --- vehiculo
    "PLUG":            (1, None, set(), "enchufa el auto (varios objetivos o ALL)"),
    "UNPLUG":          (1, None, set(), "desenchufa el auto (varios objetivos o ALL)"),
    "START":           (1, 1, {"idtag"}, "Authorize + StartTransaction"),
    "STOP":            (1, None, {"reason"}, "StopTransaction (varios objetivos o ALL: omite los sin transaccion)"),
    "DEMAND":          (2, 2, {"over"}, "corriente maxima que acepta el auto, en A"),
    "FAULT":           (1, 1, {"code"}, "pone el conector en Faulted"),
    "CLEAR_FAULT":     (1, None, set(), "saca la falla"),
    # --- tiempo
    "WAIT":            (1, 1, set(), "espera una duracion"),
    "WAIT_UNTIL_STABLE": (1, None, {"for", "timeout"}, "espera que limite y corriente dejen de cambiar"),
    "WAIT_MSG":        (2, 2, {"timeout", "count"}, "espera un mensaje del CSMS"),
    "WAIT_ARRIVALS":   (0, 0, {"timeout"}, "espera que terminen las llegadas aleatorias"),
    "SEND":            (2, 3, set(), "envia un CALL crudo desde el cargador"),
    # --- verificaciones
    "EXPECT_LIMIT":    (3, 3, {"within", "tol"}, "limite efectivo aplicado en el cargador (A)"),
    "EXPECT_ACTUAL":   (3, 3, {"within", "tol"}, "corriente real consumida (A, sin ruido)"),
    "EXPECT_MEASURED": (3, 3, {"within", "tol"}, "corriente medida (con desviacion) en A"),
    "EXPECT_SUM_ACTUAL": (2, 2, {"within", "tol", "of"}, "suma de corriente real (peor fase)"),
    "EXPECT_SUM_LIMIT": (2, 2, {"within", "tol", "of"}, "suma de limites asignados (peor fase)"),
    "EXPECT_SUM_LE":   (1, 1, {"within", "tol", "of"}, "atajo: suma de corriente real <= valor"),
    "EXPECT_STATE":    (2, 2, {"within"}, "estado del conector (Charging, SuspendedEVSE...)"),
    "EXPECT_TX":       (2, 2, {"within"}, "transaccion active | none"),
    "EXPECT_CONNECTED": (1, 1, {"within"}, "el cargador esta conectado"),
    "EXPECT_DISCONNECTED": (1, 1, {"within"}, "el cargador esta desconectado"),
    "EXPECT_MSG":      (2, 2, {"within", "count"}, "llego un mensaje del CSMS"),
    "EXPECT_NO_MSG":   (2, 2, {"for"}, "NO llego un mensaje del CSMS durante un tiempo"),
    "EXPECT_NO_PROFILE": (1, 1, {"within"}, "el cargador no tiene perfiles instalados"),
    "ASSERT_ALWAYS":   (3, 3, {"grace", "id", "tol"}, "invariante evaluada en cada tick"),
    "ASSERT_STOP":     (1, 1, set(), "desactiva un ASSERT_ALWAYS"),
    # --- azar y control de flujo
    "RANDOM_ARRIVALS": (0, 0, {"n", "over", "chargers", "demand", "duration", "seed", "idtag"}, "llegadas aleatorias"),
    "REPEAT":          (1, 1, {"var"}, "repite el bloque n veces"),
    "FOR":             (3, None, set(), "FOR var IN a b c  (o 1..5)"),
    "END":             (0, 0, set(), "cierra REPEAT / FOR"),
    "INCLUDE":         (1, 1, set(), "incluye otro .evs"),
}

# verbos cuyo primer argumento es un cargador (A, A.2, ALL) -> se valida contra la config
TARGET_FIRST = {"START", "DEMAND", "FAULT", "WAIT_MSG", "SEND",
                "EXPECT_LIMIT", "EXPECT_ACTUAL", "EXPECT_MEASURED", "EXPECT_STATE", "EXPECT_TX",
                "EXPECT_CONNECTED", "EXPECT_DISCONNECTED", "EXPECT_MSG", "EXPECT_NO_MSG", "EXPECT_NO_PROFILE"}
TARGET_ALL = {"CONNECT", "DISCONNECT", "DROP_LINK", "WAIT_UNTIL_STABLE", "PLUG", "UNPLUG", "STOP", "CLEAR_FAULT"}
OPS = ("==", "!=", "<=", ">=", "<", ">")
OP_INDEX = {"EXPECT_LIMIT": 1, "EXPECT_ACTUAL": 1, "EXPECT_MEASURED": 1,
            "EXPECT_SUM_ACTUAL": 0, "EXPECT_SUM_LIMIT": 0, "ASSERT_ALWAYS": 1}
STIMULUS = {"CONNECT", "DISCONNECT", "DROP_LINK", "PLUG", "UNPLUG", "START", "STOP", "DEMAND", "FAULT",
            "CLEAR_FAULT", "SEND"}


def tokenize(text, file=None, line=None):
    """Separa por espacios respetando "..." y '...'; corta en ';' fuera de comillas."""
    toks, cur, q, have = [], [], None, False
    for ch in text:
        if q:
            if ch == q:
                q = None
            else:
                cur.append(ch)
        elif ch in "\"'":
            q, have = ch, True
        elif ch == ";":
            break
        elif ch.isspace():
            if cur or have:
                toks.append("".join(cur))
                cur, have = [], False
        else:
            cur.append(ch)
    if q:
        raise ScriptError("comilla sin cerrar", file, line)
    if cur or have:
        toks.append("".join(cur))
    return toks


def split_args(toks):
    args, kwargs = [], {}
    for t in toks:
        m = _KW_RE.match(t)
        if m and not t.startswith(("=", "<", ">", "!")):
            kwargs[m.group(1).lower()] = m.group(2)
        else:
            args.append(t)
    return args, kwargs


def parse_target(tok):
    """'A' -> ('A', 1) ; 'A.2' -> ('A', 2) ; 'ALL' -> ('ALL', None)."""
    if tok.upper() == "ALL":
        return "ALL", None
    if "." in tok:
        cid, _, n = tok.rpartition(".")
        if not n.isdigit() or int(n) < 1:
            raise ValueError("conector invalido en %r" % tok)
        return cid, int(n)
    return tok, 1


def interpolate(text, variables, where=None):
    def rep(m):
        name = (m.group(1) or m.group(2)).lower()
        if name not in variables:
            raise ScriptError("variable no definida: $%s" % name, *(where or (None, None)))
        v = variables[name]
        return ("%g" % v) if isinstance(v, float) else str(v)
    return _VAR_RE.sub(rep, text)


def parse_file(path, known_chargers=None, _stack=None, _defined=None):
    """-> lista de Step (con bloques anidados). known_chargers: dict/iterable de ids para validar."""
    path = os.path.abspath(path)
    _stack = _stack or []
    if path in _stack:
        raise ScriptError("INCLUDE circular: %s" % " -> ".join(os.path.basename(p) for p in _stack + [path]))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise ScriptError("no se pudo leer el script: %s" % e)
    defined = _defined if _defined is not None else set()
    root, stack = [], []   # stack de (step, lista_destino)
    cur = root
    for i, raw in enumerate(lines, 1):
        toks = tokenize(raw, path, i)
        if not toks:
            continue
        verb = toks[0].upper()
        if verb not in VERBS:
            near = [v for v in VERBS if v.startswith(verb[:3])][:4]
            raise ScriptError("verbo desconocido: %s%s" % (toks[0], (" (quisiste decir: %s?)" % ", ".join(near)) if near else ""), path, i)
        args, kwargs = split_args(toks[1:])
        mn, mx, allowed, _ = VERBS[verb]
        if verb in ("SCENARIO", "DESCRIPTION", "PRINT", "MARK"):
            args = [" ".join(toks[1:])] if len(toks) > 1 else []
            kwargs = {}
        if verb not in ("SET",) and allowed is not None:
            bad = [k for k in kwargs if k not in allowed]
            if bad:
                raise ScriptError("%s: parametro(s) desconocido(s): %s (permitidos: %s)" % (
                    verb, ", ".join(bad), ", ".join(sorted(allowed)) or "ninguno"), path, i)
        if verb == "SET":
            if not (kwargs or len(args) == 2) or (kwargs and args):
                raise ScriptError("SET: usar  SET nombre valor  o  SET nombre=valor", path, i)
            for k in (kwargs or {args[0].lower(): 1}):
                defined.add(k.lower())
        elif len(args) < mn or (mx is not None and len(args) > mx):
            rango = "%d" % mn if mx == mn else ("%d o mas" % mn if mx is None else "%d a %d" % (mn, mx))
            raise ScriptError("%s: se esperaban %s argumento(s), hay %d" % (verb, rango, len(args)), path, i)
        step = Step(path, i, verb, args, kwargs, raw.strip())

        # validaciones estaticas
        for t in list(args) + list(kwargs.values()):
            for m in _VAR_RE.finditer(t):
                name = (m.group(1) or m.group(2)).lower()
                if name not in defined:
                    raise ScriptError("variable no definida: $%s (definila antes con SET)" % name, path, i)
        if verb in OP_INDEX and args[OP_INDEX[verb]] not in OPS and not args[OP_INDEX[verb]].startswith("$"):
            raise ScriptError("%s: operador invalido %r (usar %s)" % (verb, args[OP_INDEX[verb]], " ".join(OPS)), path, i)
        if verb == "MODE" and args[0].lower() not in ("strict", "soft") and not args[0].startswith("$"):
            raise ScriptError("MODE: usar strict o soft", path, i)
        if known_chargers is not None:
            names = set(known_chargers)
            tgts = []
            if verb in TARGET_FIRST:
                tgts = args[:1]
            elif verb in TARGET_ALL:
                tgts = args
            for t in tgts:
                if t.startswith("$"):
                    continue
                try:
                    cid, _n = parse_target(t)
                except ValueError as e:
                    raise ScriptError("%s: %s" % (verb, e), path, i)
                if cid != "ALL" and cid not in names:
                    raise ScriptError("%s: cargador desconocido %r (config tiene: %s)" % (verb, cid, ", ".join(sorted(names))), path, i)
            ch = kwargs.get("chargers") or kwargs.get("of")
            if ch and not ch.startswith("$"):
                for t in ch.split(","):
                    cid, _n = parse_target(t.strip())
                    if cid != "ALL" and cid not in names:
                        raise ScriptError("%s: cargador desconocido %r en lista" % (verb, cid), path, i)

        if verb == "INCLUDE":
            inc = args[0]
            if not os.path.isabs(inc):
                inc = os.path.join(os.path.dirname(path), inc)
            sub = parse_file(inc, known_chargers, _stack + [path], defined)
            cur.extend(sub)
            continue
        if verb in ("REPEAT", "FOR"):
            if verb == "FOR" and (len(args) < 3 or args[1].upper() != "IN"):
                raise ScriptError("FOR: usar  FOR var IN a b c  (o 1..5)", path, i)
            cur.append(step)
            stack.append((step, cur))
            cur = step.body
            if verb == "FOR":
                defined.add(args[0].lower())
            elif "var" in kwargs:
                defined.add(kwargs["var"].lower())
            continue
        if verb == "END":
            if not stack:
                raise ScriptError("END sin REPEAT/FOR abierto", path, i)
            _, cur = stack.pop()
            continue
        cur.append(step)
    if stack:
        s = stack[-1][0]
        raise ScriptError("%s abierto aqui nunca se cierra (falta END)" % s.verb, s.file, s.line)
    return root


def walk(steps):
    for s in steps:
        yield s
        if s.body:
            yield from walk(s.body)
