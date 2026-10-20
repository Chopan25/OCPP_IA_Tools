"""report.html: una sola pagina autocontenida (sin CDN) con resumen, verificaciones, graficos y eventos."""
import html
import json

STATUS_LABEL = {
    "passed": ("&#10003;", "APROBADO", "ok"),
    "failed": ("&#10007;", "FALLO", "bad"),
    "error": ("!", "ERROR", "bad"),
    "aborted": ("&#9632;", "CORTADO", "warn"),
}


def _series(engine):
    """Series 1 Hz por conector para los graficos."""
    out = {}
    for r in engine.rec.samples:
        key = "%s.%d" % (r["charger"], r["connector"])
        d = out.setdefault(key, {"t": [], "limit": [], "measured": [], "actual": [], "status": [], "demand": []})
        d["t"].append(r["t"])
        d["limit"].append(r["limit_a"])
        d["measured"].append(r["measured_a"])
        d["actual"].append(r["actual_a"])
        d["status"].append(r["status"])
        d["demand"].append(r["demand_a"])
    for key, d in out.items():
        cid = key.split(".")[0]
        d["max_a"] = engine.cps[cid].cfg.max_a
        d["active"] = any(st != "Available" for st in d["status"]) or any(d["measured"]) or any(d["demand"])
    return out


def _fmt(v, d=2):
    return "-" if v is None else ("%.*f" % (d, v) if isinstance(v, float) else str(v))


def render_html(engine, data):
    res = data
    icon, label, cls = STATUS_LABEL[res["status"]]
    m = res["metrics"]
    c = res["checks"]
    lat = [e["since_stimulus_s"] for e in res["expectations"] if e["ok"] and e["since_stimulus_s"] is not None]
    marks = [{"t": e["t"], "text": e["text"], "kind": "mark"} for e in engine.rec.events if e["kind"] == "mark"]
    ticks = [{"t": e["t"], "text": e["text"], "kind": "stim"} for e in engine.rec.events if e["stimulus"]]
    payload = {
        "duration": max(res["duration_s"], 1),
        "siteLimit": res["site_limit_a"],
        "site": [{"t": t, "measured": mm, "actual": a, "limit": l} for t, mm, a, l in engine.site_samples],
        "conn": _series(engine),
        "marks": marks,
        "ticks": ticks,
    }
    pj = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    def esc(x):
        return html.escape(str(x))

    exp_rows = []
    for e in res["expectations"]:
        ok = e["ok"]
        exp_rows.append(
            "<tr class='%s'><td><span class='st %s'>%s %s</span></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td class='num'>%s</td><td class='num'>%s</td></tr>" % (
                "" if ok else "rowbad", "ok" if ok else "bad", "&#10003;" if ok else "&#10007;", "OK" if ok else "FALLO",
                esc(e["where"]), esc(e["text"]), esc(e["expected"]), esc(e["got"]),
                _fmt(e["waited_s"], 1), _fmt(e["since_stimulus_s"], 1)))
    inv_rows = []
    for inv in res["invariants"]:
        bad = [v for v in inv["violations"] if v["exceeds_grace"]]
        worst = max((v["duration"] for v in inv["violations"]), default=0)
        peak = ""
        if inv["violations"]:
            peak = _fmt(max(inv["violations"], key=lambda v: v["worst_gap"])["peak"], 2)
        inv_rows.append(
            "<tr class='%s'><td><span class='st %s'>%s %s</span></td><td>%s</td><td>%s %s %s <span class='muted'>(tol %s A, gracia %s s)</span></td>"
            "<td class='num'>%d</td><td class='num'>%s</td><td class='num'>%s</td></tr>" % (
                "rowbad" if bad else "", "bad" if bad else "ok", "&#10007;" if bad else "&#10003;", "FALLO" if bad else "OK",
                esc(inv["id"]), esc(inv["metric"]), esc(inv["op"]), _fmt(inv["value"], 1), _fmt(inv["tol"], 1), _fmt(inv["grace_s"], 1),
                len(inv["violations"]), _fmt(worst, 2) if worst else "-", peak or "-"))
    win_rows = []
    for w in res["event_windows"]:
        win_rows.append("<tr><td class='num'>%.1f</td><td>%s</td><td class='num'>%d</td><td class='num'>%s</td><td class='num'>%s</td></tr>" % (
            w["t"], esc(w["label"]), w["set_charging_profile"],
            _fmt(w["first_profile_after_s"], 1), _fmt(w["last_profile_after_s"], 1)))
    ev_rows = []
    for e in res["events"]:
        if e["kind"] in ("status", "csms", "tx", "link", "script", "mark", "fail", "error", "plan", "print", "snapshot", "abort"):
            ev_rows.append("<tr><td class='num'>%.2f</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                e["t"], esc(e["kind"]), esc(e["charger"] or ""), esc(e["text"])))
    msg_rows = []
    for cid, d in res["messages_per_charger"].items():
        parts = ", ".join("%s &times; %d" % (esc(k), n) for k, n in sorted(d.items()))
        msg_rows.append("<tr><td>%s</td><td>%s</td></tr>" % (esc(cid), parts))
    notes = "".join("<li>%s</li>" % esc(n) for n in res["notes"])
    err = ""
    if res.get("error"):
        err = "<div class='callout bad'><b>Error:</b> %s</div>" % esc(res["error"])
    elif res.get("aborted") and res.get("abort_reason"):
        err = "<div class='callout warn'><b>Corrida cortada:</b> %s</div>" % esc(res["abort_reason"])

    eff = "-" if m["efficiency"] is None else "%.0f%%" % (m["efficiency"] * 100)
    tiles = [
        ("Verificaciones", "%d / %d" % (c["passed"], c["total"]), "%d expectativas, %d invariantes" % (c["expectations"], c["invariants"]),
         "bad" if c["failed"] else "ok"),
        ("Duración", "%.0f s" % res["duration_s"], "modo %s, seed %s" % (res["mode"], res["seed"]), ""),
        ("Sobrecarga máx.", "%.2f A" % m["peak_overload_a"], "sobre el límite de %g A" % res["site_limit_a"],
         "bad" if m["peak_overload_a"] > 0.5 else ""),
        ("Tiempo sobre el límite", "%.1f s" % m["time_over_limit_s"], "corriente medida del sitio", "bad" if m["time_over_limit_s"] > 5 else ""),
        ("Convergencia máx.", "-" if not lat else "%.1f s" % max(lat), "desde el último evento hasta cumplirse", ""),
        ("Energía entregada", "%.0f Wh" % m["delivered_wh"], "de %.0f Wh ideales (%s)" % (m["ideal_wh"], eff), ""),
    ]
    tile_html = "".join(
        "<div class='tile %s'><div class='tl'>%s</div><div class='tv'>%s</div><div class='ts'>%s</div></div>" % (t[3], esc(t[0]), esc(t[1]), esc(t[2]))
        for t in tiles)
    chargers = "".join("<tr><td>%s</td><td>%s</td><td class='num'>%g A</td><td class='num'>%d</td><td class='num'>%d</td><td>%s</td></tr>" % (
        esc(cid), esc(v["serial"]), v["max_a"], v["phases"], v["connectors"],
        esc("%s %g A %g%%" % (v["deviation"]["type"], v["deviation"].get("abs_a", 0), v["deviation"].get("pct", 0)) if v["deviation"]["type"] != "none" else "sin desviación"))
        for cid, v in res["chargers"].items())

    page = PAGE
    for k, v in {
        "__TITLE__": esc(res["scenario"]),
        "__SCRIPT__": esc(res["script"]),
        "__STARTED__": esc(res["started"]),
        "__DESC__": esc(res.get("description") or ""),
        "__CLS__": cls, "__ICON__": icon, "__LABEL__": label,
        "__TILES__": tile_html, "__ERR__": err,
        "__EXP__": "".join(exp_rows) or "<tr><td colspan='7' class='muted'>Sin expectativas.</td></tr>",
        "__INV__": "".join(inv_rows) or "<tr><td colspan='6' class='muted'>Sin invariantes (ASSERT_ALWAYS).</td></tr>",
        "__WIN__": "".join(win_rows) or "<tr><td colspan='5' class='muted'>Sin eventos.</td></tr>",
        "__EVENTS__": "".join(ev_rows), "__MSGS__": "".join(msg_rows),
        "__NOTES__": ("<h2>Notas</h2><ul>%s</ul>" % notes) if notes else "",
        "__CHARGERS__": chargers,
        "__DATA__": pj,
    }.items():
        page = page.replace(k, v)
    return page


PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte evsim</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,.10);
  --s1: #2a78d6; --s2: #eb6834;
  --good: #006300; --bad: #d03b3b; --warn: #8a5b00;
  --good-bg: rgba(12,163,12,.10); --bad-bg: rgba(208,59,59,.10); --warn-bg: rgba(250,178,25,.18);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,.10);
    --s1: #3987e5; --s2: #d95926;
    --good: #0ca30c; --bad: #ec6a6a; --warn: #fab219;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,.10);
  --s1: #3987e5; --s2: #d95926;
  --good: #0ca30c; --bad: #ec6a6a; --warn: #fab219;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 16px; margin: 32px 0 10px; font-weight: 600; }
.sub { color: var(--ink-2); margin: 0; }
.muted { color: var(--muted); }
.head { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; }
.badge { display: inline-flex; gap: 8px; align-items: center; padding: 6px 14px; border-radius: 999px;
  font-weight: 600; letter-spacing: .02em; border: 1px solid var(--ring); }
.badge.ok { color: var(--good); background: var(--good-bg); }
.badge.bad { color: var(--bad); background: var(--bad-bg); }
.badge.warn { color: var(--warn); background: var(--warn-bg); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 18px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 12px 14px; }
.tl { color: var(--ink-2); font-size: 12px; }
.tv { font-size: 24px; font-weight: 600; margin: 2px 0; }
.tile.bad .tv { color: var(--bad); }
.tile.ok .tv { color: var(--good); }
.ts { color: var(--muted); font-size: 12px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 14px 14px 10px; margin-bottom: 14px; }
.card h3 { font-size: 14px; margin: 0 0 2px; font-weight: 600; }
.card .hint { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 4px 0 6px; color: var(--ink-2); font-size: 12px; }
.legend i { display: inline-block; width: 18px; height: 0; border-top: 2px solid; vertical-align: middle; margin-right: 6px; }
.chart { position: relative; }
.chart svg { display: block; width: 100%; overflow: visible; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--axis);
  border-radius: 8px; padding: 6px 9px; font-size: 12px; line-height: 1.45; box-shadow: 0 4px 14px rgba(0,0,0,.14);
  display: none; z-index: 5; max-width: 260px; }
.tip b { font-weight: 600; }
.tip .k { display: inline-block; width: 10px; height: 0; border-top: 2px solid; margin-right: 6px; vertical-align: middle; }
table { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; font-size: 12px; background: var(--page); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: 0; }
tr.rowbad td { background: var(--bad-bg); }
.st { font-weight: 600; white-space: nowrap; }
.st.ok { color: var(--good); } .st.bad { color: var(--bad); }
.tablewrap { overflow-x: auto; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--ink-2); }
.callout { padding: 10px 14px; border-radius: 8px; margin-top: 14px; border: 1px solid var(--ring); }
.callout.bad { background: var(--bad-bg); color: var(--ink); }
.callout.warn { background: var(--warn-bg); color: var(--ink); }
.ax { fill: var(--muted); font-size: 11px; }
.lbl { fill: var(--ink-2); font-size: 11px; }
.mk { fill: var(--ink-2); font-size: 10.5px; }
@media (forced-colors: active) { .legend i { border-top-width: 3px; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <div>
      <h1>__TITLE__</h1>
      <p class="sub">__DESC__</p>
      <p class="sub muted">__SCRIPT__ &middot; inicio __STARTED__</p>
    </div>
    <span class="badge __CLS__"><span>__ICON__</span> __LABEL__</span>
  </div>
  __ERR__
  <div class="tiles">__TILES__</div>

  <h2>Verificaciones</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Estado</th><th>Línea</th><th>Verificación</th><th>Esperado</th><th>Obtenido</th><th class="num">Espera (s)</th><th class="num">Tras evento (s)</th></tr></thead>
    <tbody>__EXP__</tbody>
  </table></div>
  <h2>Invariantes</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Estado</th><th>Id</th><th>Regla</th><th class="num">Violaciones</th><th class="num">Mayor duración (s)</th><th class="num">Peor valor</th></tr></thead>
    <tbody>__INV__</tbody>
  </table></div>

  <h2>Corriente</h2>
  <div class="card">
    <h3>Sitio</h3>
    <p class="hint">Suma de corriente por fase (la fase más cargada) contra el límite del sitio. Las líneas verticales son los MARK del script; los triángulos, los eventos.</p>
    <div class="legend" id="lg-site"></div>
    <div class="chart" id="ch-site"></div>
    <details><summary>Ver como tabla</summary><div class="tablewrap" id="tb-site"></div></details>
  </div>
  <div id="conns"></div>

  <h2>Reacción del CSMS a cada evento</h2>
  <div class="tablewrap"><table>
    <thead><tr><th class="num">t (s)</th><th>Evento</th><th class="num">SetChargingProfile</th><th class="num">Primero tras (s)</th><th class="num">Último tras (s)</th></tr></thead>
    <tbody>__WIN__</tbody>
  </table></div>
  <p class="muted">Muchos SetChargingProfile por evento, o un último perfil muy tardío, indican oscilación o lentitud del reparto.</p>

  <h2>Cargadores</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Id</th><th>Serial</th><th class="num">Máx.</th><th class="num">Fases</th><th class="num">Conectores</th><th>Desviación de corriente</th></tr></thead>
    <tbody>__CHARGERS__</tbody>
  </table></div>
  <details><summary>Mensajes OCPP por cargador</summary><div class="tablewrap"><table><tbody>__MSGS__</tbody></table></div>
    <p class="muted">El detalle crudo está en messages.log; las muestras en samples.csv y site.csv.</p></details>
  <details><summary>Registro de eventos</summary><div class="tablewrap"><table>
    <thead><tr><th class="num">t (s)</th><th>Tipo</th><th>Cargador</th><th>Detalle</th></tr></thead><tbody>__EVENTS__</tbody></table></div></details>
  __NOTES__
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var D = JSON.parse(document.getElementById('data').textContent);
  var NS = 'http://www.w3.org/2000/svg';
  function el(n, a, p) { var e = document.createElementNS(NS, n); for (var k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; }
  function f1(v) { return v == null ? '-' : (Math.round(v * 10) / 10).toFixed(1); }
  function niceMax(v) { var s = [8, 12, 16, 20, 24, 32, 40, 48, 60, 80, 100, 120, 160, 200, 240, 320, 400]; for (var i = 0; i < s.length; i++) if (v <= s[i]) return s[i]; return Math.ceil(v / 100) * 100; }

  function chart(host, cfg) {
    host.innerHTML = '';
    var W = Math.max(host.clientWidth, 320), H = cfg.h;
    var m = { l: 40, r: 120, t: 22, b: 24 };
    var iw = W - m.l - m.r, ih = H - m.t - m.b;
    var T = D.duration;
    var X = function (t) { return m.l + (t / T) * iw; };
    var Y = function (v) { return m.t + ih - (v / cfg.yMax) * ih; };
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, height: H, role: 'img', 'aria-label': cfg.title }, host);
    // grid + y ticks
    var ticks = 4;
    for (var i = 0; i <= ticks; i++) {
      var v = cfg.yMax * i / ticks, y = Y(v);
      el('line', { x1: m.l, x2: W - m.r, y1: y, y2: y, style: 'stroke:' + (i === 0 ? 'var(--axis)' : 'var(--grid)') + ';stroke-width:1' }, svg);
      var tx = el('text', { x: m.l - 6, y: y + 4, 'text-anchor': 'end', class: 'ax' }, svg); tx.textContent = Math.round(v * 10) / 10;
    }
    var yu = el('text', { x: m.l - 6, y: m.t - 8, 'text-anchor': 'end', class: 'ax' }, svg); yu.textContent = 'A';
    // x ticks
    var step = T <= 60 ? 10 : T <= 180 ? 30 : T <= 600 ? 60 : 300;
    for (var s = 0; s <= T; s += step) {
      var xt = el('text', { x: X(s), y: H - 6, 'text-anchor': 'middle', class: 'ax' }, svg); xt.textContent = s + ' s';
    }
    // marks (MARK del script)
    var lastEnd = -1, row = 0;
    D.marks.forEach(function (mk) {
      var x = X(mk.t);
      el('line', { x1: x, x2: x, y1: m.t, y2: m.t + ih, style: 'stroke:var(--axis);stroke-width:1' }, svg);
      var txt = mk.text.length > 26 ? mk.text.slice(0, 25) + '…' : mk.text;
      var w = txt.length * 5.6 + 6;
      if (x < lastEnd) row = 1 - row; else row = 0;
      if (x + w <= W - m.r + 40) {
        var lb = el('text', { x: x + 3, y: m.t - 5 - 0, class: 'mk' }, svg); lb.textContent = txt;
        lastEnd = x + w;
      }
    });
    // eventos (triangulos en el eje)
    D.ticks.forEach(function (tk) {
      var x = X(tk.t), y = m.t + ih;
      el('path', { d: 'M' + (x - 3.5) + ' ' + (y + 6) + 'L' + (x + 3.5) + ' ' + (y + 6) + 'L' + x + ' ' + (y + 0.5) + 'Z', style: 'fill:var(--muted)' }, svg);
    });
    // referencia
    var refLabel = null;
    if (cfg.ref) {
      var yr = Y(cfg.ref.value);
      el('line', { x1: m.l, x2: W - m.r, y1: yr, y2: yr, style: 'stroke:var(--ink);stroke-width:1.5;stroke-dasharray:6 4' }, svg);
      refLabel = { y: yr, text: cfg.ref.label, ref: true };
    }
    // series
    var endLabels = [];
    cfg.series.forEach(function (s) {
      var d = '';
      for (var i = 0; i < s.t.length; i++) {
        var x = X(s.t[i]), y = Y(s.v[i]);
        if (i === 0) d += 'M' + x + ' ' + y;
        else if (s.step) d += 'H' + x + 'V' + y;
        else d += 'L' + x + ' ' + y;
      }
      el('path', { d: d, style: 'fill:none;stroke:var(' + s.color + ');stroke-width:2;stroke-linejoin:round;stroke-linecap:round' }, svg);
      if (s.t.length) endLabels.push({ y: Y(s.v[s.v.length - 1]), text: s.name + ' ' + f1(s.v[s.v.length - 1]), s: s });
    });
    if (refLabel) endLabels.unshift(refLabel);           // la referencia tiene prioridad sobre las series
    endLabels = endLabels.map(function (l, i) { l.i = i; return l; });
    endLabels.sort(function (a, b) { return a.y - b.y || a.i - b.i; });
    var prev = -99;
    endLabels.forEach(function (l) {
      if (l.y - prev < 13) return;   // si chocan, la leyenda y el tooltip llevan la identidad
      prev = l.y;
      var t = el('text', { x: (m.l + iw) + 6, y: l.y + 4, class: 'lbl' }, svg); t.textContent = l.text;
    });
    // hover
    var cross = el('line', { y1: m.t, y2: m.t + ih, style: 'stroke:var(--ink-2);stroke-width:1;display:none' }, svg);
    var dots = cfg.series.map(function (s) { return el('circle', { r: 4, style: 'fill:var(' + s.color + ');stroke:var(--surface);stroke-width:2;display:none' }, svg); });
    var tip = document.createElement('div'); tip.className = 'tip'; host.appendChild(tip);
    var hit = el('rect', { x: m.l, y: m.t, width: iw, height: ih, style: 'fill:transparent' }, svg);
    hit.addEventListener('mousemove', function (ev) {
      var r = svg.getBoundingClientRect(), sx = W / r.width;
      var t = ((ev.clientX - r.left) * sx - m.l) / iw * T;
      var base = cfg.series[0].t, lo = 0, hi = base.length - 1;
      while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (base[mid] <= t) lo = mid; else hi = mid; }
      var idx = (Math.abs(base[lo] - t) <= Math.abs(base[hi] - t)) ? lo : hi;
      var x = X(base[idx]);
      cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.style.display = '';
      var html = '<b>t = ' + f1(base[idx]) + ' s</b>';
      cfg.series.forEach(function (s, i) {
        dots[i].setAttribute('cx', x); dots[i].setAttribute('cy', Y(s.v[idx])); dots[i].style.display = '';
        html += '<br><span class="k" style="border-color:var(' + s.color + ')"></span>' + s.name + ': <b>' + f1(s.v[idx]) + ' A</b>';
      });
      if (cfg.status) html += '<br>Estado: ' + cfg.status[idx];
      D.ticks.concat(D.marks).forEach(function (e) { if (Math.abs(e.t - base[idx]) <= 1.2) html += '<br><span class="ax">' + e.text.replace(/</g, '&lt;') + '</span>'; });
      tip.innerHTML = html; tip.style.display = 'block';
      var px = (x / sx) + 12; if (px > r.width - 250) px = (x / sx) - 262;
      tip.style.left = Math.max(px, 4) + 'px'; tip.style.top = '8px';
    });
    hit.addEventListener('mouseleave', function () {
      tip.style.display = 'none'; cross.style.display = 'none'; dots.forEach(function (d) { d.style.display = 'none'; });
    });
  }

  function legend(id, items) {
    document.getElementById(id).innerHTML = items.map(function (i) {
      return '<span><i style="border-color:' + i.c + (i.dash ? ';border-top-style:dashed' : '') + '"></i>' + i.n + '</span>';
    }).join('');
  }
  function table(id, head, rows) {
    var h = '<table><thead><tr>' + head.map(function (x) { return '<th class="num">' + x + '</th>'; }).join('') + '</tr></thead><tbody>';
    rows.forEach(function (r) { h += '<tr>' + r.map(function (x) { return '<td class="num">' + x + '</td>'; }).join('') + '</tr>'; });
    document.getElementById(id).innerHTML = h + '</tbody></table><p class="muted">Muestras cada 1 s' + (rows.length >= 200 ? ' (tabla recortada; el CSV tiene todo)' : '') + '.</p>';
  }

  var siteT = D.site.map(function (r) { return r.t; });
  var peak = Math.max.apply(null, D.site.map(function (r) { return Math.max(r.measured, r.limit); }).concat([D.siteLimit]));
  var siteCfg = {
    title: 'Corriente del sitio', h: 230, yMax: niceMax(peak * 1.1), ref: { value: D.siteLimit, label: 'Límite ' + f1(D.siteLimit) + ' A' },
    series: [
      { name: 'Medida', color: '--s1', t: siteT, v: D.site.map(function (r) { return r.measured; }) },
      { name: 'Asignada', color: '--s2', step: true, t: siteT, v: D.site.map(function (r) { return r.limit; }) }
    ]
  };
  legend('lg-site', [{ n: 'Corriente medida (suma, peor fase)', c: 'var(--s1)' }, { n: 'Límites asignados (suma, peor fase)', c: 'var(--s2)' }, { n: 'Límite del sitio', c: 'var(--ink)', dash: 1 }]);
  var step = Math.max(1, Math.ceil(D.site.length / 200));
  table('tb-site', ['t (s)', 'Medida (A)', 'Real sin desviación (A)', 'Asignada (A)'],
    D.site.filter(function (_, i) { return i % step === 0; }).map(function (r) { return [f1(r.t), f1(r.measured), f1(r.actual), f1(r.limit)]; }));

  var host = document.getElementById('conns');
  var keys = Object.keys(D.conn).filter(function (k) { return D.conn[k].active; });
  var idle = Object.keys(D.conn).filter(function (k) { return !D.conn[k].active; });
  if (idle.length) { var p = document.createElement('p'); p.className = 'muted'; p.textContent = 'Sin actividad en esta corrida: ' + idle.join(', '); }
  var cards = keys.map(function (k, n) {
    var c = D.conn[k], card = document.createElement('div'); card.className = 'card';
    card.innerHTML = '<h3>Cargador ' + k + '</h3><p class="hint">Máximo físico ' + f1(c.max_a) + ' A</p><div class="legend"><span><i style="border-color:var(--s1)"></i>Corriente medida</span><span><i style="border-color:var(--s2)"></i>Límite ofrecido</span></div><div class="chart"></div><details><summary>Ver como tabla</summary><div class="tablewrap"></div></details>';
    host.appendChild(card);
    var st = Math.max(1, Math.ceil(c.t.length / 200));
    var h = '<table><thead><tr><th class="num">t (s)</th><th>Estado</th><th class="num">Demanda (A)</th><th class="num">Límite (A)</th><th class="num">Real (A)</th><th class="num">Medida (A)</th></tr></thead><tbody>';
    c.t.forEach(function (t, i) { if (i % st === 0) h += '<tr><td class="num">' + f1(t) + '</td><td>' + c.status[i] + '</td><td class="num">' + f1(c.demand[i]) + '</td><td class="num">' + f1(c.limit[i]) + '</td><td class="num">' + f1(c.actual[i]) + '</td><td class="num">' + f1(c.measured[i]) + '</td></tr>'; });
    card.querySelector('.tablewrap').innerHTML = h + '</tbody></table>';
    return { host: card.querySelector('.chart'), cfg: { title: 'Cargador ' + k, h: 170, yMax: niceMax(c.max_a * 1.1), status: c.status,
      series: [{ name: 'Medida', color: '--s1', t: c.t, v: c.measured }, { name: 'Límite', color: '--s2', step: true, t: c.t, v: c.limit }] } };
  });
  if (idle.length) host.appendChild(p);
  function draw() { chart(document.getElementById('ch-site'), siteCfg); cards.forEach(function (c) { chart(c.host, c.cfg); }); }
  draw();
  var rt; window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(draw, 120); });
})();
</script>
</body>
</html>
"""
