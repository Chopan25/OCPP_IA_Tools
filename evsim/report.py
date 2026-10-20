"""Reportes de una corrida: consola, report.json, samples.csv y report.html."""
import csv
import json
import os

from .initcfg import safe_url
from .util import bold, dim, green, red, yellow

SAMPLE_FIELDS = ["t", "charger", "connector", "status", "plugged", "tx", "connected", "demand_a",
                 "limit_a", "actual_a", "measured_a", "energy_wh"]


def event_windows(engine):
    """Agrupa los estimulos del script (a menos de 1 s entre si) y cuenta los perfiles que mando el CSMS tras cada grupo."""
    rec = engine.rec
    stim = [e for e in rec.events if e["stimulus"]]
    groups = []
    for e in stim:
        if groups and e["t"] - groups[-1]["t_last"] < 1.0:
            groups[-1]["labels"].append(e["text"])
            groups[-1]["t_last"] = e["t"]
        else:
            groups.append({"t": e["t"], "t_last": e["t"], "labels": [e["text"]]})
    out = []
    for i, g in enumerate(groups):
        t_end = groups[i + 1]["t"] if i + 1 < len(groups) else rec.now() + 1
        times = [m["t"] for m in rec.messages
                 if m["dir"] == "<-" and m["action"] == "SetChargingProfile" and g["t"] <= m["t"] < t_end]
        clears = rec.count_between("ClearChargingProfile", g["t"], t_end)
        out.append({
            "t": g["t"], "label": " + ".join(g["labels"])[:160], "window_s": round(t_end - g["t"], 2),
            "set_charging_profile": len(times), "clear_charging_profile": clears,
            "first_profile_after_s": round(times[0] - g["t"], 2) if times else None,
            "last_profile_after_s": round(times[-1] - g["t"], 2) if times else None,
        })
    return out


def message_summary(engine):
    summary = {}
    for m in engine.rec.messages:
        key = "%s %s" % (m["dir"], m["action"])
        summary.setdefault(m["charger"], {})
        summary[m["charger"]][key] = summary[m["charger"]].get(key, 0) + 1
    return summary


def write_csv(engine, out_dir):
    with open(os.path.join(out_dir, "samples.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SAMPLE_FIELDS)
        w.writeheader()
        for row in engine.rec.samples:
            w.writerow(row)
    with open(os.path.join(out_dir, "site.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "sum_measured_a", "sum_actual_a", "sum_limit_a", "site_limit_a"])
        for t, m, a, l in engine.site_samples:
            w.writerow([t, m, a, l, engine.site_limit])


def build_json(engine, result):
    windows = event_windows(engine)
    out = dict(result)
    out["event_windows"] = windows
    out["events"] = engine.rec.events
    out["messages_per_charger"] = message_summary(engine)
    out["chargers"] = {cid: {"serial": cp.cfg.serial, "url": safe_url(cp.cfg.url), "max_a": cp.cfg.max_a,
                            "phases": cp.cfg.phases, "connectors": cp.cfg.connectors,
                            "deviation": cp.cfg.deviation} for cid, cp in engine.cps.items()}
    return out


def write_reports(engine, result, out_dir, html=True):
    os.makedirs(out_dir, exist_ok=True)
    data = build_json(engine, result)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    write_csv(engine, out_dir)
    if html:
        from .report_html import render_html
        with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
            f.write(render_html(engine, data))
    return data


def print_summary(result, data, out_dir=None):
    st = result["status"]
    badge = {"passed": green("PASSED"), "failed": red("FAILED"), "error": red("ERROR"),
             "aborted": yellow("ABORTED")}[st]
    c = result["checks"]
    print()
    print(bold("=" * 72))
    print("%s  %s" % (badge, bold(result["scenario"])))
    print("duracion %.1fs | modo %s | seed %s | limite del sitio %g A" % (
        result["duration_s"], result["mode"], result["seed"], result["site_limit_a"]))
    print("verificaciones: %d total, %s ok, %s fallidas  (%d expectativas, %d invariantes)" % (
        c["total"], green(str(c["passed"])), (red if c["failed"] else dim)(str(c["failed"])), c["expectations"], c["invariants"]))
    if result.get("error"):
        print(red("error: " + result["error"]))
    if result.get("aborted") and result.get("abort_reason"):
        print(yellow("cortado por: " + str(result["abort_reason"])))
    fails = [e for e in result["expectations"] if not e["ok"]]
    for e in fails:
        print(red("  x %s  %s  -> esperado %s, obtenido %s" % (e["where"], e["text"], e["expected"], e["got"])))
    for inv in result["invariants"]:
        for v in inv["violations"]:
            if v["exceeds_grace"]:
                print(red("  x invariante %s (%s %s %g): violada %.1fs desde t=%.1fs, peor valor %.2f" % (
                    inv["id"], inv["metric"], inv["op"], inv["value"], v["duration"], v["start"], v["peak"])))
    m = result["metrics"]
    print(dim("sitio: sobrecarga maxima %.2f A, %.1fs sobre el limite | energia %.1f Wh de %.1f Wh ideales%s" % (
        m["peak_overload_a"], m["time_over_limit_s"], m["delivered_wh"], m["ideal_wh"],
        " (eficiencia %.0f%%)" % (m["efficiency"] * 100) if m["efficiency"] is not None else "")))
    lat = [e["since_stimulus_s"] for e in result["expectations"] if e["ok"] and e["since_stimulus_s"] is not None]
    if lat:
        print(dim("convergencia tras eventos: min %.1fs, max %.1fs" % (min(lat), max(lat))))
    for n in result["notes"]:
        print(yellow("nota: " + n))
    if out_dir:
        print(dim("reporte: " + os.path.abspath(out_dir)))
    print(bold("=" * 72))
