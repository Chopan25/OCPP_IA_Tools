"""`evsim summary`: resumen de texto de una corrida, pensado para analizar resultados (humano o IA)."""
import glob
import json
import os
import re

_LINE = re.compile(r"^(\S+)\s+([\d.]+)\s+(\S+)\s+(->|<-)\s+(.*)$")


def latest_run(out_base):
    runs = sorted(d for d in glob.glob(os.path.join(out_base, "*")) if os.path.isfile(os.path.join(d, "report.json")))
    return runs[-1] if runs else None


def _profiles_from_log(path):
    """SetChargingProfile / ClearChargingProfile que mando el CSMS y como respondio el cargador."""
    out = []
    if not os.path.isfile(path):
        return out
    pending = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _LINE.match(line.rstrip("\n"))
            if not m:
                continue
            _, t, ch, d, frame = m.groups()
            try:
                msg = json.loads(frame)
            except ValueError:
                continue
            if d == "<-" and msg[0] == 2 and msg[2] in ("SetChargingProfile", "ClearChargingProfile"):
                p = msg[3]
                if msg[2] == "SetChargingProfile":
                    cp = p.get("csChargingProfiles", {})
                    sch = cp.get("chargingSchedule", {})
                    per = sch.get("chargingSchedulePeriod") or [{}]
                    desc = "conector %s %s stack %s: %s%s%s" % (
                        p.get("connectorId"), cp.get("chargingProfilePurpose"), cp.get("stackLevel"),
                        "/".join(str(x.get("limit")) for x in per), sch.get("chargingRateUnit", ""),
                        " (%d periodos)" % len(per) if len(per) > 1 else "")
                else:
                    desc = "ClearChargingProfile %s" % json.dumps(p)
                row = {"t": float(t), "charger": ch, "action": msg[2], "desc": desc, "result": None}
                pending[msg[1]] = row
                out.append(row)
            elif d == "->" and msg[0] == 3 and msg[1] in pending:
                pending[msg[1]]["result"] = (msg[2] or {}).get("status")
    return out


def summarize(out_dir):
    with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as f:
        r = json.load(f)
    L = []
    add = L.append
    c = r["checks"]
    add("CORRIDA   %s" % os.path.basename(out_dir.rstrip("/\\")))
    add("escenario %s | script %s" % (r["scenario"], os.path.basename(r["script"])))
    add("RESULTADO %s | %d verificaciones: %d ok, %d fallidas | duracion %.0fs | modo %s | seed %s | limite sitio %g A" % (
        r["status"].upper(), c["total"], c["passed"], c["failed"], r["duration_s"], r["mode"], r["seed"], r["site_limit_a"]))
    if r.get("error"):
        add("ERROR     %s" % r["error"])
    if r.get("aborted") and r.get("abort_reason"):
        add("CORTADA   %s" % r["abort_reason"])

    fails = [e for e in r["expectations"] if not e["ok"]]
    add("")
    add("== Verificaciones fallidas (%d) ==" % len(fails))
    for e in fails:
        add("  %s  %s -> esperado %s, obtenido %s (espero %.0fs)" % (e["where"], e["text"], e["expected"], e["got"], e["waited_s"]))
    if not fails:
        add("  ninguna")
    for inv in r["invariants"]:
        add("  invariante %-10s %s %s %g (tol %g, gracia %gs): %s" % (
            inv["id"], inv["metric"], inv["op"], inv["value"], inv["tol"], inv["grace_s"],
            "FALLO" if inv["failed"] else "ok"))
        for v in inv["violations"]:
            add("     violacion t=%.1fs duracion %.1fs peor valor %.2f%s" % (
                v["start"], v["duration"], v["peak"], "" if v["exceeds_grace"] else " (dentro de la gracia)"))

    lat = [(e["where"], e["text"], e["since_stimulus_s"]) for e in r["expectations"] if e["ok"] and e["since_stimulus_s"] is not None]
    add("")
    add("== Convergencia (verificaciones que pasaron, segundos desde el ultimo evento) ==")
    if lat:
        for w, t, s in sorted(lat, key=lambda x: -x[2])[:8]:
            add("  %6.1fs  %s  %s" % (s, w, t))
    else:
        add("  sin datos")

    add("")
    add("== Reaccion del CSMS por evento ==")
    for w in r["event_windows"]:
        add("  t=%6.1f  %-70s perfiles=%d primero=%s ultimo=%s" % (
            w["t"], w["label"][:70], w["set_charging_profile"],
            "-" if w["first_profile_after_s"] is None else "%.1fs" % w["first_profile_after_s"],
            "-" if w["last_profile_after_s"] is None else "%.1fs" % w["last_profile_after_s"]))

    prof = _profiles_from_log(os.path.join(out_dir, "messages.log"))
    add("")
    add("== Perfiles de carga recibidos del CSMS (%d) ==" % len(prof))
    if not prof:
        add("  NINGUNO: el CSMS no mando SetChargingProfile (el DLB no actuo, o los cargadores no estan en un grupo con limite)")
    for p in prof[:60]:
        add("  t=%6.1f %-10s %s -> %s" % (p["t"], p["charger"], p["desc"], p["result"] or "sin respuesta"))
    if len(prof) > 60:
        add("  ... %d mas (ver messages.log)" % (len(prof) - 60))

    if r.get("snapshots"):
        add("")
        add("== Fotos del estado (SNAPSHOT) ==")
        for sn in r["snapshots"]:
            site = sn.get("site", {})
            add("  [%s] t=%.1fs  sitio: medido %.1f A, limites asignados %.1f A" % (
                sn["name"], sn["t"], site.get("measured", 0), site.get("limit", 0)))
            for cid, conns in sn.get("chargers", {}).items():
                for k, c in conns.items():
                    if c["status"] == "Available" and not c["tx"]:
                        continue
                    add("      %s.%s %-13s limite %5.1f A | real %5.1f A | demanda del auto %5.1f A" % (
                        cid, k, c["status"], c["limit_a"], c["nominal_a"], c["demand_a"]))

    m = r["metrics"]
    add("")
    add("== Sitio ==")
    add("  sobrecarga maxima %.2f A | tiempo sobre el limite %.1fs | energia %.1f Wh de %.1f Wh ideales%s" % (
        m["peak_overload_a"], m["time_over_limit_s"], m["delivered_wh"], m["ideal_wh"],
        " (eficiencia %.0f%%)" % (m["efficiency"] * 100) if m["efficiency"] is not None else ""))
    add("")
    add("== Mensajes por cargador ==")
    for cid, d in r["messages_per_charger"].items():
        add("  %s: %s" % (cid, ", ".join("%s x%d" % (k, n) for k, n in sorted(d.items()))))
    errs = [e for e in r["events"] if e["kind"] in ("error", "fail")]
    if errs:
        add("")
        add("== Errores/fallos registrados ==")
        for e in errs[:20]:
            add("  t=%.1f %s %s" % (e["t"], e["charger"] or "", e["text"]))
    for n in r["notes"]:
        add("NOTA: %s" % n)
    add("")
    add("archivos: %s (report.html, report.json, messages.log, samples.csv, site.csv)" % out_dir)
    return "\n".join(L)
