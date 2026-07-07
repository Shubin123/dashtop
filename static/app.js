"use strict";

/* ================= formatting ================= */

const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

function fmtBytes(n) {
  n = Math.max(0, +n || 0);
  let i = 0;
  while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
  const digits = i === 0 || n >= 100 ? 0 : 1;
  return n.toFixed(digits) + " " + UNITS[i];
}
const fmtRate = (n) => fmtBytes(n) + "/s";
const fmtPct = (v) => Math.round(+v || 0) + "%";
const fmtPct1 = (v) => (+v || 0).toFixed(1) + "%";

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor(s / 3600) % 24, m = Math.floor(s / 60) % 60;
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${s % 60}s`;
}
const fmtClock = (t) =>
  new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

/* nice byte ceiling: powers of two on a 1024 base, so ticks read 512 KB / 1 MB / 2 MB */
function niceMaxBytes(v) {
  if (!(v > 0)) return 1024;
  let unit = 1;
  while (v / unit >= 1024) unit *= 1024;
  let nice = 1;
  while (nice < v / unit) nice *= 2;
  return nice * unit;
}

/* ================= dom helpers ================= */

const $ = (sel) => document.querySelector(sel);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text; // labels are untrusted data — always textContent
  return e;
}
const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

/* ================= state ================= */

const state = {
  info: null,
  interval: 2,
  latest: null,
  history: [],   // {t, cpu, mem, dn, up, rd, wr}
  windowMin: 5,
  lastMsgAt: 0,
};

function windowedHistory() {
  if (!state.history.length) return [];
  const t1 = state.history[state.history.length - 1].t;
  const t0 = t1 - state.windowMin * 60;
  return state.history.filter((p) => p.t >= t0);
}

/* ================= stat tiles ================= */

const TILE_DEFS = [
  { key: "cpu", label: "CPU", value: (s) => fmtPct(s.cpu.total),
    sub: (s) => (s.cpu.freq ? (s.cpu.freq / 1000).toFixed(2) + " GHz" : ""),
    spark: (p) => p.cpu, yMax: 100 },
  { key: "mem", label: "Memory", value: (s) => fmtPct(s.mem.percent),
    sub: (s) => fmtBytes(s.mem.used) + " of " + fmtBytes(s.mem.total),
    spark: (p) => p.mem, yMax: 100 },
  { key: "dn", label: "Download", value: (s) => fmtRate(s.net.down_bps),
    sub: (s) => "total " + fmtBytes(s.net.recv_total),
    spark: (p) => p.dn },
  { key: "up", label: "Upload", value: (s) => fmtRate(s.net.up_bps),
    sub: (s) => "total " + fmtBytes(s.net.sent_total),
    spark: (p) => p.up },
];

const tileEls = {};

function buildTiles(hasBattery) {
  const wrap = $("#tiles");
  wrap.textContent = "";
  const defs = TILE_DEFS.slice();
  if (hasBattery) {
    defs.push({ key: "bat", label: "Battery",
      value: (s) => s.battery ? s.battery.percent + "%" : "—",
      sub: (s) => s.battery ? (s.battery.plugged ? "plugged in" : (s.battery.secsleft ? fmtDur(s.battery.secsleft) + " left" : "on battery")) : "" });
  }
  for (const def of defs) {
    const tile = el("div", "tile");
    const label = el("div", "label", def.label);
    const value = el("div", "value", "—");
    const sub = el("div", "sub", "");
    tile.append(label, value, sub);
    let svg = null;
    if (def.spark) {
      svg = svgEl("svg", { width: 120, height: 30, role: "img" });
      tile.append(svg);
    }
    wrap.append(tile);
    tileEls[def.key] = { def, value, sub, svg };
  }
}

function renderTiles() {
  const s = state.latest;
  if (!s) return;
  for (const key in tileEls) {
    const { def, value, sub, svg } = tileEls[key];
    value.textContent = def.value(s);
    sub.textContent = def.sub ? def.sub(s) : "";
    if (svg && def.spark) renderSparkline(svg, def);
  }
}

/* 12-point sparkline: line in the de-emphasis hue, current point in the accent */
function renderSparkline(svg, def) {
  svg.textContent = "";
  const pts = state.history.slice(-12).map((p) => def.spark(p));
  if (pts.length < 2) return;
  const w = 120, h = 30, pad = 4;
  const yMax = def.yMax || Math.max(niceMaxBytes(Math.max(...pts)), 1);
  const x = (i) => pad + (i / (pts.length - 1)) * (w - pad * 2);
  const y = (v) => h - pad - (Math.min(v, yMax) / yMax) * (h - pad * 2);
  const d = pts.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1)).join("");
  const line = svgEl("path", { d, fill: "none", "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round" });
  line.style.stroke = "var(--muted)";
  const last = pts.length - 1;
  const dot = svgEl("circle", { cx: x(last).toFixed(1), cy: y(pts[last]).toFixed(1), r: 3.5, "stroke-width": 2 });
  dot.style.fill = "var(--series-1)";
  dot.style.stroke = "var(--surface-1)";
  svg.append(line, dot);
}

/* ================= line charts ================= */

const CHART_DEFS = [
  { id: "cpu", title: "CPU usage",
    series: [{ key: "cpu", name: "CPU", cssVar: "--series-1" }],
    yMax: 100, fmt: fmtPct1, tickFmt: fmtPct },
  { id: "mem", title: "Memory usage",
    series: [{ key: "mem", name: "Memory", cssVar: "--series-1" }],
    yMax: 100, fmt: fmtPct1, tickFmt: fmtPct },
  { id: "net", title: "Network throughput",
    series: [{ key: "dn", name: "Down", cssVar: "--series-1" },
             { key: "up", name: "Up", cssVar: "--series-2" }],
    fmt: fmtRate, tickFmt: fmtRate },
  { id: "io", title: "Disk I/O",
    series: [{ key: "rd", name: "Read", cssVar: "--series-1" },
             { key: "wr", name: "Write", cssVar: "--series-2" }],
    fmt: fmtRate, tickFmt: fmtRate },
];

const charts = [];

function buildCharts() {
  const grid = $("#charts");
  grid.textContent = "";
  for (const def of CHART_DEFS) charts.push(makeLineChart(grid, def));
}

function makeLineChart(parent, def) {
  const card = el("div", "card");
  const head = el("div", "card-head");
  head.append(el("h2", null, def.title));

  // a legend is always present for ≥2 series; a single series is named by the title
  if (def.series.length > 1) {
    const legend = el("div", "legend");
    for (const s of def.series) {
      const key = el("span", "key");
      const swatch = el("i");
      swatch.style.background = `var(${s.cssVar})`;
      key.append(swatch, document.createTextNode(s.name));
      legend.append(key);
    }
    head.append(legend);
  }
  head.append(el("span", "spacer"));
  const tblBtn = el("button", "tbl-btn", "Table");
  tblBtn.type = "button";
  tblBtn.setAttribute("aria-pressed", "false");
  head.append(tblBtn);

  const wrap = el("div", "chart-wrap");
  const svg = svgEl("svg", { role: "img" });
  svg.setAttribute("aria-label", def.title + " history chart");
  const crosshair = el("div", "crosshair");
  const tooltip = el("div", "tooltip");
  wrap.append(svg, crosshair, tooltip);

  const tableWrap = el("div", "chart-table");
  tableWrap.hidden = true;

  card.append(head, wrap, tableWrap);
  parent.append(card);

  const chart = {
    def, svg, wrap, crosshair, tooltip, tableWrap,
    showTable: false,
    hoverFrac: null, // persists pointer position across live re-renders
    geom: null,
    render() { this.showTable ? renderChartTable(this) : renderChartSvg(this); },
  };

  tblBtn.addEventListener("click", () => {
    chart.showTable = !chart.showTable;
    tblBtn.setAttribute("aria-pressed", String(chart.showTable));
    tblBtn.textContent = chart.showTable ? "Chart" : "Table";
    wrap.hidden = chart.showTable;
    tableWrap.hidden = !chart.showTable;
    chart.render();
  });

  svg.addEventListener("pointermove", (e) => onChartPointer(chart, e));
  svg.addEventListener("pointerdown", (e) => onChartPointer(chart, e));
  svg.addEventListener("pointerleave", () => {
    chart.hoverFrac = null;
    hideTooltip(chart);
  });

  return chart;
}

function chartYMax(def, pts) {
  if (def.yMax) return def.yMax;
  let max = 0;
  for (const p of pts) for (const s of def.series) max = Math.max(max, p[s.key]);
  return niceMaxBytes(max);
}

/* split into continuous runs so a server restart shows as a gap, not a false line */
function runsOf(pts, key, maxGap) {
  const runs = [];
  let run = [];
  let prevT = null;
  for (const p of pts) {
    if (prevT !== null && p.t - prevT > maxGap) { if (run.length) runs.push(run); run = []; }
    run.push({ t: p.t, v: p[key] });
    prevT = p.t;
  }
  if (run.length) runs.push(run);
  return runs;
}

function renderChartSvg(chart) {
  const { def, svg } = chart;
  svg.textContent = "";
  const pts = windowedHistory();
  const width = chart.wrap.clientWidth || 600;
  const m = { l: def.yMax ? 40 : 62, r: 14, t: 10, b: 24 };
  const plotH = 156;
  const height = m.t + plotH + m.b; // container includes the x-axis band
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  if (pts.length < 2) {
    const wait = svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "axis-text" });
    wait.textContent = "waiting for samples…";
    svg.append(wait);
    chart.geom = null;
    return;
  }

  const t1 = pts[pts.length - 1].t;
  const t0 = t1 - state.windowMin * 60;
  const plotW = width - m.l - m.r;
  const yMax = chartYMax(def, pts);
  const xOf = (t) => m.l + ((t - t0) / (t1 - t0)) * plotW;
  const yOf = (v) => m.t + plotH - (Math.min(v, yMax) / yMax) * plotH;
  chart.geom = { pts, t0, t1, m, plotW, plotH, yMax, xOf, yOf };

  // gridlines + y ticks: solid hairlines, clean numbers
  const yTicks = def.yMax ? [0, 25, 50, 75, 100] : [0, yMax / 2, yMax];
  for (const v of yTicks) {
    const y = yOf(v);
    const line = svgEl("line", { x1: m.l, x2: m.l + plotW, y1: y, y2: y });
    line.style.stroke = v === 0 ? "var(--axis)" : "var(--grid)";
    line.style.strokeWidth = "1";
    svg.append(line);
    const label = svgEl("text", { x: m.l - 6, y: y + 3.5, "text-anchor": "end", class: "axis-text" });
    label.textContent = def.tickFmt(v);
    svg.append(label);
  }

  // x ticks: four clock labels along the window
  for (let i = 0; i <= 3; i++) {
    const t = t0 + ((t1 - t0) * i) / 3;
    const anchor = i === 0 ? "start" : i === 3 ? "end" : "middle";
    const label = svgEl("text", { x: xOf(t), y: m.t + plotH + 16, "text-anchor": anchor, class: "axis-text" });
    label.textContent = fmtClock(t);
    svg.append(label);
  }

  const maxGap = Math.max(state.interval * 3, 6);
  const endPoints = [];
  def.series.forEach((s, idx) => {
    const runs = runsOf(pts, s.key, maxGap);
    for (const run of runs) {
      const d = run.map((p, i) => (i ? "L" : "M") + xOf(p.t).toFixed(1) + "," + yOf(p.v).toFixed(1)).join("");
      if (def.series.length === 1 && run.length > 1) {
        // single series: a ~10% wash under the line
        const first = run[0], last = run[run.length - 1];
        const area = svgEl("path", {
          d: d + `L${xOf(last.t).toFixed(1)},${(m.t + plotH).toFixed(1)}` +
             `L${xOf(first.t).toFixed(1)},${(m.t + plotH).toFixed(1)}Z`,
          "fill-opacity": "0.1", stroke: "none",
        });
        area.style.fill = `var(${s.cssVar})`;
        svg.append(area);
      }
      const path = svgEl("path", { d, fill: "none", "stroke-width": 2,
        "stroke-linecap": "round", "stroke-linejoin": "round" });
      path.style.stroke = `var(${s.cssVar})`;
      svg.append(path);
    }
    const lastRun = runs[runs.length - 1];
    const end = lastRun[lastRun.length - 1];
    endPoints.push({ s, x: xOf(end.t), y: yOf(end.v), v: end.v, idx });
  });

  // end markers with a surface ring so overlapping dots stay legible
  for (const ep of endPoints) {
    const dot = svgEl("circle", { cx: ep.x.toFixed(1), cy: ep.y.toFixed(1), r: 4, "stroke-width": 2 });
    dot.style.fill = `var(${ep.s.cssVar})`;
    dot.style.stroke = "var(--surface-1)";
    svg.append(dot);
  }

  // selective direct labels: the endpoint value — skipped if two ends collide
  const collide = endPoints.length === 2 && Math.abs(endPoints[0].y - endPoints[1].y) < 15;
  if (!collide) {
    for (const ep of endPoints) {
      const label = svgEl("text", {
        x: (ep.x - 8).toFixed(1),
        y: Math.max(m.t + 10, Math.min(ep.y - 8, m.t + plotH - 4)).toFixed(1),
        "text-anchor": "end", class: "end-label",
      });
      label.textContent = def.fmt(ep.v);
      svg.append(label);
    }
  }

  // re-anchor the tooltip after a live re-render
  if (chart.hoverFrac !== null) showTooltipAtFrac(chart, chart.hoverFrac);
}

function onChartPointer(chart, e) {
  if (!chart.geom) return;
  const rect = chart.svg.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const { m, plotW } = chart.geom;
  const frac = Math.min(1, Math.max(0, (x - m.l) / plotW));
  chart.hoverFrac = frac;
  showTooltipAtFrac(chart, frac);
}

function showTooltipAtFrac(chart, frac) {
  const g = chart.geom;
  if (!g) return;
  const targetT = g.t0 + frac * (g.t1 - g.t0);
  let best = g.pts[0], bestD = Infinity;
  for (const p of g.pts) {
    const d = Math.abs(p.t - targetT);
    if (d < bestD) { bestD = d; best = p; }
  }
  const px = g.xOf(best.t);

  chart.crosshair.style.display = "block";
  chart.crosshair.style.left = px + "px";
  chart.crosshair.style.top = g.m.t + "px";
  chart.crosshair.style.height = g.plotH + "px";

  // one tooltip, every series: value leads, name follows
  const tt = chart.tooltip;
  tt.textContent = "";
  tt.append(el("div", "tt-time", fmtClock(best.t)));
  for (const s of chart.def.series) {
    const row = el("div", "tt-row");
    const key = el("i");
    key.style.background = `var(${s.cssVar})`;
    row.append(key, el("span", "tt-val", chart.def.fmt(best[s.key])), el("span", "tt-name", s.name));
    tt.append(row);
  }
  tt.style.display = "block";
  const wrapW = chart.wrap.clientWidth;
  const ttW = tt.offsetWidth;
  let left = px + 12;
  if (left + ttW > wrapW - 4) left = px - ttW - 12;
  tt.style.left = Math.max(4, left) + "px";
  tt.style.top = g.m.t + 4 + "px";
}

function hideTooltip(chart) {
  chart.crosshair.style.display = "none";
  chart.tooltip.style.display = "none";
}

/* the WCAG-clean twin: same values as the chart, no color required */
function renderChartTable(chart) {
  const { def, tableWrap } = chart;
  tableWrap.textContent = "";
  const pts = windowedHistory().slice(-30).reverse();
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  hr.append(el("th", null, "Time"));
  for (const s of def.series) {
    const th = el("th", "num", s.name);
    hr.append(th);
  }
  thead.append(hr);
  const tbody = el("tbody");
  for (const p of pts) {
    const tr = el("tr");
    tr.append(el("td", null, fmtClock(p.t)));
    for (const s of def.series) tr.append(el("td", "num", def.fmt(p[s.key])));
    tbody.append(tr);
  }
  table.append(thead, tbody);
  tableWrap.append(table);
}

/* ================= cores & disks (meters) ================= */

function meterRow(labelText, percent, valueText, severity) {
  const row = el("div", "meter-row");
  const label = el("span", "m-label", labelText);
  const meter = el("div", "meter" + (severity ? " " + severity : ""));
  const fill = el("i");
  fill.style.width = Math.min(100, Math.max(0, percent)) + "%";
  meter.append(fill);
  const val = el("span", "m-val", valueText);
  row.append(label, meter, val);
  return row;
}

function renderCores() {
  const s = state.latest;
  if (!s) return;
  const body = $("#cores-body");
  body.textContent = "";
  const grid = el("div", "cores-grid");
  s.cpu.percore.forEach((pct, i) => {
    grid.append(meterRow("Core " + i, pct, fmtPct(pct)));
  });
  body.append(grid);
  const sub = [];
  if (s.cpu.freq) sub.push((s.cpu.freq / 1000).toFixed(2) + " GHz");
  if (s.cpu.load) sub.push("load " + s.cpu.load.join(" / "));
  $("#cores-sub").textContent = sub.join(" · ");
}

function diskSeverity(pct) {
  if (pct >= 95) return "crit";
  if (pct >= 85) return "warn";
  return "";
}

function renderDisks() {
  const s = state.latest;
  if (!s) return;
  const body = $("#disks-body");
  body.textContent = "";
  if (!s.disks.length) {
    body.append(el("div", "card-sub", "No readable volumes."));
    return;
  }
  for (const d of s.disks) {
    const row = el("div", "disk-row");
    const top = el("div", "disk-top");
    top.append(el("span", "d-mount", d.mount));
    const sev = diskSeverity(d.percent);
    if (sev) top.append(el("span", "badge " + sev, sev === "crit" ? "almost full" : "high"));
    top.append(el("span", "d-text", `${fmtBytes(d.used)} of ${fmtBytes(d.total)} · ${fmtPct(d.percent)}`));
    row.append(top);
    const meter = el("div", "meter" + (sev ? " " + sev : ""));
    const fill = el("i");
    fill.style.width = Math.min(100, d.percent) + "%";
    meter.append(fill);
    row.append(meter);
    body.append(row);
  }
}

/* ================= processes ================= */

const procSort = { key: "cpu", dir: -1 };

function renderProcs() {
  const s = state.latest;
  if (!s) return;
  const body = $("#procs-body");
  body.textContent = "";
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  const cols = [
    { key: "name", label: "Process", num: false },
    { key: "pid", label: "PID", num: true },
    { key: "cpu", label: "CPU %", num: true },
    { key: "mem", label: "Mem %", num: true },
  ];
  for (const c of cols) {
    const th = el("th", c.num ? "num" : null);
    const btn = el("button", procSort.key === c.key ? "sorted" : null, c.label);
    btn.type = "button";
    btn.addEventListener("click", () => {
      if (procSort.key === c.key) procSort.dir *= -1;
      else { procSort.key = c.key; procSort.dir = c.key === "name" ? 1 : -1; }
      renderProcs();
    });
    th.append(btn);
    hr.append(th);
  }
  thead.append(hr);
  const tbody = el("tbody");
  const procs = s.procs.slice().sort((a, b) => {
    const va = a[procSort.key], vb = b[procSort.key];
    return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * procSort.dir;
  });
  for (const p of procs) {
    const tr = el("tr");
    tr.append(el("td", null, p.name)); // untrusted name → textContent via el()
    tr.append(el("td", "num", String(p.pid)));
    tr.append(el("td", "num", p.cpu.toFixed(1)));
    tr.append(el("td", "num", p.mem.toFixed(1)));
    tbody.append(tr);
  }
  table.append(thead, tbody);
  body.append(table);
}

/* ================= system card ================= */

function renderSystem() {
  const s = state.latest, info = state.info;
  if (!s || !info) return;
  const body = $("#sys-body");
  body.textContent = "";
  const kv = el("dl", "kv");
  const add = (k, v) => {
    kv.append(el("dt", null, k));
    kv.append(el("dd", null, v));
  };
  add("OS", info.os);
  add("Machine", info.machine);
  const phys = info.cpu_count_physical;
  add("CPU", `${info.cpu_count} threads` + (phys ? ` (${phys} cores)` : ""));
  add("Uptime", fmtDur(s.uptime));
  add("Booted", new Date(info.boot_time * 1000).toLocaleString());
  if (s.mem.swap_total > 0)
    add("Swap", `${fmtBytes(s.mem.swap_used)} of ${fmtBytes(s.mem.swap_total)}`);
  if (s.battery)
    add("Battery", `${s.battery.percent}% ${s.battery.plugged ? "(plugged in)" : ""}`);
  add("Dashboard", `http://${location.hostname}:${location.port || 80}`);
  body.append(kv);
  if (s.temps && s.temps.length) {
    const chips = el("div", "chips");
    for (const t of s.temps) chips.append(el("span", "chip", `${t.label} ${t.c}°C`));
    body.append(chips);
  }
}

/* ================= header & status ================= */

function renderHeader() {
  const s = state.latest, info = state.info;
  if (!info) return;
  $("#hostname").textContent = info.hostname;
  document.title = `${info.hostname} · dashtop`;
  $("#host-meta").textContent = info.os + (s ? ` · up ${fmtDur(s.uptime)}` : "");
}

function setStatus(mode, text) {
  const box = $("#status");
  box.className = "status " + mode;
  $("#status-text").textContent = text;
}

/* ================= render loop ================= */

function renderAll() {
  renderHeader();
  renderTiles();
  for (const c of charts) c.render();
  renderCores();
  renderDisks();
  renderProcs();
  renderSystem();
  if (state.latest) {
    $("#foot").textContent =
      `updated ${fmtClock(state.latest.t)} · sampling every ${state.interval}s · dashtop`;
  }
}

/* ================= data plumbing ================= */

async function fetchSummary() {
  const res = await fetch("/api/summary");
  if (!res.ok) throw new Error("summary " + res.status);
  const data = await res.json();
  state.info = data.info;
  state.interval = data.interval || 2;
  state.latest = data.latest;
  state.history = data.history || [];
}

function pruneHistory() {
  const keep = 15 * 60 + 30; // matches the server's default retention
  if (!state.history.length) return;
  const t1 = state.history[state.history.length - 1].t;
  while (state.history.length && state.history[0].t < t1 - keep) state.history.shift();
}

function openStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => {
    setStatus("live", "live");
    // backfill anything missed while disconnected
    fetchSummary().then(renderAll).catch(() => {});
  };
  es.onerror = () => setStatus("down", "reconnecting…");
  es.onmessage = (ev) => {
    let snap;
    try { snap = JSON.parse(ev.data); } catch { return; }
    state.latest = snap;
    state.lastMsgAt = Date.now();
    state.history.push({
      t: snap.t, cpu: snap.cpu.total, mem: snap.mem.percent,
      dn: snap.net.down_bps, up: snap.net.up_bps,
      rd: snap.io.read_bps, wr: snap.io.write_bps,
    });
    pruneHistory();
    setStatus("live", "live");
    renderAll();
  };
}

/* hold the frame at reduced opacity when data goes stale — no skeletons */
setInterval(() => {
  if (!state.lastMsgAt) return;
  const stale = Date.now() - state.lastMsgAt > Math.max(6, state.interval * 3) * 1000;
  document.body.classList.toggle("is-stale", stale);
  if (stale) setStatus("down", "stale — reconnecting…");
}, 3000);

/* ================= boot ================= */

$("#window-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-min]");
  if (!btn) return;
  state.windowMin = +btn.dataset.min;
  for (const b of $("#window-seg").querySelectorAll("button")) {
    const on = b === btn;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  }
  renderAll();
});

let resizeQueued = false;
new ResizeObserver(() => {
  if (resizeQueued) return;
  resizeQueued = true;
  requestAnimationFrame(() => {
    resizeQueued = false;
    for (const c of charts) c.render();
  });
}).observe($("#charts"));

async function boot() {
  try {
    await fetchSummary();
  } catch {
    setStatus("down", "server unreachable — retrying…");
    setTimeout(boot, 3000);
    return;
  }
  buildTiles(!!(state.latest && state.latest.battery));
  buildCharts();
  state.lastMsgAt = Date.now();
  renderAll();
  openStream();
}

boot();
