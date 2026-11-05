"use strict";

/* FantaLineup web: parla con fanta/server.py (JSON) e disegna campo, moduli, panchina e sfida. */

const $ = (selector, root = document) => root.querySelector(selector);
const css = getComputedStyle(document.documentElement);
const token = (name) => css.getPropertyValue(`--${name}`).trim();

const SVG_TAGS = new Set(["svg", "defs", "radialGradient", "stop", "polygon", "polyline", "path", "ellipse", "rect", "line"]);

function el(tag, attrs = {}, ...children) {
  const node = SVG_TAGS.has(tag)
    ? document.createElementNS("http://www.w3.org/2000/svg", tag)
    : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

const signed = (v) => (Math.abs(v) < 0.005 ? "0.00" : (v > 0 ? "+" : "") + v.toFixed(2));
const pct = (v) => `${Math.round(v * 100)}%`;
const arrow = (d, tol = 0.005) => (Math.abs(d) < tol ? ["=", "flat"] : [d > 0 ? "▲" : "▼", d > 0 ? "good" : "bad"]);

/* ---------- colori (dai token del CSS) ---------- */

function hexToRgb(hex) {
  let h = hex.replace("#", "");
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 128);
}
/* Difficoltà 1..5 → colore (verde, giallo, rosso dai token diff-*). */
function difficultyColor(d) {
  const stops = [[1, hexToRgb(token("diff-1"))], [3, hexToRgb(token("diff-3"))], [5, hexToRgb(token("diff-5"))]];
  const x = Math.min(5, Math.max(1, d));
  for (let i = 0; i < stops.length - 1; i++) {
    const [a, ca] = stops[i], [b, cb] = stops[i + 1];
    if (x <= b) {
      const t = (x - a) / (b - a);
      return `rgb(${ca.map((c, k) => Math.round(c + (cb[k] - c) * t)).join(",")})`;
    }
  }
  return `rgb(${stops[2][1].join(",")})`;
}
const textOn = (d) => (d > 2.2 && d < 4.2 ? "#1d1600" : "#ffffff");

/* Colore del gettone dai punti attesi: 1 = ottimo (verde) ... 5 = scarso (rosso). Scala dai dati. */
function pointsLevel(points) {
  const { lo, hi } = state.scale || { lo: 4.5, hi: 8.5 };
  const t = Math.min(1, Math.max(0, (points - lo) / (hi - lo)));
  return 1 + 4 * (1 - t);
}
function computeScale(players) {
  const values = Object.values(players).map((p) => p.points).filter((v) => v > 0).sort((a, b) => a - b);
  if (!values.length) return { lo: 4.5, hi: 8.5 };
  const lo = values[Math.floor(values.length * 0.1)], hi = values[Math.floor(values.length * 0.9)];
  return hi - lo < 0.5 ? { lo: hi - 1, hi } : { lo, hi };
}

/* ---------- stato ---------- */

const state = {
  address: null, data: null, index: 0, edits: {},
  selected: null, // id evidenziato (ultimo toccato)
  pick: null,     // primo giocatore toccato in attesa del secondo: {id, bench}
  tab: "bench", busy: 0, scale: null, manualOpponent: "",
};
const saved = (() => { try { return JSON.parse(localStorage.getItem("fantalineup") || "{}"); } catch { return {}; } })();
const persist = () => { try { localStorage.setItem("fantalineup", JSON.stringify({ address: state.address, team: $("#team").value })); } catch { /* ignora */ } };

async function api(path, body) {
  busy(true);
  try {
    const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
    const json = await res.json();
    if (!res.ok) throw new Error(json.error || "Errore");
    return json;
  } finally {
    busy(false);
  }
}
/* L'overlay compare solo se la richiesta dura più di 300 ms: gli scambi restano istantanei. */
let busyTimer = 0;
function busy(on, text = "Calcolo...") {
  state.busy += on ? 1 : -1;
  clearTimeout(busyTimer);
  if (on) $("#busy-text").textContent = text;
  if (state.busy > 0) busyTimer = setTimeout(() => { $("#busy").hidden = state.busy <= 0; }, 300);
  else $("#busy").hidden = true;
}
let statusBase = "", flashTimer = 0;
function setStatus(text) { statusBase = text; $("#status").textContent = text; }
function flash(text) {
  $("#status").textContent = text;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { $("#status").textContent = statusBase; }, 2500);
}
function fail(error) { setStatus(`Errore: ${error.message || error}`); }

const current = () => state.edits[state.index]?.lineup ?? state.data.lineups[state.index];
const player = (id) => state.data.players[id];

/* ---------- campo in prospettiva ---------- */

const W = 1000, H = 720;
function project(u, v) {
  const width = 0.60 + 0.40 * v;
  return { x: (0.5 + (u - 0.5) * width) * W, y: (0.05 + 0.91 * v) * H, s: 0.78 + 0.27 * v };
}
const poly = (pairs) => pairs.map(([u, v]) => { const p = project(u, v); return `${p.x.toFixed(1)},${p.y.toFixed(1)}`; }).join(" ");

function pitchSvg() {
  const grass = [];
  const bands = 8;
  for (let i = 0; i < bands; i++) {
    grass.push(el("polygon", { points: poly([[0, i / bands], [1, i / bands], [1, (i + 1) / bands], [0, (i + 1) / bands]]), fill: i % 2 ? token("grass-b") : token("grass-a") }));
  }
  const line = { fill: "none", stroke: token("pitch-line"), "stroke-width": 2.2, "stroke-opacity": 0.85, "stroke-linejoin": "round" };
  const circle = Array.from({ length: 49 }, (_, k) => [0.5 + 0.12 * Math.cos((k / 48) * Math.PI * 2), 0.5 + 0.16 * Math.sin((k / 48) * Math.PI * 2)]);
  return el("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
    el("defs", {},
      el("radialGradient", { id: "light", cx: "50%", cy: "55%", r: "70%" },
        el("stop", { offset: "0%", "stop-color": "#ffffff", "stop-opacity": "0.14" }),
        el("stop", { offset: "100%", "stop-color": "#000000", "stop-opacity": "0.45" }))),
    ...grass,
    el("polygon", { points: poly([[0, 0], [1, 0], [1, 1], [0, 1]]), fill: "url(#light)" }),
    el("polygon", { points: poly([[0, 0], [1, 0], [1, 1], [0, 1]]), ...line }),
    el("polyline", { points: poly([[0, 0.5], [1, 0.5]]), ...line }),
    el("polyline", { points: poly(circle), ...line }),
    el("polyline", { points: poly([[0.22, 0], [0.22, 0.17], [0.78, 0.17], [0.78, 0]]), ...line }),
    el("polyline", { points: poly([[0.38, 0], [0.38, 0.06], [0.62, 0.06], [0.62, 0]]), ...line }),
    el("polyline", { points: poly([[0.22, 1], [0.22, 0.83], [0.78, 0.83], [0.78, 1]]), ...line }),
    el("polyline", { points: poly([[0.38, 1], [0.38, 0.94], [0.62, 0.94], [0.62, 1]]), ...line }));
}

/* Gettone: punti (due cifre, colore per valore), nome con avversario colorato, titolarità in grande. */
function renderPitch() {
  const lineup = current();
  const box = $("#pitch");
  box.replaceChildren(pitchSvg());
  const rows = [["A", 0.12], ["C", 0.36], ["D", 0.60], ["P", 0.85]];
  for (const [role, v] of rows) {
    const ids = lineup[role];
    ids.forEach((id, i) => {
      const p = player(id);
      const pos = project((i + 1) / (ids.length + 1), v);
      const level = pointsLevel(p.points);
      const classes = ["w-token", id === state.selected && "is-selected"].filter(Boolean).join(" ");
      box.append(el("div", {
        class: classes, style: `left:${(pos.x / W) * 100}%;top:${(pos.y / H) * 100}%`,
        onclick: () => onPick(id), title: `${p.name} · ${p.points.toFixed(2)} punti attesi · titolarità ${pct(p.prob)}`,
      },
        el("div", { class: "w-token-inner", style: `--s:${pos.s.toFixed(3)}` },
          el("div", { class: "w-avatar", style: `background:${difficultyColor(level)};color:${textOn(level)}` }, p.points.toFixed(2)),
          el("div", { class: "w-name" }, p.name, el("small", {}, "vs ", el("span", { class: "w-opp-code", style: `color:${difficultyColor(p.difficulty)}`, text: p.opp }))),
          el("div", { class: "w-tit", text: pct(p.prob) }))));
    });
  }
}

/* Tribuna: gli esclusi come gettoni grigi, una riga per ruolo in ordine P D C A. */
function renderStand() {
  const box = $("#stand");
  const ids = current().stand || [];
  if (!ids.length) { box.replaceChildren(); return; }
  const rows = ["P", "D", "C", "A"].map((role) => {
    const group = ids.filter((id) => player(id).role === role);
    if (!group.length) return null;
    return el("div", { class: `w-stand-row role-${role}` },
      el("b", { class: "w-stand-role", text: role }),
      ...group.map((id) => {
        const p = player(id);
        return el("div", {
          class: `w-ghost ${id === state.selected ? "is-selected" : ""}`,
          onclick: () => onPick(id),
          title: `${p.name} · ${p.points.toFixed(2)} punti attesi · titolarità ${pct(p.prob)}`,
        },
          el("div", { class: "w-ghost-pts", text: p.points.toFixed(2) }),
          el("div", { class: "w-ghost-name", text: p.name }));
      }));
  });
  box.replaceChildren(el("div", { class: "w-stand-title", text: "In tribuna" }), ...rows);
}
/* ---------- moduli, riepilogo, sfida ---------- */

function renderFormations() {
  $("#formations").replaceChildren(...state.data.lineups.map((l, i) => {
    const lineup = state.edits[i]?.lineup ?? l;
    return el("button", { type: "button", class: `w-chip ${i === state.index ? "is-active" : ""}`, onclick: () => selectFormation(i), title: `Modificatore ${signed(lineup.modifier)} · titolarità ${pct(lineup.avgProb)}` },
      el("b", { text: `${i === 0 ? "★ " : ""}${l.formation}` }),
      el("span", { text: lineup.total.toFixed(2) }),
      state.edits[i] && el("i", { text: "modificato" }));
  }));
}

function duelOf() { return state.edits[state.index]?.duel ?? state.data.duel[state.index]; }

function renderSummary() {
  const lineup = current();
  const label = state.edits[state.index] ? " (modificato)" : state.index === 0 ? " (consigliato)" : "";
  $("#summary").textContent = `Modulo ${lineup.formation}${label} · ${lineup.total.toFixed(2)} punti attesi · modificatore ${signed(lineup.modifier)} · titolarità ${pct(lineup.avgProb)}`;

  const banner = $("#banner");
  const opp = state.data.opponent;
  const duel = duelOf();
  let text = "", tone = "";
  if (!opp.name) {
    text = "Sfida: scegli l'avversario nella scheda «Sfida» per vedere la probabilità di vittoria.";
    tone = "flat";
  } else if (opp.error) {
    text = `Sfida contro ${opp.name}: formazione avversaria non disponibile.`;
    tone = "flat";
  } else if (duel) {
    const verdict = duel.win - duel.loss >= 0.15 ? ["Sei favorito", "good"] : duel.loss - duel.win >= 0.15 ? ["Sei sfavorito", "bad"] : ["Sfida equilibrata", "flat"];
    text = `Sfida contro ${opp.name}: ${lineup.total.toFixed(1)} a ${opp.total.toFixed(1)} · vittoria ${pct(duel.win)}` +
      (state.data.hasThresholds ? ` · pareggio ${pct(duel.draw)}` : "") + ` · sconfitta ${pct(duel.loss)} · ${verdict[0].toLowerCase()}`;
    tone = verdict[1];
  }
  banner.textContent = text;
  banner.className = `w-banner ${tone ? "is-" + tone : ""}`;
}

/* ---------- pannello giocatore e confronto ---------- */

function renderDetail() {
  const box = $("#detail");
  const p = state.selected && player(state.selected);
  if (!p) { box.replaceChildren(el("p", { class: "w-hint", text: "Clicca un giocatore." })); return; }
  box.replaceChildren(el("h3", { text: p.title }), ...p.lines.map((line) => el("p", { text: line })));
}

function renderCompare() {
  const box = $("#compare");
  const data = state.edits[state.index]?.compare ?? state.data.compare[state.index];
  const edited = !!state.edits[state.index];
  const nodes = [el("p", { text: data.defense })];
  if (edited) {
    nodes.push(el("p", { class: "w-muted", text: "Confronto con la formazione consigliata" }),
      el("table", {}, ...data.rows.map((r) => el("tr", {},
        el("td", { text: r.label }), el("td", { text: `${r.before} → ${r.after}` }),
        el("td", { class: r.tag, text: `${r.symbol} ${r.delta}` })))));
    if (data.warn) nodes.push(el("p", { class: "bad", text: data.warn }));
  } else {
    nodes.push(el("p", { class: "w-hint", text: "Per provare un cambio: tocca un titolare sul campo e un giocatore della panchina dello stesso ruolo. Lo scambio avviene subito." }));
  }
  box.replaceChildren(...nodes);
}

/* ---------- schede ---------- */

function cell(text, cls) { return el("td", { class: cls, text }); }

function renderTab() {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === state.tab));
  const body = $("#tab-body");
  const lineup = current();
  if (state.tab === "bench") {
    const rows = lineup.bench.map((id) => {
      const p = player(id);
      return el("tr", { class: `role-${p.role} ${id === state.selected ? "is-selected" : ""}`, onclick: () => onPick(id) },
        cell(p.name), cell(p.role), cell(p.opp), cell(p.points.toFixed(2)), cell(pct(p.prob)));
    });
    body.replaceChildren(el("table", { class: "w-table" },
      el("thead", {}, el("tr", {}, ...["Giocatore", "R", "Avv.", "Punti", "Tit."].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...rows)));
  } else if (state.tab === "seriea") {
    const counts = {};
    [...lineup.P, ...lineup.D, ...lineup.C, ...lineup.A].forEach((id) => { const t = player(id).team; counts[t] = (counts[t] || 0) + 1; });
    const rows = state.data.outlooks.map((o) => {
      const mine = (counts[o.home] || 0) + (counts[o.away] || 0);
      return el("tr", { class: mine ? "row-mine" : "" },
        cell(`${o.home} - ${o.away}`), cell(pct(o.pHome)), cell(pct(o.pDraw)), cell(pct(o.pAway)),
        cell(o.favorite ? `${o.favorite} ${pct(Math.max(o.pHome, o.pAway))}` : "Equilibrata"),
        cell(`${o.score[0]}-${o.score[1]}${o.real ? " ✓" : ""}`), cell(mine || ""));
    });
    body.replaceChildren(el("table", { class: "w-table" },
      el("thead", {}, el("tr", {}, ...["Partita", "1", "X", "2", "Favorita", "Ris.", "Miei"].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...rows)));
  } else if (state.tab === "duel") {
    renderDuelTab(body);
  } else {
    const rows = state.data.schedule.map((m) => {
      const result = m.result ? `${m.result[0]}-${m.result[1]} ${m.result[0] > m.result[1] ? "V" : m.result[0] < m.result[1] ? "S" : "P"}` : "";
      return el("tr", { class: m.day === state.data.matchday ? "row-mine" : "", onclick: () => { $("#matchday").value = m.day; state.tab = "duel"; calculate(); } },
        cell(`${m.leagueDay}${m.serieA ? ` (SA ${m.serieA})` : ""}`), cell(m.home ? "Casa" : "Trasf."), cell(m.opponent), cell(result));
    });
    body.replaceChildren(rows.length
      ? el("table", { class: "w-table" }, el("thead", {}, el("tr", {}, ...["Giornata", "", "Avversario", "Esito"].map((h) => el("th", { text: h })))), el("tbody", {}, ...rows))
      : el("p", { class: "w-hint", text: state.data.calendarNote || "Non ho trovato il calendario della lega." }));
  }
}

function renderDuelTab(body) {
  const { opponent: opp, others } = state.data;
  const duel = duelOf();
  const select = el("select", { onchange: (e) => { state.manualOpponent = e.target.value === "(nessuno)" ? "" : e.target.value; calculate(); } },
    ...["(nessuno)", ...others].map((n) => el("option", { value: n, text: n, selected: n === (opp.name || "(nessuno)") })));
  const lineup = current();
  const nodes = [el("div", { class: "w-opp" }, el("span", { class: "w-muted", text: opp.auto ? "Avversario dal calendario" : "Avversario scelto da te" }), select)];
  if (!opp.name) {
    nodes.push(el("p", { class: "w-hint", text: `Nessun avversario selezionato. ${state.data.calendarNote || ""}`.trim() }));
  } else if (opp.error) {
    nodes.push(el("p", { class: "bad", text: `Non riesco a schierare ${opp.name}: ${opp.error}` }));
  } else if (duel) {
    const [sym, tag] = arrow(lineup.total - opp.total, 0.05);
    const verdict = duel.win - duel.loss >= 0.15 ? ["Sei favorito", "good"] : duel.loss - duel.win >= 0.15 ? ["Sei sfavorito", "bad"] : ["Sfida equilibrata", "flat"];
    nodes.push(
      el("p", {}, el("b", { text: `${state.data.team} vs ${opp.name}` })),
      el("p", { class: "w-muted", text: `Serie A ${state.data.matchday}${opp.leagueDay ? ` · lega ${opp.leagueDay}` : ""}` }),
      el("p", {}, `Punti attesi: ${lineup.total.toFixed(1)} (± ${duel.sd.toFixed(1)}) vs ${opp.total.toFixed(1)} (± ${opp.sd.toFixed(1)}) `, el("span", { class: tag, text: `${sym} ${(lineup.total - opp.total >= 0 ? "+" : "")}${(lineup.total - opp.total).toFixed(1)}` })),
      el("p", { text: `Moduli: ${lineup.formation} vs ${opp.formation} (al meglio)` }),
      el("p", {}, "Vittoria ", el("b", { class: verdict[1], text: pct(duel.win) }),
        state.data.hasThresholds ? ` · pari ${pct(duel.draw)} · sconfitta ${pct(duel.loss)}` : ` · sconfitta ${pct(duel.loss)} (sui punti totali)`),
      duel.goals != null && el("p", { text: `Gol attesi: ${duel.goals.toFixed(1)} - ${duel.oppGoals.toFixed(1)}` }),
      el("p", {}, el("b", { class: verdict[1], text: `${verdict[0]}. ` }), el("span", { class: "w-hint", text: "Stima indicativa." })),
      opp.played && el("p", { text: `Partita già giocata: ${opp.played[0]}-${opp.played[1]}.` }));
  }
  body.replaceChildren(...nodes);
}

/* ---------- scambi istantanei ---------- */
function zoneOf(id) {
  const lineup = current();
  return lineup.bench.includes(id) ? "bench" : (lineup.stand || []).includes(id) ? "stand" : "pitch";
}

function onPick(id) {
  const first = state.pick;
  if (first && first.id === id) {
    state.pick = null; state.selected = null;
    return renderAll();
  }
  const zone = zoneOf(id);
  if (first && first.zone !== zone && (first.zone === "pitch" || zone === "pitch")) {
    const out = zone === "pitch" ? id : first.id;
    const inn = zone === "pitch" ? first.id : id;
    if (player(out).role === player(inn).role) {
      state.pick = null;
      return swapNow(out, inn);
    }
    flash("Puoi scambiare solo giocatori dello stesso ruolo.");
  }
  state.pick = { id, zone };
  state.selected = id;
  renderAll();
}


/* Gli scambi si accodano: toccando in fretta non si perde nessuno dei due. */
let swapQueue = Promise.resolve();
function swapNow(out, inn) {
  swapQueue = swapQueue.then(() => doSwap(out, inn));
  return swapQueue;
}
async function doSwap(out, inn) {
  const edit = state.edits[state.index] || { swaps: [] };
  const swaps = [...edit.swaps, { out, in: inn }];
  try {
    const res = await api("/api/cambio", { recId: state.data.recId, index: state.index, swaps });
    state.edits[state.index] = { swaps, ...res };
    state.selected = inn;
    renderAll();
  } catch (error) { fail(error); }
}

function reset() {
  delete state.edits[state.index];
  state.pick = null; state.selected = null;
  renderAll();
}

function renderActions() { $("#reset").disabled = !state.edits[state.index]; }

function renderAll() {
  renderFormations(); renderSummary(); renderPitch(); renderDetail(); renderCompare(); renderTab(); renderActions();
}

function selectFormation(i) {
  state.index = i; state.pick = null; state.selected = null;
  renderAll();
}

/* ---------- caricamento ---------- */

async function loadLeague(address, wantedTeam) {
  try {
    const league = await api(`/api/lega?address=${encodeURIComponent(address)}`);
    state.address = address;
    const team = $("#team");
    team.replaceChildren(...league.teams.map((n) => el("option", { value: n, text: n })));
    team.value = league.teams.includes(wantedTeam) ? wantedTeam : league.teams[0];
    $("#matchday").value = league.matchday;
    state.manualOpponent = "";
    await calculate();
  } catch (error) { fail(error); }
}

async function calculate() {
  try {
    state.address = $("#league").value;
    const data = await api("/api/formazione", {
      address: state.address, team: $("#team").value, matchday: Number($("#matchday").value) || 0, avversario: state.manualOpponent || "",
    });
    state.data = data; state.edits = {}; state.index = 0;
    state.scale = computeScale(data.players);
    state.pick = null; state.selected = null;
    $("#matchday").value = data.matchday;
    const warning = $("#warning");
    warning.hidden = data.warnings.length === 0;
    warning.textContent = data.warnings.join("\n");
    renderAll();
    setStatus(`${data.leagueName} · ${data.team} · giornata ${data.matchday} · ${data.evaluated.toLocaleString("it-IT")} combinazioni valutate${data.updated ? ` · dati aggiornati ${data.updated}` : ""}`);
    persist();
  } catch (error) { fail(error); }
}

async function init() {
  $("#toolbar").addEventListener("submit", (e) => { e.preventDefault(); state.manualOpponent = ""; calculate(); });
  $("#league").addEventListener("change", () => loadLeague($("#league").value));
  $("#team").addEventListener("change", () => { state.manualOpponent = ""; calculate(); });
  $("#reset").addEventListener("click", reset);
  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && state.data) { state.tab = b.dataset.tab; renderTab(); } });

  let dayTimer = 0;
  const stepDay = (delta) => {
    const input = $("#matchday");
    input.value = Math.min(38, Math.max(1, (Number(input.value) || 1) + delta));
    clearTimeout(dayTimer);
    dayTimer = setTimeout(() => { state.manualOpponent = ""; calculate(); }, 250); // più clic ravvicinati = un solo calcolo
  };
  $("#md-down").addEventListener("click", () => stepDay(-1));
  $("#md-up").addEventListener("click", () => stepDay(1));

  try {
    const init = await api("/api/init");
    $("#league").replaceChildren(...init.leghe.map((l) => el("option", { value: l.address, text: l.label })));
    const wanted = [init.lega, saved.address, "demo"].find((a) => a && init.leghe.some((l) => l.address === a)) || "demo";
    $("#league").value = wanted;
    await loadLeague(wanted, init.squadra || (wanted === saved.address ? saved.team : null));
  } catch (error) { fail(error); }
}

window.addEventListener("load", init);