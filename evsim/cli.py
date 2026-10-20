"""Linea de comandos de evsim."""
import argparse
import asyncio
import glob
import os
import re
import sys
import time

from . import __version__
from .config import ConfigError, load_config
from .initcfg import safe_url
from .script import ScriptError, parse_file, walk
from .util import bold, dim, green, red, yellow

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CONFIG_NAMES = ("sitio.json", "site.json", "config.json", "sitio.example.json")   # el ultimo es el mock local


def find_config(path):
    if path:
        return path
    for base in (os.getcwd(), ROOT):
        for n in DEFAULT_CONFIG_NAMES:
            p = os.path.join(base, n)
            if os.path.isfile(p):
                return p
    raise ConfigError("no encontre archivo de config (busque %s en %s y %s). Copia sitio.example.json a sitio.json "
                      "o usa --config" % ("/".join(DEFAULT_CONFIG_NAMES), os.getcwd(), ROOT))


def find_script(name):
    cands = [name, name + ".evs", os.path.join(ROOT, "scripts", name), os.path.join(ROOT, "scripts", name + ".evs"),
             os.path.join(os.getcwd(), "scripts", name), os.path.join(os.getcwd(), "scripts", name + ".evs")]
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    raise ScriptError("no encontre el script %r (busque en ./ y scripts/)" % name)


def cmd_check(args):
    try:
        cfg = load_config(find_config(args.config))
        path = find_script(args.script)
        prog = parse_file(path, known_chargers=cfg.chargers.keys())
    except (ConfigError, ScriptError) as e:
        print(red("ERROR: %s" % e))
        return 2
    steps = list(walk(prog))
    verbs = {}
    for s in steps:
        verbs[s.verb] = verbs.get(s.verb, 0) + 1
    exp = sum(n for v, n in verbs.items() if v.startswith("EXPECT_"))
    asr = verbs.get("ASSERT_ALWAYS", 0)
    print(green("OK") + "  %s" % path)
    print("    %d instrucciones, %d EXPECT, %d ASSERT_ALWAYS; cargadores en config: %s" % (
        len(steps), exp, asr, ", ".join(cfg.chargers)))
    print(dim("    verbos: " + ", ".join("%s x%d" % (v, n) for v, n in sorted(verbs.items()))))
    return 0


def cmd_list(args):
    sdir = os.path.join(ROOT, "scripts")
    print(bold("Scripts") + dim("  (%s)" % sdir))
    for p in sorted(glob.glob(os.path.join(sdir, "*.evs"))):
        title = ""
        with open(p, encoding="utf-8-sig") as f:
            for line in f:
                m = re.match(r"^\s*SCENARIO\s+(.*)$", line, re.I)
                if m:
                    title = m.group(1).split(";")[0].strip().strip("\"'")
                    break
        print("  %-28s %s" % (os.path.basename(p), dim(title)))
    try:
        cfg = load_config(find_config(args.config))
    except ConfigError as e:
        print(yellow("\n(sin config: %s)" % e))
        return 0
    print("\n" + bold("Cargadores") + dim("  (%s)" % cfg.path))
    print("  sitio: limite %g A, %d fases, %g V" % (cfg.site_limit_a, cfg.site_phases, cfg.site_voltage))
    for c in cfg.chargers.values():
        dev = c.deviation
        print("  %-6s serial=%-14s max=%gA fases=%d conectores=%d desviacion=%s  %s" % (
            c.id, c.serial, c.max_a, c.phases, c.connectors,
            "%s(%g A, %g%%)" % (dev["type"], dev.get("abs_a", 0), dev.get("pct", 0)) if dev["type"] != "none" else "no", dim(safe_url(c.url))))
    return 0


async def _run(args):
    from .engine import Engine
    from .report import print_summary, write_reports
    cfg = load_config(find_config(args.config))
    path = find_script(args.script)
    prog = parse_file(path, known_chargers=cfg.chargers.keys())
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(args.out, "%s_%s" % (stamp, os.path.splitext(os.path.basename(path))[0]))
    mode = args.mode or "strict"
    for s in prog:  # MODE declarado en el script manda sobre el default, no sobre --mode
        if s.verb == "MODE" and not args.mode:
            mode = s.args[0].lower()
    engine = Engine(cfg, prog, out_dir, path, mode=mode, seed=args.seed, verbose=not args.quiet)
    try:
        result = await engine.run()
    except asyncio.CancelledError:
        result = engine.result()
        result["status"] = "aborted"
        result["abort_reason"] = "interrumpido por el usuario (Ctrl+C)"
    data = write_reports(engine, result, out_dir, html=not args.no_html)
    print_summary(result, data, out_dir)
    return {"passed": 0, "failed": 1, "aborted": 1, "error": 3}[result["status"]]


def cmd_run(args):
    try:
        return asyncio.run(_run(args))
    except (ConfigError, ScriptError) as e:
        print(red("ERROR: %s" % e))
        return 2
    except KeyboardInterrupt:
        print(yellow("interrumpido"))
        return 1


def cmd_summary(args):
    from .summary import latest_run, summarize
    d = args.dir or latest_run(args.out)
    if not d or not os.path.isfile(os.path.join(d, "report.json")):
        print(red("ERROR: no encontre ninguna corrida en %s (corre primero evsim run)" % args.out))
        return 2
    print(summarize(d))
    return 0


def cmd_init(args):
    from .initcfg import InitError, build_config, mask, split_link, write_config
    try:
        cfg = build_config(args.links, limit_a=args.limit, max_a=args.max_a, phases=args.phases,
                           id_tag=args.id_tag, sample_s=args.sample)
        out = args.out
        write_config(cfg, out, force=args.force)
        load_config(out)          # valida que lo generado sea una config correcta
    except (InitError, ConfigError) as e:
        print(red("ERROR: %s" % e))
        return 2
    print(green("OK") + "  config creada: %s  (contiene credenciales: no la compartas)" % os.path.abspath(out))
    print("    limite del sitio: %g A | cada cargador: %g A, %d fases | idTag: %s" % (args.limit, args.max_a, args.phases, args.id_tag))
    for c in cfg["chargers"]:
        link = next(l for l in args.links if split_link(l)[1] == c["serial"])
        print("    %-3s serial %-14s %s" % (c["id"], c["serial"], dim(mask(link))))
    print(dim("    en los scripts los cargadores se llaman: " + ", ".join(c["id"] for c in cfg["chargers"])))
    print("    siguiente paso:  evsim run smoke_estados -c %s" % out)
    return 0


def cmd_mock(args):
    from .mockcsms import serve_forever
    try:
        asyncio.run(serve_forever(args.limit, args.cap, args.host, args.port, args.meter_interval, args.headroom))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evsim", description="Banco de pruebas de smart charging / DLB para OCPP 1.6J")
    ap.add_argument("--version", action="version", version="evsim " + __version__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="corre un script contra el CSMS y genera el reporte")
    p.add_argument("script", help="archivo .evs (o nombre dentro de scripts/)")
    p.add_argument("-c", "--config", help="config JSON (default: sitio.json)")
    p.add_argument("-o", "--out", default=os.path.join(ROOT, "out"), help="carpeta base de reportes (default: out/)")
    p.add_argument("--mode", choices=["strict", "soft"], help="strict corta en el primer fallo; soft sigue y registra")
    p.add_argument("--seed", type=int, help="fuerza la semilla (pisa SEED del script)")
    p.add_argument("--no-html", action="store_true", help="no generar report.html")
    p.add_argument("-q", "--quiet", action="store_true", help="sin salida paso a paso")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("check", help="valida sintaxis y referencias del script sin conectar nada")
    p.add_argument("script")
    p.add_argument("-c", "--config")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("list", help="lista scripts y cargadores configurados")
    p.add_argument("-c", "--config")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("init", help="crea la config a partir de los links de conexion de los cargadores")
    p.add_argument("links", nargs="+", help="un link ws:// o wss:// por cargador (el ultimo tramo es el serial)")
    p.add_argument("-o", "--out", default=os.path.join(ROOT, "sitio_real.json"), help="archivo a crear (default: sitio_real.json)")
    p.add_argument("--limit", type=float, default=40.0, help="limite del grupo/sitio en A (default 40)")
    p.add_argument("--max-a", type=float, default=32.0, help="corriente maxima de cada cargador en A (default 32)")
    p.add_argument("--phases", type=int, choices=[1, 3], default=3, help="fases de cada cargador (default 3)")
    p.add_argument("--id-tag", default="plug_and_play", help="idTag para arrancar cargas (default plug_and_play)")
    p.add_argument("--sample", type=int, default=5, help="cada cuantos segundos manda MeterValues (default 5)")
    p.add_argument("--force", action="store_true", help="reemplazar el archivo si ya existe")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("summary", help="resumen de texto de una corrida (por defecto la ultima), para analizar resultados")
    p.add_argument("dir", nargs="?", help="carpeta de la corrida (default: la mas reciente de out/)")
    p.add_argument("-o", "--out", default=os.path.join(ROOT, "out"))
    p.set_defaults(fn=cmd_summary)

    p = sub.add_parser("mock-csms", help="CSMS de juguete con DLB, para probar evsim sin tu backend")
    p.add_argument("--limit", type=float, default=40.0, help="limite del sitio en A (default 40)")
    p.add_argument("--cap", type=float, default=32.0, help="tope por cargador en A (default 32)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--meter-interval", type=int, default=2, help="MeterValueSampleInterval que pide el mock (s)")
    p.add_argument("--headroom", type=float, default=0.0, help="A extra sobre lo medido a cargadores limitados por el auto")
    p.set_defaults(fn=cmd_mock)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)
