"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v, d = 0) => (v === null || v === undefined ? "—" : Number(v).toFixed(d).replace(".", ","));
const clock = (t) => new Date(t * 1000).toLocaleTimeString("pt-BR");
const short = (id) => id.split("/").pop().replace(/\.csv$/, "");
const setHtml = (el, html) => { if (el._html !== html) { el._html = html; el.innerHTML = html; } };

const TILE_ZOOM = 18, TILE_SIZE = 256;
let last = null, routes = [], routeData = null, trail = [], view = "graph", vb = null, tilesKey = "";
try { view = localStorage.getItem("painel.view") === "gps" ? "gps" : "graph"; } catch (e) { /* sem armazenamento */ }

// ---------------------------------------------------------------- projeção
// O SVG usa float de 32 bits: em coordenadas absolutas (~1e8) o traçado fica serrilhado.
let base = null;
function project(lat, lon) {
  const n = TILE_SIZE * 2 ** TILE_ZOOM, r = lat * Math.PI / 180;
  const x = (lon + 180) / 360 * n, y = (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n;
  if (!base) base = [Math.floor(x / TILE_SIZE) * TILE_SIZE, Math.floor(y / TILE_SIZE) * TILE_SIZE];
  return [x - base[0], y - base[1]];
}
const metersPerPx = (lat) => 156543.03392 * Math.cos(lat * Math.PI / 180) / 2 ** TILE_ZOOM;

// Enquadramento: "route" (rota, e o veículo se estiver perto), "all" (rota, veículo e
// rastro), "car" (segue o veículo) ou "free" (depois de arrastar ou dar zoom).
const MIN_W = 25, MAX_W = 6000, MAX_TILES = 120;
let cam = { mode: "route", cx: 0, cy: 0, w: 120 };

function bounds(points) {
  return { x0: Math.min(...points.map((p) => p[0])), x1: Math.max(...points.map((p) => p[0])),
           y0: Math.min(...points.map((p) => p[1])), y1: Math.max(...points.map((p) => p[1])) };
}
function fitPoints(points, ratio) {
  const b = bounds(points);
  const pad = Math.max(b.x1 - b.x0, b.y1 - b.y0) * 0.06 + 4;
  let w = b.x1 - b.x0 + 2 * pad, h = b.y1 - b.y0 + 2 * pad;
  const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
  if (w / h < ratio) w = h * ratio; else h = w / ratio;
  return { x: cx - w / 2, y: cy - h / 2, w, h };
}
function fitView() {
  const wrap = $("mapWrap").getBoundingClientRect(), ratio = wrap.width / Math.max(wrap.height, 1);
  const carPos = last?.position?.car, car = carPos ? project(carPos.lat, carPos.lon) : null;
  const around = (cx, cy, w) => { const width = Math.min(MAX_W, Math.max(MIN_W, w)), h = width / ratio; return { x: cx - width / 2, y: cy - h / 2, w: width, h }; };
  if (cam.mode === "free") return around(cam.cx, cam.cy, cam.w);
  if (cam.mode === "car" && car) return around(car[0], car[1], cam.w);
  const pts = (routeData?.points || []).map((p) => project(p[0], p[1]));
  if (cam.mode === "all") {
    if (car) pts.push(car);
    trail.forEach((p) => pts.push(project(p[0], p[1])));
  } else if (car) {
    if (!pts.length) return around(car[0], car[1], 120);
    if (Math.hypot(car[0] - pts[0][0], car[1] - pts[0][1]) < 400) pts.push(car);   // longe, não encolhe a pista
  }
  if (!pts.length) return car ? around(car[0], car[1], 120) : null;
  const fit = fitPoints(pts, ratio);
  if (fit.w > MAX_W) return around(fit.x + fit.w / 2, fit.y + fit.h / 2, MAX_W);
  return fit;
}
function setCam(mode) {
  if (vb && mode === "car" && cam.mode !== "car") cam.w = Math.min(vb.w, 160);
  cam.mode = mode;
  [["camRoute", "route"], ["camCar", "car"], ["camAll", "all"]].forEach(([id, m]) => $(id).classList.toggle("on", m === mode));
  if (last || routeData) drawMap();
}
// Zoom mantendo fixo o ponto sob o cursor; seguindo o veículo, só muda a distância.
function zoomBy(factor, clientX, clientY) {
  if (!vb) return;
  const box = $("mapWrap").getBoundingClientRect();
  const width = Math.min(MAX_W, Math.max(MIN_W, vb.w * factor)), scale = width / vb.w;
  if (cam.mode === "car") { cam.w = width; drawMap(); return; }
  const fx = clientX === undefined ? 0.5 : (clientX - box.left) / box.width;
  const fy = clientY === undefined ? 0.5 : (clientY - box.top) / box.height;
  const px = vb.x + fx * vb.w, py = vb.y + fy * vb.h;
  cam.cx = px + (0.5 - fx) * vb.w * scale;
  cam.cy = py + (0.5 - fy) * vb.h * scale;
  cam.w = width;
  setCam("free");
}

function drawTiles() {
  const g = $("tiles");
  if (view !== "gps" || !vb) { g.innerHTML = ""; tilesKey = ""; return; }
  const X = vb.x + base[0], Y = vb.y + base[1];
  const i0 = Math.floor(X / TILE_SIZE), i1 = Math.floor((X + vb.w) / TILE_SIZE);
  const j0 = Math.floor(Y / TILE_SIZE), j1 = Math.floor((Y + vb.h) / TILE_SIZE);
  const key = [i0, i1, j0, j1].join();
  if (key === tilesKey) return;
  tilesKey = key; g.innerHTML = ""; $("mapWrap")._missing = 0; $("mapWrap")._tooFar = 0;
  // Muito afastado seriam imagens demais: fica só o traçado até aproximar.
  if ((i1 - i0 + 1) * (j1 - j0 + 1) > MAX_TILES) { $("mapWrap")._tooFar = 1; return; }
  for (let i = i0; i <= i1; i++) for (let j = j0; j <= j1; j++) {
    const im = document.createElementNS("http://www.w3.org/2000/svg", "image");
    im.setAttribute("href", `/tiles/${TILE_ZOOM}/${j}/${i}`);
    im.setAttribute("x", i * TILE_SIZE - base[0]); im.setAttribute("y", j * TILE_SIZE - base[1]);
    im.setAttribute("width", TILE_SIZE + .5); im.setAttribute("height", TILE_SIZE + .5);
    im.addEventListener("error", () => { im.remove(); $("mapWrap")._missing = 1; });
    g.appendChild(im);
  }
}

const polyPts = (list) => list.map((p) => { const q = project(p[0], p[1]); return `${q[0].toFixed(2)},${q[1].toFixed(2)}`; }).join(" ");
function drawRoute() {
  const rp = routeData?.points || [];
  $("rt").setAttribute("points", polyPts(routeData?.closed ? rp.concat([rp[0]]) : rp));
}
function drawTrail() { $("tr").setAttribute("points", polyPts(trail)); }

function drawMap() {
  const box = $("mapWrap").getBoundingClientRect();
  if (!box.width) return;
  const next = fitView();
  const map = $("map");
  $("mapCard").classList.toggle("gps", view === "gps");
  if (!next) { vb = null; $("mapMsg").textContent = "Aguardando rota ou posição do GPS"; map.style.visibility = "hidden"; return; }
  map.style.visibility = "visible";
  vb = next; map.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  drawTiles();
  const k = vb.w / box.width, font = 13 * k, pos = last?.position || {}, rp = routeData?.points;
  const refLat = rp?.[0]?.[0] ?? pos.car?.lat ?? 0, mpp = metersPerPx(refLat);
  const zone = $("zone"), startPt = $("start"), startLabel = $("startLabel");
  if (rp) {
    const s = project(rp[0][0], rp[0][1]), max = last?.limits?.max_distance ?? 3;
    zone.setAttribute("cx", s[0]); zone.setAttribute("cy", s[1]); zone.setAttribute("r", max / mpp);
    startPt.setAttribute("cx", s[0]); startPt.setAttribute("cy", s[1]); startPt.setAttribute("r", 5 * k);
    startLabel.setAttribute("x", s[0] + max / mpp + 5 * k); startLabel.setAttribute("y", s[1] + 4 * k); startLabel.setAttribute("font-size", font);
  }
  [zone, startPt, startLabel].forEach((el) => { el.style.display = rp ? "" : "none"; });
  const car = pos.car, carEl = $("car"), line = $("toStart"), label = $("toLabel");
  if (car) {
    const c = project(car.lat, car.lon), size = Math.max(1.5 / mpp, 8 * k);
    const a = car.yaw ?? 0, cs = Math.cos(a), sn = Math.sin(a);
    const pt = (f, l) => `${c[0] + f * size * cs - l * size * sn},${c[1] - f * size * sn - l * size * cs}`;
    carEl.setAttribute("points", car.yaw === null ? `${c[0]},${c[1] - size} ${c[0] + size},${c[1]} ${c[0]},${c[1] + size} ${c[0] - size},${c[1]}` : `${pt(1.3, 0)} ${pt(-.9, .75)} ${pt(-.9, -.75)}`);
    carEl.classList.toggle("stale", (pos.car_age ?? 99) > 3);
    carEl.style.display = "";
    const showLine = rp && !last?.mode?.automatic && pos.distance !== null && pos.distance > (last?.limits?.max_distance ?? 3);
    line.style.display = label.style.display = showLine ? "" : "none";
    if (showLine) {
      const s = project(rp[0][0], rp[0][1]);
      line.setAttribute("x1", c[0]); line.setAttribute("y1", c[1]); line.setAttribute("x2", s[0]); line.setAttribute("y2", s[1]);
      label.textContent = `${num(pos.distance, 1)} m até o início`;
      label.setAttribute("x", c[0] + 14 * k); label.setAttribute("y", c[1] + 5 * k); label.setAttribute("font-size", 14 * k);
    }
  } else { carEl.style.display = line.style.display = label.style.display = "none"; }
  const sc = $("scale"), sl = sc.querySelector("line"), st = sc.querySelector("text");
  const meters = [5, 10, 20, 50, 100, 200, 500, 1000, 2000].find((m) => m / mpp / k >= 55) ?? 5000, len = meters / mpp;
  st.textContent = meters >= 1000 ? `${meters / 1000} km` : `${meters} m`;
  const sx = vb.x + 14 * k, sy = vb.y + vb.h - 12 * k;
  sl.setAttribute("x1", sx); sl.setAttribute("x2", sx + len); sl.setAttribute("y1", sy); sl.setAttribute("y2", sy);
  st.setAttribute("x", sx + len / 2); st.setAttribute("y", sy - 5 * k); st.setAttribute("font-size", font);
  let msg = "";
  if (!rp) msg = "Selecione uma rota na lista";
  else if (!car) msg = "Sem posição do GPS";
  else if (cam.mode === "route" && pos.distance !== null && pos.distance > 200) msg = `Veículo a ${num(pos.distance)} m do início, fora desta vista: use Tudo ou Veículo`;
  if (view === "gps" && $("mapWrap")._tooFar) msg = (msg ? msg + ". " : "") + "Aproxime para ver a imagem";
  else if (view === "gps" && $("mapWrap")._missing) msg = (msg ? msg + ". " : "") + "Sem imagem salva desta área";
  $("mapMsg").textContent = msg;
  $("foot").textContent = view === "gps" ? "Imagem do mapa: Esri World Imagery (pode estar deslocada de 1 a 3 m)" : "Painel do VIDA · LMT/UFLA";
}

// ---------------------------------------------------------------- blocos
function tile(id, cls, big, small) {
  const el = $(id);
  el.className = "t " + cls;
  el.querySelector("b").textContent = big;
  el.querySelector("small").textContent = small;
}
function renderTiles(d) {
  const t = d.tiles, s = t.safety;
  if (!s.on) tile("tSafety", "old", "SEM DADOS", s.age === null ? "ESP32 sem resposta" : `há ${num(s.age)} s`);
  else if (s.permitted) tile("tSafety", "ok", "LIBERADO", "enable físico e lógico · 0 flags");
  else tile("tSafety", "bad", "BLOQUEADO", s.flags.map((f) => f.name).join(" · ") || "sem permissão");
  tile("tConn", t.link.cls, t.link.big, t.link.small);
  const r = t.rtk;
  if (!r.on) tile("tRtk", "old", "SEM DADOS", r.age === null ? "GPS sem resposta" : `há ${num(r.age)} s`);
  else tile("tRtk", r.status === "FIXED" ? "ok" : r.status === "FLOAT" ? "warn" : "bad", r.status, `${num((r.accuracy ?? 0) * 100, 1)} cm · ${r.satellites} satélites`);
  const c = t.corrections, n = c.ntrip;
  const ntxt = n ? (n.connected ? "NTRIP conectado" : `NTRIP ${n.state}`) : "NTRIP sem dados";
  if (!c.on) tile("tCorr", "old", "SEM DADOS", ntxt);
  else tile("tCorr", c.age <= 5 ? "ok" : c.age <= c.limit ? "warn" : "bad", `${num(c.age)} s`, ntxt);
  if (!t.ekf.on) tile("tEkf", "old", "SEM DADOS", "Odroid sem resposta");
  else tile("tEkf", t.ekf.running ? "ok" : "warn", t.ekf.running ? "RODANDO" : "AGUARDANDO", t.ekf.running ? "posição estimada ok" : t.ekf.text);
  const b = d.battery, box = $("tBat");
  box.className = "t " + (b.on ? (b.percent === null ? "old" : b.level) : "old");
  $("batPct").textContent = b.percent === null ? "—" : `${num(b.percent)}%`;
  $("batV").textContent = b.voltage === null ? "—" : `${num(b.voltage, 2)} V`;
  $("batFill").setAttribute("width", ((b.percent ?? 0) / 100 * 33).toFixed(1));
  $("batSub").textContent = !b.on ? "sem leitura" : b.percent === null ? "aguardando 60 s parado" : b.resting ? "carga medida com o carro parado" : "última carga medida parado";
}

const row = (a, b) => `<div class="r"><span>${esc(a)}</span><b>${b}</b></div>`;
function renderCards(d) {
  const ck = d.checks, ok = ck.items.filter((c) => c.ok).length, total = ck.items.length;
  $("chkTitle").textContent = ck.no_route ? "Sem rota: modo manual" : ck.automatic ? ck.title : `Para executar a ${short(d.selected)}`;
  $("chkCount").textContent = ck.no_route ? "" : ck.ready ? `${ok} de ${total}` : `faltam ${total - ok}`;
  const big = $("chkBig");
  big.hidden = (ck.automatic && ck.ready) || ck.no_route;
  big.textContent = ck.ready ? "PRONTO" : "NÃO PRONTO";
  big.classList.toggle("okb", ck.ready);
  // "Navegação no ar" só aparece quando falta: é o mesmo tamanho da tela aprovada.
  setHtml($("checks"), ck.items.filter((c) => !(c.ok && c.label.startsWith("Navegação"))).map((c) => `<div class="c${c.ok ? "" : " no"}"><i>${c.ok ? "✓" : "✕"}</i><span>${esc(c.label)}</span><b>${esc(c.value)}</b></div>`).join(""));
  const tip = $("tip");
  tip.hidden = !ck.next;
  setHtml(tip, ck.next ? `<b>Próximo passo:</b> ${esc(ck.next)}` : "");
  const s = d.tiles.safety, off = (v) => (s.on ? (v ? "Ligado" : '<span class="red">Desligado</span>') : "—");
  setHtml($("safety"), row("Enable físico", off(s.physical)) + row("Enable lógico", off(s.logical)) +
    row("Referências", s.on ? (s.refs ? "Recentes" : '<span class="red">Atrasadas</span>') : "—") +
    row("Flags ativas", s.on ? (s.flags.length ? `<span class="red">${s.flags.map((f) => f.bit).join(", ")}</span>` : "Nenhuma") : "—"));
  const v = d.vehicle;
  let brake = "—";
  if (v.maneuver) brake = "Solto (manobra)";
  else if (v.brake === 0) brake = "Sem pulsos";
  else if (v.brake !== null) brake = v.brake >= 2400 ? "Solto" : v.brake < 1550 ? "Total" : v.brake <= 1650 ? "Segurando" : "Parcial";
  setHtml($("vehicle"), row("Velocidade ped./med.", `${num(v.speed_ref, 2)} / ${num(v.speed, 2)} m/s`) +
    row("Ângulo ped./med.", `${num(v.angle_ref, 1)} / ${num(v.angle, 1)} °`) +
    row("PWM tração / esterç.", `${num(v.pwm_drive)} / ${num(v.pwm_steering)}`) + row("Freio", brake));
  const b = d.battery;
  setHtml($("battery"), row("Carga (parado)", b.percent === null ? "<em>aguardando 60 s parado</em>" : `${num(b.percent)}% · <em>36 V = 0%, 38,7 V = 100%</em>`) +
    row("Tensão agora", `${num(b.voltage, 2)} V`) +
    row("Corrente", b.current === null ? "— A <em>(ainda sem medição)</em>" : `${num(b.current, 1)} A`) +
    row("Menor tensão na sessão", `${num(b.minimum, 2)} V`));
  setHtml($("health"), d.health.map((h) => `<div class="h${h.hz > 0 ? "" : " off"}"><i></i>${esc(h.name)}<b>${h.hz > 0 ? `${num(h.hz, h.hz < 10 ? 1 : 0)} Hz` : "—"}</b></div>`).join(""));

  const active = s.flags.map((f) => `<div class="e"><div class="o">Segurança · agora</div><b>Flag ${f.bit}: ${esc(f.name)}</b><div class="d">${esc(f.hint)}</div></div>`);
  const activeTitles = new Set(s.flags.map((f) => `Flag ${f.bit}: ${f.name}`));
  const past = d.errors.filter((e) => !activeTitles.has(e.title)).map((e) => `<div class="e g"><div class="o">${esc(e.source)} · ${clock(e.t)}</div><b>${esc(e.title)}</b>${e.detail ? `<div class="d">${esc(e.detail)}</div>` : ""}</div>`);
  $("btnClearErrors").hidden = !past.length;
  setHtml($("errors"), active.concat(past).join("") || '<div class="none">Nenhum erro nesta sessão</div>');
  setHtml($("events"), d.events.map((e) => `<div><time>${clock(e.t)}</time><span>${esc(e.text)}</span></div>`).join(""));

  const box = $("modeBox");
  box.className = "md " + ({ blue: "", ok: "ok", warn: "warn", old: "old" }[d.mode.color] ?? "");
  $("modeLabel").textContent = d.mode.label;
  $("modeDetail").textContent = d.mode.detail || "Modo atual";
  const btn = $("btnCar"), running = d.car.running;
  btn.textContent = d.busy ? "Aguarde" : running ? "■ Parar" : "▶ Iniciar";
  btn.className = "stop" + (running ? "" : " go");
  btn.disabled = d.busy || !!d.recording || !!d.bags?.playing;
  $("carText").textContent = d.busy ? "Trocando o estado do carro" : running ? `Carro em execução${d.car.navigation ? "" : " · sem rota"}` : "Carro parado";

  const pos = d.position, auto = d.mode.automatic && pos.cross_track !== null;
  $("k1l").textContent = auto ? "Erro lateral" : "Até o início da rota";
  $("k1").innerHTML = auto ? `${pos.cross_track >= 0 ? "+" : ""}${num(pos.cross_track, 2)} <small>m</small>` : `${num(pos.distance, 1)} <small>m</small>`;
  $("k2l").textContent = auto ? "Erro de rumo" : "Rumo × rota";
  $("k2").innerHTML = `${num(auto ? pos.stanley_heading : pos.heading_error, auto ? 1 : 0)} <small>°</small>`;
  $("k3").innerHTML = `${num(v.speed, 2)} <small>m/s</small>`;
  $("k4").innerHTML = `${num(v.angle, 1)} <small>°${pos.saturated ? " · limite" : ""}</small>`;
  const prog = $("progress");
  prog.hidden = !auto;
  if (auto) {
    const total = (pos.progress ?? 0) + (pos.remaining ?? 0), pct = total > 0 ? pos.progress / total * 100 : 0;
    $("progBar").style.width = `${pct}%`;
    $("progText").textContent = `${num(pos.progress)} de ${num(total)} m · ${num(pct)}%`;
  }
  const rec = d.recording, recBox = $("recBox");
  recBox.hidden = !rec;
  $("btnRecord").hidden = !!rec || $("tabBags").classList.contains("on");
  if (rec) {
    const info = recBox.querySelector("span") || (recBox.innerHTML = '<span></span><button id="btnRecStop">■ Parar gravação</button>', $("btnRecStop").onclick = stopRecording, recBox.querySelector("span"));
    info.textContent = `● Gravando ${rec.name} · ${num(rec.length_m)} m · ${Math.floor(rec.seconds / 60)}:${String(rec.seconds % 60).padStart(2, "0")}`;
  }
  renderBagBars(d.bags);
  $("topRoute").textContent = d.selected ? `Rota: ${short(d.selected)}` : "Sem rota";
  if (d.selected !== last?.selected_seen) { loadRoutes(); }
  d.selected_seen = d.selected;
}

function renderRoutes(selected) {
  const none = `<div class="rl rlp${selected ? "" : " cur"}" data-id=""><b>Nenhuma</b><em>só modo manual</em>${selected ? "" : '<span class="tag">selecionada</span>'}</div>`;
  setHtml($("routes"), none + routes.map((r) => {
    const cur = r.id === selected;
    const tag = cur ? '<span class="tag">selecionada</span>' : r.warning ? `<span class="warnt" title="${esc(r.warning)}">!</span>` : "";
    return `<div class="rl rlp${cur ? " cur" : ""}" data-id="${esc(r.id)}"><b>${esc(r.name)}</b><em>${r.folder.slice(8, 10)}/${r.folder.slice(5, 7)} · ${r.length_m} m${r.warning ? " · " + esc(r.warning) : ""}</em><span class="act">${tag}<button class="ic" data-act="rename" title="Renomear">✎</button><button class="ic del" data-act="delete" title="Excluir">✕</button></span></div>`;
  }).join(""));
}

let bagList = { bags: [] };
async function loadBags() {
  bagList = await (await fetch("/api/bags")).json();
  const free = bagList.free_gb === null ? "" : `<div class="none">${num(bagList.free_gb, 1)} GB livres</div>`;
  setHtml($("bags"), bagList.bags.map((b) => `<div class="rl" data-bag="${esc(b.name)}"><b>${esc(b.name)}</b><em>${b.seconds === null ? "?" : Math.floor(b.seconds / 60) + ":" + String(Math.round(b.seconds % 60)).padStart(2, "0")} · ${b.size_mb} MB</em><span class="act"><button class="ic pl" data-act="play" title="Reproduzir">▶</button></span></div>`).join("") + free || '<div class="none">Nenhum bag salvo</div>');
}
function setTab(name) {
  const bags = name === "bags";
  $("tabRoutes").classList.toggle("on", !bags); $("tabBags").classList.toggle("on", bags);
  $("routes").hidden = bags; $("bags").hidden = !bags;
  $("btnRecord").hidden = bags || !!last?.recording; $("btnBagRecord").hidden = !bags || !!last?.bags?.recording;
  if (bags) loadBags();
}
function renderBagBars(b) {
  const rec = $("bagRecBox"), play = $("bagPlayBox");
  rec.hidden = !b.recording; play.hidden = !b.playing;
  if (b.recording) {
    const t = b.recording.seconds;
    setHtml(rec, `<span>● Gravando bag ${esc(b.recording.name)} · ${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}</span><button data-bagact="recstop">■ Parar</button>`);
  } else rec._html = "";
  if (b.playing) {
    setHtml(play, `<span>▶ ${esc(b.playing.name)}${b.playing.paused ? " · pausado" : ""}</span><span><button data-bagact="pause">${b.playing.paused ? "▶ Continuar" : "❚❚ Pausar"}</button><button data-bagact="stop">■ Parar</button></span>`);
  } else play._html = "";
  if (!$("tabBags").classList.contains("on")) return;
  $("btnBagRecord").hidden = !!b.recording;
}

// ---------------------------------------------------------------- dados
async function loadRoutes() {
  const res = await fetch("/api/routes");
  const data = await res.json();
  routes = data.routes;
  renderRoutes(data.selected);
  if (data.selected && routeData?.id !== data.selected) {
    routeData = await (await fetch(`/api/route?id=${encodeURIComponent(data.selected)}`)).json();
    vb = null; tilesKey = ""; drawRoute(); if (cam.mode !== "route") setCam("route");
  } else if (!data.selected) { routeData = null; drawRoute(); }
  return data;
}

function connect() {
  const socket = new WebSocket(`ws://${location.host}/ws`);
  socket.onmessage = (event) => {
    const d = JSON.parse(event.data);
    d.limits = { max_distance: parseFloat((d.checks.items.find((c) => c.label.startsWith("Veículo até"))?.label.match(/\d+/) || [3])[0]) };
    if (d.trail.reset) trail = [];
    if (d.trail.points.length) { trail = trail.concat(d.trail.points); drawTrail(); }
    d.selected_seen = last?.selected_seen;
    last = d;
    renderTiles(d); renderCards(d); drawMap();
    const ok = d.tiles.safety.on || d.tiles.rtk.on;
    $("liveDot").className = "dot " + (d.replay ? "replay" : ok ? "on" : "off");
    $("liveText").textContent = d.replay ? "REPRODUZINDO BAG · dados não são do carro" : ok ? "Ao vivo" : "Sem dados do carro";
    $("liveBox").classList.toggle("replaying", d.replay);
  };
  socket.onclose = () => {
    $("liveDot").className = "dot off";
    $("liveText").textContent = "Sem conexão com o painel";
    setTimeout(connect, 1000);
  };
}

// ---------------------------------------------------------------- ações
function dialog(title, body, buttons) {
  $("mTitle").textContent = title;
  $("mBody").innerHTML = body;
  const box = $("mBtns");
  box.innerHTML = "";
  buttons.forEach(([label, cls, action]) => {
    const b = document.createElement("button");
    b.textContent = label; b.className = cls || "";
    b.onclick = async () => { $("modal").hidden = true; if (action) await action(); };
    box.appendChild(b);
  });
  $("modal").hidden = false;
}
async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!res.ok) { dialog("Não foi possível", esc(await res.text()), [["Fechar", "p"]]); return null; }
  return res.json();
}
$("btnCar").onclick = () => {
  if (!last) return;
  if (!last.car.running) {
    const route = last.selected;
    const text = route ? `Sobe a navegação com a rota <b>${esc(short(route))}</b>. O gamepad assume o carro no manual.` : "Nenhuma rota selecionada: sobe só o gamepad, no manual.";
    dialog("Iniciar o carro?", text, [["Cancelar"], ["Iniciar", "p", () => post("/api/start")]]);
  } else {
    const moving = last.mode.automatic || (last.vehicle.speed ?? 0) > 0.05;
    dialog("Parar o carro?", (moving ? "<b>O veículo está em operação.</b> " : "") + "Sem as referências, o ESP32 corta a tração e aplica o freio.", [["Cancelar"], ["Parar", "d", () => post("/api/stop")]]);
  }
};
$("routes").addEventListener("click", (event) => {
  const rowEl = event.target.closest(".rl");
  if (!rowEl || !last) return;
  const id = rowEl.dataset.id || null, act = event.target.dataset.act;
  if (act === "rename") {
    const name = short(id);
    dialog(`Renomear ${esc(name)}`, `Novo nome:<input id="newName" value="${esc(name)}" maxlength="40">`, [["Cancelar"], ["Renomear", "p", async () => { const r = await post("/api/route/rename", { id, name: $("newName").value }); if (r) await loadRoutes(); }]]);
    return;
  }
  if (act === "delete") {
    dialog(`Excluir ${esc(short(id))}?`, "A rota sai da lista e vai para a pasta <b>analises/.lixeira</b>, de onde ainda dá para recuperar.", [["Cancelar"], ["Excluir", "d", async () => { const r = await post("/api/route/delete", { id }); if (r) await loadRoutes(); }]]);
    return;
  }
  if (id === (last.selected || null)) return;
  const route = routes.find((item) => item.id === id);
  const warning = route?.warning ? `<p><b>Atenção:</b> esta rota foi ${esc(route.warning)}. Não use no automático sem conferir.</p>` : "";
  const select = async () => { const res = await post("/api/select", { id }); if (res) await loadRoutes(); };
  const title = id ? `Trocar para ${esc(short(id))}?` : "Ficar sem rota?";
  if (last.car.running && (id || last.car.navigation)) dialog(title, `${warning}A navegação reinicia, e o gamepad para por alguns segundos.`, [["Cancelar"], ["Trocar", "p", select]]);
  else if (warning) dialog(title, warning, [["Cancelar"], ["Selecionar", "p", select]]);
  else select();
});
$("tabRoutes").onclick = () => setTab("routes");
$("tabBags").onclick = () => setTab("bags");
$("btnBagRecord").onclick = async () => {
  const info = await (await fetch("/api/bags")).json();
  dialog("Gravar bag", `Grava todos os tópicos até você clicar em Parar. ${info.free_gb === null ? "" : `Há ${num(info.free_gb, 1)} GB livres.`}<br>Nome:<input id="bagName" value="${esc(info.next_name)}" maxlength="60">`,
    [["Cancelar"], ["Começar a gravar", "p", () => post("/api/bag/record/start", { name: $("bagName").value })]]);
};
$("bags").addEventListener("click", (event) => {
  const name = event.target.closest(".rl")?.dataset.bag;
  if (!name || event.target.dataset.act !== "play") return;
  dialog(`Reproduzir ${esc(name)}?`, "Só funciona com o Odroid fora da rede e o carro parado no painel: o bag repete os comandos gravados.", [["Cancelar"], ["Reproduzir", "p", () => post("/api/bag/play", { name })]]);
});
const bagAction = async (event) => {
  const act = event.target.dataset.bagact;
  if (act === "recstop") { await post("/api/bag/record/stop"); loadBags(); }
  if (act === "pause") await post("/api/bag/pause");
  if (act === "stop") await post("/api/bag/stop");
};
$("bagRecBox").addEventListener("click", bagAction);
$("bagPlayBox").addEventListener("click", bagAction);
// Só apaga o histórico: flag ativa continua na lista até ser resolvida no carro.
$("btnClearErrors").onclick = () => post("/api/errors/clear");
$("btnRecord").onclick = async () => {
  const data = await (await fetch("/api/routes")).json();
  dialog("Gravar nova rota", `Conduza o veículo pelo traçado desejado. A rota usa os pontos do GPS.<br>Nome:<input id="recName" value="${esc(data.next_name)}" maxlength="40">`,
    [["Cancelar"], ["Começar a gravar", "p", () => post("/api/record/start", { name: $("recName").value })]]);
};
function stopRecording() {
  const rec = last?.recording;
  dialog("Parar a gravação?", rec ? `${esc(rec.name)}: ${num(rec.length_m)} m gravados.` : "", [
    ["Continuar gravando"],
    ["Descartar", "d", () => post("/api/record/stop", { save: false })],
    ["Salvar", "p", async () => {
      const r = await post("/api/record/stop", { save: true });
      if (!r?.summary) return;
      await loadRoutes();
      const s = r.summary, below = s.below_min_radius_m > 0 ? `<li><b>${num(s.below_min_radius_m, 1)} m com raio abaixo de 3,3 m</b>: o Stanley fica no limite de 20° nesses trechos</li>` : "";
      dialog(`Rota ${esc(s.name)} salva`, `<ul><li>${num(s.length_m)} m · ${s.closed ? "fechada" : "aberta"}</li><li>Raio mínimo: ${num(s.min_radius_m, 2)} m</li>${below}${s.not_fixed_points ? `<li>${s.not_fixed_points} pontos fora de RTK FIXED</li>` : ""}</ul>`, [["Fechar", "p"]]);
    }],
  ]);
}
function setView(name) {
  view = name;
  try { localStorage.setItem("painel.view", name); } catch (e) { /* sem armazenamento */ }
  $("vGraph").classList.toggle("on", name === "graph");
  $("vGps").classList.toggle("on", name === "gps");
  tilesKey = "";
  if (last) drawMap();
}
$("vGraph").onclick = () => setView("graph");
$("vGps").onclick = () => setView("gps");
$("camRoute").onclick = () => setCam("route");
$("camCar").onclick = () => setCam("car");
$("camAll").onclick = () => setCam("all");
$("zoomIn").onclick = () => zoomBy(1 / 1.5);
$("zoomOut").onclick = () => zoomBy(1.5);
const mapEl = $("map");
mapEl.addEventListener("wheel", (event) => {
  event.preventDefault();
  zoomBy(event.deltaY > 0 ? 1.2 : 1 / 1.2, event.clientX, event.clientY);
}, { passive: false });
let dragging = null;
mapEl.addEventListener("pointerdown", (event) => {
  if (!vb) return;
  dragging = { x: event.clientX, y: event.clientY, cx: vb.x + vb.w / 2, cy: vb.y + vb.h / 2, w: vb.w };
  mapEl.setPointerCapture(event.pointerId);
  mapEl.classList.add("drag");
});
mapEl.addEventListener("pointermove", (event) => {
  if (!dragging) return;
  const k = dragging.w / $("mapWrap").getBoundingClientRect().width;
  cam.cx = dragging.cx - (event.clientX - dragging.x) * k;
  cam.cy = dragging.cy - (event.clientY - dragging.y) * k;
  cam.w = dragging.w;
  if (cam.mode !== "free") { cam.mode = "free"; ["camRoute", "camCar", "camAll"].forEach((id) => $(id).classList.remove("on")); }
  drawMap();
});
const endDrag = () => { dragging = null; mapEl.classList.remove("drag"); };
mapEl.addEventListener("pointerup", endDrag);
mapEl.addEventListener("pointercancel", endDrag);
window.addEventListener("resize", () => { vb = null; tilesKey = ""; if (last) drawMap(); });
setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("pt-BR"); }, 500);
const asked = new URLSearchParams(location.search).get("view");
setView(asked === "gps" || asked === "graph" ? asked : view);
setCam(cam.mode);
loadRoutes().then(connect);
