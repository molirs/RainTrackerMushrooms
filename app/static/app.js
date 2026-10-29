"use strict";

const WINDOW_DAYS = 31;  // = MAX_WINDOW_DAYS de app/main.py

// ---- Escala de color (CLAUDE.md: 0 mm gris -> azules -> morado para >100 mm) ----------------
const BINS = [
  { max: 0.05, color: "#b9bfc4", label: "0" },
  { max: 5, color: "#c6dbef", label: "0–5" },
  { max: 10, color: "#9ecae1", label: "5–10" },
  { max: 20, color: "#6baed6", label: "10–20" },
  { max: 35, color: "#4292c6", label: "20–35" },
  { max: 50, color: "#2171b5", label: "35–50" },
  { max: 75, color: "#08519c", label: "50–75" },
  { max: 100, color: "#08306b", label: "75–100" },
  { max: Infinity, color: "#7b1fa2", label: "> 100" },
];
const NO_DATA = "#9aa19d";
const colorFor = (mm) => (mm == null ? NO_DATA : BINS.find((b) => mm < b.max).color);

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" });
const fmtMm = (mm) => (mm == null ? "sin dato" : `${mm.toFixed(1)} mm`);
const GRID_HALF = 0.075; // media celda de la rejilla de Open-Meteo (paso 0.15°)

const state = {
  mode: "acc", range: "last", days: 7, from: null, to: null, dayIdx: 0,
  provinces: new Set(), sources: new Set(), altMin: null, altMax: null,
  threshold: 0, showGrid: true, playing: null,
};
const data = { dates: [], series: {}, partial: {}, filled: {}, stations: [], byId: {}, sources: [] };
const layers = {}; // station_id -> capa Leaflet

// ---- Mapa ---------------------------------------------------------------------------------
// zoomSnap fino: en pantallas estrechas el zoom entero que abarca las 9 provincias se queda muy lejos
const map = L.map("map", { preferCanvas: true, zoomControl: true, zoomSnap: 0.25 }).setView([41.9, -0.6], 8);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 17, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
map.createPane("grid").style.zIndex = 350;
const renderer = L.canvas({ padding: 0.3 });

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

async function fetchData() {
  const [sources, stations, daily] = await Promise.all([
    getJSON("/api/sources"), getJSON("/api/stations?estimated=true"),
    getJSON(`/api/rain/daily?days=${WINDOW_DAYS}&estimated=true`),
  ]);
  Object.assign(data, {
    dates: daily.dates, series: daily.series, partial: daily.partial, filled: daily.filled || {}, stations, sources,
    byId: {},
  });
  stations.forEach((s) => (data.byId[s.id] = s));
}

async function load() {
  const [meta, provinces] = await Promise.all([getJSON("/api/meta"), getJSON("/api/provinces")]);
  await fetchData();

  meta.provinces.forEach((p) => state.provinces.add(p));
  data.sources.filter((s) => !s.estimated).forEach((s) => state.sources.add(s.id));
  state.dayIdx = data.dates.length - 1;
  state.from = data.dates[Math.max(0, data.dates.length - 7)];
  state.to = data.dates[data.dates.length - 1];

  const provLayer = L.geoJSON(provinces, {
    style: { color: "#555", weight: 1.2, opacity: 0.7, fill: false, dashArray: "4 3" }, interactive: false,
  }).addTo(map);
  map.fitBounds(provLayer.getBounds(), { padding: [8, 8] });

  buildControls(meta);
  buildLayers();
  buildLegend();
  renderSourcesInfo();
  render();
  $("#loading").hidden = true;
  if (meta.mobile) setupApp(meta);
  const linked = new URLSearchParams(location.hash.slice(1)).get("estacion");
  if (linked && data.byId[linked]) {
    const st = data.byId[linked];
    map.setView([st.lat, st.lon], 10);
    openDetail(linked);
  }
}

// ---- Cálculo de valores --------------------------------------------------------------------
function windowIdx() {
  if (state.mode === "day") return [state.dayIdx, state.dayIdx];
  if (state.range === "last") return [data.dates.length - state.days, data.dates.length - 1];
  const a = data.dates.indexOf(state.from), b = data.dates.indexOf(state.to);
  return [Math.max(0, Math.min(a, b)), Math.max(a, b)];
}

function valueOf(id, [a, b]) {
  const s = data.series[id];
  if (!s) return { mm: null, partial: false };
  let sum = 0, any = false;
  for (let i = a; i <= b; i++) if (s[i] != null) { sum += s[i]; any = true; }
  const partial = (data.partial[id] || []).some((i) => i >= a && i <= b);
  return { mm: any ? Math.round(sum * 10) / 10 : null, partial };
}

function visible(st) {
  if (!state.provinces.has(st.province)) return false;
  // Una estación se muestra si la publica alguna de las fuentes activas (ella o sus duplicadas)
  if (st.estimated ? !state.showGrid : !(st.sources || [st.source]).some((x) => state.sources.has(x))) return false;
  if (state.altMin != null && (st.altitude == null || st.altitude < state.altMin)) return false;
  if (state.altMax != null && (st.altitude == null || st.altitude > state.altMax)) return false;
  return true;
}

// ---- Capas ---------------------------------------------------------------------------------
function buildLayers() {
  for (const [id, layer] of Object.entries(layers)) {  // estaciones que ya no están (al recargar)
    if (!data.byId[id]) { layer.remove(); delete layers[id]; }
  }
  for (const st of data.stations) {
    if (layers[st.id]) continue;
    let layer;
    if (st.estimated) {
      layer = L.rectangle(
        [[st.lat - GRID_HALF, st.lon - GRID_HALF], [st.lat + GRID_HALF, st.lon + GRID_HALF]],
        { pane: "grid", renderer, stroke: false, fillOpacity: 0.3 });
    } else {
      layer = L.circleMarker([st.lat, st.lon], { renderer, radius: 6, weight: 1.5, color: "#fff", fillOpacity: 0.95 });
      layer.on("click", () => openDetail(st.id));
    }
    layer.bindTooltip("", { direction: "top", sticky: st.estimated });
    layers[st.id] = layer;
  }
}

function render() {
  const win = windowIdx();
  const ranking = [];
  for (const st of data.stations) {
    const layer = layers[st.id];
    if (!visible(st)) { layer.remove(); continue; }
    const { mm, partial } = valueOf(st.id, win);
    const below = state.threshold > 0 && (mm == null || mm < state.threshold);
    const fill = colorFor(mm);
    if (st.estimated) {
      layer.setStyle({ fillColor: fill, fillOpacity: mm == null ? 0 : below ? 0.08 : 0.3 });
    } else {
      layer.setStyle({
        fillColor: mm == null ? "transparent" : fill,
        color: mm == null ? NO_DATA : "#fff",
        fillOpacity: below ? 0.25 : 0.95, opacity: below ? 0.5 : 1,
      });
      layer.setRadius(below ? 4 : state.threshold > 0 ? 8 : 6);
      if (mm != null) ranking.push({ st, mm, partial });
    }
    const alt = st.altitude != null ? ` · ${Math.round(st.altitude)} m` : "";
    layer.setTooltipContent(
      `<b>${esc(st.estimated ? "Estimación (modelo)" : st.name)}</b>${alt}<br>` +
      `${fmtMm(mm)}${partial ? " <i>(parcial)</i>" : ""}`);
    if (!map.hasLayer(layer)) layer.addTo(map);
  }
  // Las estaciones siempre encima de la rejilla y las que destacan encima del resto
  ranking.sort((a, b) => a.mm - b.mm).forEach(({ st }) => layers[st.id].bringToFront());
  renderRanking(ranking.reverse().slice(0, 12));
  renderPeriodLabel(win);
}

function renderRanking(rows) {
  $("#ranking").innerHTML = rows.map(({ st, mm, partial }) =>
    `<li data-id="${esc(st.id)}"><span class="mm">${mm.toFixed(1)}${partial ? "*" : ""}</span>${esc(st.name)}` +
    `<span class="sub">${esc(st.province)}${st.altitude != null ? ` · ${Math.round(st.altitude)} m` : ""}</span></li>`,
  ).join("") || "<li>Sin datos con estos filtros</li>";
}

function renderPeriodLabel([a, b]) {
  const title = state.mode === "day" ? fmtDate(data.dates[a])
    : a === b ? fmtDate(data.dates[a]) : `${fmtDate(data.dates[a])} → ${fmtDate(data.dates[b])}`;
  $("#legend .period").textContent = title;
  $("#legend summary").dataset.period = title;
  $("#day-out").textContent = fmtDate(data.dates[state.dayIdx]);
}

function buildLegend() {
  $("#legend").innerHTML =
    `<summary class="title">Lluvia (mm)</summary><div class="period"></div>` +
    BINS.slice().reverse().map((b) => `<div class="item"><span class="sw" style="background:${b.color}"></span>${b.label}</div>`).join("") +
    `<div class="item"><span class="sw" style="background:transparent;border-color:${NO_DATA}"></span>sin dato</div>` +
    `<div class="item"><span class="sw sq" style="background:#6baed6"></span>estimado (modelo)</div>` +
    `<div class="note">Círculos: medido en estación. Cuadros: estimación de Open-Meteo. * = día incompleto.</div>`;
}

// ---- Controles -----------------------------------------------------------------------------
function timeAgo(iso) {
  if (!iso) return "";
  const h = (Date.now() - new Date(iso)) / 36e5;
  return h < 1 ? "hace <1 h" : h < 48 ? `hace ${Math.round(h)} h` : `hace ${Math.round(h / 24)} d`;
}

function buildControls(meta) {
  // Modo
  document.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("active", x === b));
    state.mode = b.dataset.mode;
    $("#mode-acc").hidden = state.mode !== "acc";
    $("#mode-day").hidden = state.mode !== "day";
    if (state.mode !== "day") stopPlay();
    render();
  }));
  document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("[data-range]").forEach((x) => x.classList.toggle("active", x === b));
    state.range = b.dataset.range;
    $("#range-last").hidden = state.range !== "last";
    $("#range-custom").hidden = state.range !== "custom";
    render();
  }));
  const days = $("#days");
  days.addEventListener("input", () => { state.days = +days.value; $("#days-out").textContent = days.value; render(); });

  for (const [id, key] of [["#date-from", "from"], ["#date-to", "to"]]) {
    const el = $(id);
    el.value = state[key];
    el.addEventListener("change", () => { if (data.dates.includes(el.value)) { state[key] = el.value; render(); } });
  }

  const day = $("#day");
  updateDateControls();
  day.value = state.dayIdx;
  day.addEventListener("input", () => { state.dayIdx = +day.value; render(); });
  $("#play").addEventListener("click", () => (state.playing ? stopPlay() : startPlay()));

  // Provincias
  $("#provinces").innerHTML = meta.provinces.map((p) => `<button class="on" data-p="${esc(p)}">${esc(p)}</button>`).join("");
  $("#provinces").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const p = b.dataset.p;
    // Clic con todas activas = quedarse solo con esa; si no, alternar
    if (state.provinces.size === meta.provinces.length) { state.provinces = new Set([p]); }
    else if (state.provinces.has(p)) { state.provinces.delete(p); if (!state.provinces.size) meta.provinces.forEach((x) => state.provinces.add(x)); }
    else state.provinces.add(p);
    document.querySelectorAll("#provinces button").forEach((x) => x.classList.toggle("on", state.provinces.has(x.dataset.p)));
    render();
  });

  // Altitud y umbral
  for (const [id, key] of [["#alt-min", "altMin"], ["#alt-max", "altMax"]]) {
    $(id).addEventListener("input", (e) => { state[key] = e.target.value === "" ? null : +e.target.value; render(); });
  }
  $("#threshold").addEventListener("input", (e) => { state.threshold = +e.target.value; $("#thr-out").textContent = e.target.value; render(); });

  // Fuentes (la lista se pinta en renderSourcesInfo, que se repite al recargar)
  $("#sources").addEventListener("change", (e) => {
    const s = e.target.dataset.s; if (!s) return;
    e.target.checked ? state.sources.add(s) : state.sources.delete(s);
    render();
  });
  $("#show-grid").addEventListener("change", (e) => { state.showGrid = e.target.checked; render(); });

  $("#ranking").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]"); if (!li) return;
    const st = data.byId[li.dataset.id];
    map.flyTo([st.lat, st.lon], Math.max(map.getZoom(), 10), { duration: 0.6 });
    openDetail(st.id);
  });

  $("#detail-close").addEventListener("click", () => {
    $("#detail").hidden = true;
    history.replaceState(null, "", location.pathname);
  });
  $("#toggle-panel").addEventListener("click", () => {
    $("#panel").classList.toggle("collapsed");
    map.invalidateSize();
  });
}

function updateDateControls() {
  const first = data.dates[0], last = data.dates[data.dates.length - 1];
  $("#days").max = data.dates.length;
  for (const id of ["#date-from", "#date-to"]) { $(id).min = first; $(id).max = last; }
  $("#day").max = data.dates.length - 1;
}

let dataAttribution = "";
function renderSourcesInfo() {
  $("#sources").innerHTML = data.sources.filter((s) => !s.estimated).map((s) => {
    const st = s.status;
    const info = !st ? "sin clave" : st.last_error && !st.last_success ? "error" : timeAgo(st.last_success);
    const title = st?.last_error ? ` title="${esc(st.last_error)}"` : "";
    return `<label class="check"${title}><input type="checkbox" data-s="${esc(s.id)}" ` +
      `${!st ? "disabled" : state.sources.has(s.id) ? "checked" : ""}>${esc(s.label)}<small>${esc(info)}</small></label>`;
  }).join("");
  // En el mapa, versión corta; la titularidad completa va en el panel
  const active = data.sources.filter((s) => s.status?.last_success);
  map.attributionControl.removeAttribution(dataAttribution);
  dataAttribution = active.length ? "Datos: " + active.map((s) => esc(s.label)).join(", ") : "";
  map.attributionControl.addAttribution(dataAttribution);
  $("#credits").innerHTML = active.map((s) => `<li>${esc(s.attribution)}</li>`).join("");
}

async function reloadData() {
  const wasLast = state.dayIdx === data.dates.length - 1;
  await fetchData();
  if (wasLast) state.dayIdx = data.dates.length - 1;
  updateDateControls();
  buildLayers();
  renderSourcesInfo();
  render();
}

// ---- App Android: actualización en el móvil, claves de API y versión --------------------------
const sync = { started: null, seen: new Set(), timer: null, hideTimer: null };

function setupApp(meta) {
  document.body.classList.add("app");
  $("#app-version").textContent = `Versión ${meta.version || "?"}`;
  $("#check-update").addEventListener("click", () => window.RadarApp?.checkForUpdates());
  $("#refresh").addEventListener("click", async () => {
    await fetch("/api/refresh", { method: "POST" });
    pollSync();
  });
  $("#settings").addEventListener("submit", async (e) => {
    e.preventDefault();
    const params = new URLSearchParams();
    for (const input of e.target.querySelectorAll("input")) if (input.value.trim()) params.set(input.name, input.value.trim());
    if (![...params].length) return;
    await fetch(`/api/settings?${params}`, { method: "POST" });
    e.target.reset();
    renderSettings();
    pollSync();
  });
  renderSettings();
  pollSync();
}

async function renderSettings() {
  const st = await getJSON("/api/settings");
  document.querySelectorAll("#settings [data-key]").forEach((el) => {
    el.textContent = st[el.dataset.key] ? "✓ guardada" : "sin clave";
    el.classList.toggle("ok", !!st[el.dataset.key]);
  });
}

async function pollSync() {
  clearTimeout(sync.timer);
  let p;
  try { p = await getJSON("/api/refresh"); } catch { sync.timer = setTimeout(pollSync, 5000); return; }
  if (p.started !== sync.started) { sync.started = p.started; sync.seen.clear(); }
  // Cada fuente que termina se pinta ya, sin esperar a las lentas (SAIH Ebro tarda minutos)
  const finished = Object.entries(p.sources).filter(([, s]) => !["pending", "running"].includes(s.state)).map(([n]) => n);
  const fresh = finished.filter((n) => !sync.seen.has(n));
  fresh.forEach((n) => sync.seen.add(n));
  if (fresh.length) await reloadData().catch(console.error);
  showSync(p);
  $("#refresh").classList.toggle("spin", p.running);
  if (p.running) sync.timer = setTimeout(pollSync, 3000);
}

function showSync(p) {
  const el = $("#sync");
  clearTimeout(sync.hideTimer);
  if (!Object.keys(p.sources).length) { el.hidden = true; return; }
  const label = (id) => data.sources.find((s) => s.id === id)?.label || id;
  const icon = { pending: "·", running: "…", done: "✓", skipped: "–", error: "⚠" };
  const items = Object.entries(p.sources).filter(([, s]) => s.state !== "skipped")
    .map(([n, s]) => `<span class="${s.state}" title="${esc(s.summary || "")}">${icon[s.state]} ${esc(label(n))}</span>`);
  const empty = !data.stations.some((s) => !s.estimated);
  el.innerHTML = `<b>${p.running ? "Actualizando datos" : "Datos actualizados"}</b>` +
    (p.running && empty ? `<div class="hint">Primera descarga: puede tardar varios minutos.</div>` : "") +
    `<div class="items">${items.join("")}</div>`;
  el.hidden = false;
  if (!p.running) sync.hideTimer = setTimeout(() => (el.hidden = true), 4000);
}

// La app Android avisa al volver a primer plano (puede haber empezado una actualización)
window.radarResume = () => { if (document.body.classList.contains("app")) pollSync(); };

// Botón "atrás" de Android: primero cierra la ficha o el panel, después sale de la app
window.radarBack = () => {
  if (!$("#detail").hidden) { $("#detail-close").click(); return true; }
  if (matchMedia("(max-width: 760px)").matches && !$("#panel").classList.contains("collapsed")) {
    $("#toggle-panel").click(); return true;
  }
  return false;
};

function startPlay() {
  $("#play").textContent = "❚❚"; $("#play").setAttribute("aria-label", "Pausa");
  state.playing = setInterval(() => {
    state.dayIdx = (state.dayIdx + 1) % data.dates.length;
    $("#day").value = state.dayIdx;
    render();
  }, 800);
}
function stopPlay() {
  clearInterval(state.playing); state.playing = null;
  $("#play").textContent = "▶"; $("#play").setAttribute("aria-label", "Reproducir");
}

// ---- Ficha de estación -----------------------------------------------------------------------
const charts = {};
let stripePattern;
function stripes() {  // relleno rayado para los días completados con otra red
  if (stripePattern) return stripePattern;
  const c = document.createElement("canvas");
  c.width = c.height = 6;
  const g = c.getContext("2d");
  g.fillStyle = "#c6dbef"; g.fillRect(0, 0, 6, 6);
  g.strokeStyle = "#2171b5"; g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(0, 6); g.lineTo(6, 0); g.moveTo(-1, 1); g.lineTo(1, -1); g.moveTo(5, 7); g.lineTo(7, 5); g.stroke();
  return (stripePattern = g.createPattern(c, "repeat"));
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

async function openDetail(id) {
  const st = data.byId[id];
  const src = data.sources.find((s) => s.id === st.source);
  const d = await getJSON(`/api/stations/${encodeURIComponent(id)}/series?days=${WINDOW_DAYS}`);
  const total = d.days[d.days.length - 1].cum_mm;
  const label = (id) => data.sources.find((s) => s.id === id)?.label || id;
  const also = d.also_in.length
    ? `<div class="meta">También publicada por: ${[...new Set(d.also_in.map((x) => label(x.source)))].map(esc).join(", ")}</div>` : "";
  const nFilled = d.days.filter((x) => x.filled_from).length;
  const filledNote = nFilled
    ? `<div class="meta">${nFilled} día(s) sin dato propio completados con otra red (barras rayadas)</div>` : "";
  $("#detail-head").innerHTML =
    `<h3>${esc(d.name)}</h3>` +
    `<div class="meta">${esc(src?.label || d.source)} · ${esc(d.province)}` +
    `${d.altitude != null ? ` · ${Math.round(d.altitude)} m` : ""}</div>` +
    `${d.day_window ? `<div class="meta">Día pluviométrico: ${esc(d.day_window)}</div>` : ""}${also}${filledNote}` +
    `<div class="big">${total.toFixed(1)} mm</div><div class="meta">acumulado en los últimos ${d.days.length} días</div>`;
  $("#detail").hidden = false;
  history.replaceState(null, "", `#estacion=${encodeURIComponent(id)}`);

  const labels = d.days.map((x) => fmtDate(x.date));
  const ink = cssVar("--ink-2"), grid = cssVar("--line");
  const scales = {
    x: { ticks: { color: ink, maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 10 } }, grid: { display: false } },
    y: { beginAtZero: true, ticks: { color: ink, font: { size: 10 }, maxTicksLimit: 4 }, grid: { color: grid }, border: { display: false } },
  };
  const common = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false } }, scales,
  };
  charts.daily?.destroy();
  charts.cum?.destroy();
  charts.daily = new Chart($("#chart-daily"), {
    type: "bar",
    data: { labels, datasets: [{
      data: d.days.map((x) => x.mm),
      backgroundColor: d.days.map((x) => (x.filled_from ? stripes() : x.partial ? "#9ecae1" : "#4292c6")),
      borderRadius: 3, maxBarThickness: 14,
    }] },
    options: { ...common, plugins: { ...common.plugins, tooltip: { callbacks: {
      label: (c) => {
        const day = d.days[c.dataIndex];
        return `${fmtMm(c.raw)}${day.partial ? " (parcial)" : ""}` +
          `${day.filled_from ? ` · dato de ${label(day.filled_from)}` : ""}`;
      },
    } } } },
  });
  charts.cum = new Chart($("#chart-cum"), {
    type: "line",
    data: { labels, datasets: [{
      data: d.days.map((x) => x.cum_mm), borderColor: "#2171b5", borderWidth: 2,
      backgroundColor: "rgba(66,146,198,.15)", fill: true, pointRadius: 0, pointHoverRadius: 4, tension: 0.2,
    }] },
    options: { ...common, plugins: { ...common.plugins, tooltip: { callbacks: { label: (c) => `${c.raw.toFixed(1)} mm` } } } },
  });
}

if (matchMedia("(max-width: 760px)").matches) {
  $("#panel").classList.add("collapsed");
  $("#legend").open = false;
  map.invalidateSize();
}

load().catch((e) => { $("#loading").textContent = `Error cargando datos: ${e.message}`; console.error(e); });
