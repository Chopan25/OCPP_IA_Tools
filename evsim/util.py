"""Utilidades chicas: duraciones, fechas ISO, evaluador aritmetico seguro, colores ANSI."""
import ast
import operator
import os
import re
import sys
from datetime import datetime, timezone

_DUR_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_DUR_MULT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_duration(text):
    """'20s' '500ms' '2m' '1h' o un numero pelado (segundos) -> segundos (float)."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _DUR_RE.match(str(text))
    if not m:
        raise ValueError("duracion invalida: %r (usar 20s, 500ms, 2m, 1h)" % (text,))
    return float(m.group(1)) * _DUR_MULT[m.group(2)]


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    """UTC con milisegundos y 'Z', formato OCPP."""
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def parse_iso(text):
    if text is None:
        return None
    t = str(text).strip()
    if t.endswith("Z") or t.endswith("z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---- evaluador aritmetico seguro (para SET y argumentos numericos) -------------

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"min": min, "max": max, "abs": abs, "round": round, "int": int, "float": float}


def safe_eval(expr, names=None):
    """Evalua aritmetica simple: numeros, + - * / // % **, parentesis, min/max/abs/round."""
    names = names or {}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            return _BINOPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNOPS:
            return _UNOPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in names:
            return names[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("expresion no permitida: %r" % (expr,))

    try:
        return ev(ast.parse(str(expr).strip(), mode="eval"))
    except SyntaxError:
        raise ValueError("expresion invalida: %r" % (expr,))


def to_number(text, names=None):
    """Convierte un token a numero: acepta '32', '32.5', '40/2', '10s' (-> segundos)."""
    if isinstance(text, (int, float)):
        return text
    s = str(text).strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if _DUR_RE.match(s):
        return parse_duration(s)
    return safe_eval(s, names)


# ---- colores de consola ---------------------------------------------------------

def _enable_vt():
    if os.name == "nt":
        try:
            os.system("")  # activa secuencias ANSI en conhost/Windows Terminal
        except Exception:
            pass


_enable_vt()
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else str(text)


def green(t): return c(t, "32")
def red(t): return c(t, "31")
def yellow(t): return c(t, "33")
def dim(t): return c(t, "2")
def bold(t): return c(t, "1")
