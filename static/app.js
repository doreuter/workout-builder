"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  user: null,
  workouts: [],
  currentId: null,
  view: null,
  registering: false,
  zwiftAvailable: false, // the server can reach the Zwift folder (app running on your own computer)
  browserZwift: false,   // this browser can save into a Zwift folder the user picked
};

const EXAMPLES = [
  "15 min warm-up, 4×8 min at threshold with 3 min easy, 10 min cool-down",
  "Sweet spot ride: 3×12 min at 90% with 5 min recovery, 75 min total",
  "VO2 max: 5×4 min at 115% with 4 min easy, 1 hour total",
  "Run: 6×800 m at 5k pace with 400 m jog",
  "Easy 45 min run with 4 strides at the end",
];

const ZONES = {
  cycling: [
    { max: 0.55, name: "Recovery", color: "var(--z1)" },
    { max: 0.75, name: "Endurance", color: "var(--z2)" },
    { max: 0.90, name: "Tempo", color: "var(--z3)" },
    { max: 1.05, name: "Threshold", color: "var(--z4)" },
    { max: 1.20, name: "VO2 max", color: "var(--z5)" },
    { max: Infinity, name: "Anaerobic", color: "var(--z6)" },
  ],
  running: [
    { max: 0.80, name: "Recovery", color: "var(--z1)" },
    { max: 0.88, name: "Easy", color: "var(--z2)" },
    { max: 0.95, name: "Tempo", color: "var(--z3)" },
    { max: 1.02, name: "Threshold", color: "var(--z4)" },
    { max: 1.10, name: "Interval", color: "var(--z5)" },
    { max: Infinity, name: "Repetition", color: "var(--z6)" },
  ],
};

// --- api ---------------------------------------------------------------------

async function api(path, options = {}) {
  const { form, ...rest } = options;
  const res = await fetch(path, {
    ...rest,
    headers: options.body ? { "Content-Type": "application/json" } : {},
    body: form || (options.body ? JSON.stringify(options.body) : undefined),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "Please check the values you entered." : data.detail;
    const err = new Error(detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// --- formatting ----------------------------------------------------------------

function fmtDuration(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function fmtShort(s) {
  if (s < 60) return `${s} s`;
  if (s % 60 === 0) return `${s / 60} min`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} min`;
}

function fmtPace(secPerKm) {
  const s = Math.round(secPerKm);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} /km`;
}

function parsePace(text) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text.trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function target(sport, i) {
  if (i == null) return "free";
  const pct = `${Math.round(i * 100)}%`;
  if (sport === "cycling") return state.user.ftp ? `${pct} · ${Math.round(state.user.ftp * i)} W` : `${pct} FTP`;
  const pace = state.user.threshold_pace_s_per_km;
  return pace ? fmtPace(pace / i) : `${pct} threshold`;
}

function targetRange(sport, a, b) {
  if (a == null) return "free";
  if (b == null || a === b) return target(sport, a);
  return `${target(sport, a)} → ${target(sport, b)}`;
}

function zoneFor(sport, i) {
  if (i == null) return { name: "Free", color: "var(--z1)" };
  return ZONES[sport].find((z) => i <= z.max);
}

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

// --- auth ------------------------------------------------------------------------

function showAuth(panel = "login") {
  $("#app").hidden = true;
  $("#auth").hidden = false;
  $("#auth-form").hidden = panel !== "login";
  $("#forgot-form").hidden = panel !== "forgot";
  $("#reset-form").hidden = panel !== "reset";
}

function showMsg(node, text, kind) {
  node.textContent = text;
  node.className = `small ${kind}`;
  node.hidden = false;
}

// --- password reset ---------------------------------------------------------------------

$("#forgot-link").addEventListener("click", () => {
  $("#forgot-msg").hidden = true;
  $("#forgot-form").email.value = $("#auth-form").email.value;
  showAuth("forgot");
});
document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", () => {
  history.replaceState(null, "", location.pathname);
  showAuth("login");
}));

$("#forgot-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const r = await api("/api/password-reset/request", { method: "POST", body: { email: e.target.email.value } });
    showMsg($("#forgot-msg"), r.message, "success");
  } catch (err) {
    showMsg($("#forgot-msg"), err.message, "error");
  } finally {
    btn.disabled = false;
  }
});

$("#reset-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.password.value !== f.repeat.value) return showMsg($("#reset-msg"), "The passwords don't match.", "error");
  const token = new URLSearchParams(location.hash.slice(1)).get("reset");
  try {
    await api("/api/password-reset/confirm", { method: "POST", body: { token, password: f.password.value } });
    f.reset();
    history.replaceState(null, "", location.pathname);  // the link only works once anyway
    showMsg($("#reset-msg"), "Password saved. You can sign in now.", "success");
    setTimeout(() => showAuth("login"), 1800);
  } catch (err) {
    showMsg($("#reset-msg"), err.message, "error");
  }
});

function setAuthMode(registering) {
  state.registering = registering;
  $("#invite-field").hidden = !(registering && state.config?.invites_required);
  $("#auth-submit").textContent = registering ? "Create account" : "Sign in";
  $("#auth-switch-text").textContent = registering ? "Already have an account?" : "No account yet?";
  $("#auth-switch").textContent = registering ? "Sign in" : "Create one";
  $("#auth-form").password.autocomplete = registering ? "new-password" : "current-password";
  $("#auth-error").hidden = true;
}

$("#auth-switch").addEventListener("click", () => setAuthMode(!state.registering));

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const btn = $("#auth-submit");
  btn.disabled = true;
  try {
    const body = { email: form.email.value, password: form.password.value };
    if (state.registering && form.invite.value.trim()) body.invite_code = form.invite.value.trim();
    state.user = await api(state.registering ? "/api/register" : "/api/login", { method: "POST", body });
    form.reset();
    await startApp();
  } catch (err) {
    $("#auth-error").textContent = err.message;
    $("#auth-error").hidden = false;
  } finally {
    btn.disabled = false;
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  forgetAccount();
  showAuth();
});

// Drop everything shown for the previous account, so the next one never sees it, not even while loading.
function forgetAccount() {
  Object.assign(state, { user: null, readiness: null, currentId: null, view: null, workouts: [], plan: null, planStart: null });
  for (const sel of ["#workout-list", "#messages", "#p-weeks", "#p-messages", "#r-components", "#r-trends", "#r-acts"]) {
    $(sel).replaceChildren();
  }
  $("#r-body").hidden = true;
}

// All tabs of a browser share one login. When this tab or window comes back into view, check it's still
// the account it shows; if someone signed out or into another account elsewhere, start over.
async function checkAccount() {
  if (document.hidden || !state.user) return;
  try {
    const me = await api("/api/me");
    if (me.email !== state.user.email) location.reload();
  } catch (err) {
    if (err.status === 401) {
      forgetAccount();
      showAuth();
    }
  }
}
document.addEventListener("visibilitychange", checkAccount);
window.addEventListener("focus", checkAccount);

// --- workouts list -----------------------------------------------------------------

async function startApp() {
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#user-email").textContent = state.user.email;
  renderQuota(state.user.messages_left_today);
  $("#tab-readiness").hidden = !state.user.garmin;
  showView("workouts");
  const integrations = await api("/api/integrations").catch(() => ({}));
  state.zwiftAvailable = !!integrations.zwift?.available;
  state.device = deviceKind();
  state.browserZwift = !state.zwiftAvailable && ZwiftFolder.supported && !["phone", "tablet"].includes(state.device);
  if (state.browserZwift) await ZwiftFolder.load();
  await loadList();
  if (state.workouts.length) await openWorkout(state.workouts[0].id);
  else await newWorkout();
}

async function loadList() {
  state.workouts = await api("/api/workouts");
  renderList();
}

// Which sections are folded, per browser (Past starts folded).
const FOLDED_KEY = "vibegrind.folded";
function foldedGroups() {
  try { return JSON.parse(localStorage.getItem(FOLDED_KEY)) || { past: true }; } catch { return { past: true }; }
}
function setFolded(key, folded) {
  const all = foldedGroups();
  all[key] = folded;
  try { localStorage.setItem(FOLDED_KEY, JSON.stringify(all)); } catch { /* fine for this visit */ }
}

// "Today", "Tomorrow", "Thu 30" within the next week, else "30 Oct".
function relativeDay(iso) {
  const today = todayIso();
  const days = Math.round((new Date(`${iso}T12:00:00`) - new Date(`${today}T12:00:00`)) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  if (days > 1 && days < 7) return fmtDay(iso, { weekday: "short", day: "numeric" });
  return fmtDay(iso, { day: "numeric", month: "short" });
}

function renderList() {
  const box = $("#workout-list");
  box.replaceChildren();
  const query = $("#list-search").value.trim().toLowerCase();
  const sport = $("#list-sport").value;
  const today = todayIso();
  // Untouched drafts aren't worth a row, except the one that's open.
  const shown = state.workouts.filter((w) => (w.sport || w.name !== "New workout" || w.id === state.currentId)
    && (!sport || w.sport === sport || (!w.sport && w.id === state.currentId))
    && (!query || w.name.toLowerCase().includes(query)));
  const upcoming = shown.filter((w) => w.planned_date && w.planned_date >= today)
    .sort((a, b) => a.planned_date.localeCompare(b.planned_date) || a.id - b.id);
  const past = shown.filter((w) => w.planned_date && w.planned_date < today)
    .sort((a, b) => b.planned_date.localeCompare(a.planned_date) || b.id - a.id);
  const library = shown.filter((w) => !w.planned_date);  // already newest first from the server
  const folded = foldedGroups();
  const filtering = !!(query || sport);
  for (const [key, title, items, empty] of [
    ["upcoming", "Up next", upcoming, "Nothing planned. Put workouts on the calendar in the Plan tab."],
    ["past", "Past", past, null],
    ["library", "Library", library, "No other workouts yet."],
  ]) {
    if (!items.length && (key === "past" || filtering)) continue;
    const group = el("details", { class: "w-group" });
    // While searching, show every match; otherwise keep what the user folded.
    group.open = filtering || items.some((w) => w.id === state.currentId) || !folded[key];
    const summary = el("summary");
    summary.append(el("span", {}, title), el("span", { class: "w-count" }, String(items.length)));
    group.append(summary);
    group.addEventListener("toggle", () => { if (!filtering) setFolded(key, !group.open); });
    const ul = el("ul", { class: "workout-list" });
    if (!items.length) ul.append(el("li", { class: "w-empty" }, empty));
    for (const w of items) ul.append(listItem(w));
    group.append(ul);
    box.append(group);
  }
  if (!box.children.length) box.append(el("p", { class: "muted small w-none" }, "No workouts match."));
}

function listItem(w) {
  const li = el("li", w.id === state.currentId ? { class: "active" } : {});
  li.append(el("span", { class: "sport-dot" }, w.sport === "running" ? "🏃" : w.sport === "cycling" ? "🚴" : "•"));
  li.append(el("span", { class: "w-title" }, w.name));
  if (w.planned_date) li.append(el("span", { class: "w-when" }, relativeDay(w.planned_date)));
  const del = el("button", { class: "del", "aria-label": `Delete ${w.name}` }, "✕");
  del.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm(`Delete "${w.name}"?`)) return;
    await api(`/api/workouts/${w.id}`, { method: "DELETE" });
    await loadList();
    if (w.id === state.currentId) {
      if (state.workouts.length) await openWorkout(state.workouts[0].id);
      else await newWorkout();
    }
  });
  li.append(del);
  li.addEventListener("click", () => openWorkout(w.id));
  return li;
}

$("#list-search").addEventListener("input", renderList);
$("#list-sport").addEventListener("change", renderList);

async function newWorkout() {
  // Reuse an untouched draft instead of piling up empty ones.
  const draft = state.workouts.find((w) => !w.sport && w.name === "New workout");
  const view = draft ? await api(`/api/workouts/${draft.id}`) : await api("/api/workouts", { method: "POST" });
  if (!draft) await loadList();
  setView(view);
}

async function openWorkout(id) {
  setView(await api(`/api/workouts/${id}`));
}

function setView(view) {
  state.view = view;
  state.currentId = view.id;
  renderList();
  renderMessages();
  renderWorkout();
  $("#sidebar").classList.remove("open");
}

$("#new-btn").addEventListener("click", newWorkout);
$("#menu-btn").addEventListener("click", () => $("#sidebar").classList.toggle("open"));

// --- chat ---------------------------------------------------------------------------

function renderMessages(extra = []) {
  const box = $("#messages");
  box.replaceChildren();
  const all = [...state.view.transcript, ...extra];
  if (!all.length && state.view.workout) {
    box.append(el("div", { class: "msg assistant" },
      `Tell me what to change in "${state.view.workout.name}", for example "add one more interval" or "make the recoveries 1 min shorter".`));
  } else if (!all.length) {
    const intro = el("div", { class: "msg assistant" });
    intro.textContent = "Describe a ride or a run and I'll build it. You can then ask for changes, like \"make the intervals 30 s longer\".";
    const ex = el("div", { class: "examples" });
    const examples = state.user?.garmin ? ["What should I ride today, given my readiness?", ...EXAMPLES.slice(0, 3)] : EXAMPLES;
    for (const text of examples) {
      const b = el("button", { type: "button" }, text);
      b.addEventListener("click", () => sendMessage(text));
      ex.append(b);
    }
    intro.append(ex);
    box.append(intro);
  }
  for (const m of all) {
    const node = el("div", { class: `msg ${m.role}${m.cls ? " " + m.cls : ""}` });
    if (m.role === "assistant") appendFormatted(node, m.text);
    else node.textContent = m.text;
    box.append(node);
  }
  box.scrollTop = box.scrollHeight;
}

// Claude sometimes uses **bold**; show it as bold instead of raw asterisks.
function appendFormatted(node, text) {
  text.split(/(\*\*[^*]+\*\*)/g).forEach((part) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) node.append(el("strong", {}, part.slice(2, -2)));
    else if (part) node.append(document.createTextNode(part));
  });
}

async function sendMessage(text) {
  text = text.trim();
  if (!text) return;
  const id = state.currentId;
  $("#chat-input").value = "";
  $("#send-btn").disabled = true;
  renderMessages([{ role: "user", text }, { role: "assistant", cls: "pending", text: "Building your workout…" }]);
  try {
    const view = await api(`/api/workouts/${id}/chat`, { method: "POST", body: { message: text } });
    renderQuota(view.messages_left_today);
    if (state.currentId === id) setView(view);
    await loadList();
  } catch (err) {
    if (err.status === 401) return showAuth();
    if (state.currentId === id) {
      renderMessages([{ role: "user", text }, { role: "assistant", cls: "error", text: err.message }]);
      $("#chat-input").value = text;
    }
  } finally {
    $("#send-btn").disabled = false;
  }
}

// Daily chat allowance; null means unlimited (admins).
function renderQuota(left) {
  const q = $("#quota");
  q.hidden = left == null;
  if (left != null) q.textContent = left ? `${left} message${left === 1 ? "" : "s"} left today` : "No messages left today";
}

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage($("#chat-input").value);
});
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage($("#chat-input").value);
  }
});

// --- workout panel -------------------------------------------------------------------

function renderWorkout() {
  const { workout, segments, stats } = state.view;
  $("#workout-empty").hidden = !!workout;
  $("#workout-view").hidden = !workout;
  $("#export-error").hidden = true;
  $("#send-status").hidden = true;
  $("#send-zwift").hidden = !(state.zwiftAvailable || state.browserZwift);
  renderZwiftHelp();
  if (!workout) return;
  $("#w-plan-date").value = state.view.planned_date || "";
  $("#w-plan-clear").hidden = !state.view.planned_date;

  $("#w-name").textContent = workout.name;
  $("#w-desc").textContent = workout.description;
  const source = state.view.source_filename;
  $("#w-source").hidden = !source;
  if (source) {
    $("#w-source").replaceChildren("Imported from Zwift: ", el("code", {}, source),
      ". Update in Zwift replaces that file and keeps a backup.");
  }
  $("#send-zwift").textContent = source ? "Update in Zwift" : "Send to Zwift";
  $("#w-sport").textContent = workout.sport === "running" ? "🏃 Running" : "🚴 Cycling";
  $("#s-dur").textContent = fmtDuration(stats.duration_s);
  $("#s-if").textContent = stats.intensity_factor.toFixed(2);
  $("#s-tss-label").textContent = workout.sport === "running" ? "rTSS" : "TSS";
  $("#s-tss").textContent = stats.tss;

  renderChart(workout.sport, segments, stats.duration_s);
  renderLegend(workout.sport, segments);
  renderSteps(workout);

  for (const fmt of ["zwo", "fit"]) {
    const a = $(`#export-${fmt}`);
    a.href = `/api/workouts/${state.view.id}/export/${fmt}`;
    a.onclick = (e) => exportFile(e, fmt);
  }
}

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 800, H = 260;

function renderChart(sport, segments, total) {
  const svg = $("#chart");
  svg.replaceChildren();
  const wrap = svg.parentElement;
  wrap.querySelectorAll(".axis, .y-labels").forEach((n) => n.remove());

  const peak = Math.max(...segments.map((s) => Math.max(s.intensity ?? 0.5, s.intensity_end ?? 0)));
  const yMax = Math.max(1.2, Math.ceil((peak + 0.1) * 4) / 4);
  const x = (t) => (t / total) * W;
  const y = (i) => H - (i / yMax) * H;

  const yLabels = el("div", { class: "y-labels" });
  for (let g = 0.25; g < yMax; g += 0.25) {
    const line = document.createElementNS(SVG_NS, "line");
    Object.entries({ x1: 0, x2: W, y1: y(g), y2: y(g), class: Math.abs(g - 1) < 1e-9 ? "ftp" : "grid" })
      .forEach(([k, v]) => line.setAttribute(k, v));
    svg.append(line);
    if (Math.abs(g * 100 % 50) < 1e-9) {
      const lab = el("span", {}, `${Math.round(g * 100)}%`);
      lab.style.top = `${(y(g) / H) * 100}%`;
      yLabels.append(lab);
    }
  }
  wrap.append(yLabels);

  const tip = $("#tooltip");
  tip.hidden = true;
  segments.forEach((s) => {
    const a = s.intensity ?? 0.5;
    const b = s.intensity_end ?? a;
    const x0 = x(s.start_s), x1 = x(s.start_s + s.duration_s);
    const poly = document.createElementNS(SVG_NS, "polygon");
    poly.setAttribute("points", `${x0},${H} ${x0},${y(a)} ${x1},${y(b)} ${x1},${H}`);
    poly.setAttribute("class", "seg");
    poly.setAttribute("fill", zoneFor(sport, s.intensity == null ? null : (a + b) / 2).color);
    if (s.intensity == null) poly.setAttribute("fill-opacity", "0.45");
    const rep = s.repeat ? ` (${s.repeat[0]}/${s.repeat[1]})` : "";
    const len = s.distance_m ? `${s.distance_m} m (~${fmtShort(s.duration_s)})` : fmtShort(s.duration_s);
    const label = `${s.label || s.type[0].toUpperCase() + s.type.slice(1)}${rep}: ${len} @ ${targetRange(sport, s.intensity, s.intensity_end)}`;
    poly.addEventListener("mousemove", (e) => {
      const r = wrap.getBoundingClientRect();
      tip.textContent = label;
      tip.style.left = `${Math.min(Math.max(e.clientX - r.left, 90), r.width - 90)}px`;
      tip.style.top = `${e.clientY - r.top - 8}px`;
      tip.hidden = false;
    });
    poly.addEventListener("mouseleave", () => (tip.hidden = true));
    svg.append(poly);
  });

  // Time axis: pick a tick step that gives roughly 4-8 ticks.
  const axis = el("div", { class: "axis" });
  const step = [300, 600, 900, 1200, 1800, 3600].find((st) => total / st <= 8) || 3600;
  for (let t = 0; t <= total; t += step) {
    const span = el("span", {}, t ? `${Math.round(t / 60)}′` : "0");
    span.style.left = `${(t / total) * 100}%`;
    axis.append(span);
  }
  wrap.append(axis);
}

function renderLegend(sport, segments) {
  const used = new Set(segments.filter((s) => s.intensity != null)
    .map((s) => zoneFor(sport, (s.intensity + (s.intensity_end ?? s.intensity)) / 2).name));
  const legend = $("#legend");
  legend.replaceChildren();
  for (const z of ZONES[sport]) {
    if (!used.has(z.name)) continue;
    const item = el("span");
    const sw = el("i");
    sw.style.background = z.color;
    item.append(sw, z.name);
    legend.append(item);
  }
}

function messagesText(block) {
  const n = block.text_events?.length || 0;
  return n ? ` · ${n} message${n > 1 ? "s" : ""}` : "";
}

function stepText(sport, s) {
  const len = s.distance_m ? `${s.distance_m} m` : fmtShort(s.duration_s);
  const name = s.label || s.type[0].toUpperCase() + s.type.slice(1);
  const cad = s.cadence ? ` · ${s.cadence} ${sport === "running" ? "spm" : "rpm"}` : "";
  return `${name}: ${len} @ ${targetRange(sport, s.intensity, s.intensity_end)}${cad}${messagesText(s)}`;
}

function renderSteps(workout) {
  const ol = $("#step-list");
  ol.replaceChildren();
  for (const block of workout.steps) {
    if (block.type === "repeat") {
      const li = el("li", {}, `${block.count}×${messagesText(block)}`);
      const inner = el("ol");
      for (const s of block.steps) inner.append(el("li", {}, stepText(workout.sport, s)));
      li.append(inner);
      ol.append(li);
    } else {
      ol.append(el("li", {}, stepText(workout.sport, block)));
    }
  }
}

function zwiftPath() {
  return state.device === "windows" ? "Documents\\Zwift\\Workouts\\" : "Documents/Zwift/Workouts/";
}

// The line under the export buttons: where "Send to Zwift" saves, and how to get files onto devices.
function renderZwiftHelp() {
  const line = $("#zwift-folder-line");
  line.hidden = !state.browserZwift;
  if (state.browserZwift) {
    line.replaceChildren();
    if (ZwiftFolder.folderName) {
      line.append(`Send to Zwift saves into your Zwift folder `, el("code", {}, ZwiftFolder.folderName), " · ");
      const change = el("button", { type: "button", class: "link" }, "Change folder");
      change.addEventListener("click", () => connectZwiftFolder());
      line.append(change);
    } else {
      line.append("First time: Send to Zwift asks you to pick your Zwift folder once: ",
        el("code", {}, `${zwiftPath()}<the folder with a number>`), ".");
    }
  }

  const hint = $("#export-hint");
  hint.hidden = state.zwiftAvailable;
  if (state.zwiftAvailable) return;
  hint.replaceChildren();
  const garmin = ["phone", "tablet"].includes(state.device)
    ? "Garmin: on a computer, copy the .fit file to GARMIN/NewFiles on your watch or Edge (USB)."
    : "Garmin: connect your watch or Edge by USB and copy the .fit file to GARMIN/NewFiles.";
  if (state.device === "phone" || state.device === "tablet") {
    hint.append("Zwift: put the .zwo file into the Zwift folder on the computer you use Zwift on; it then syncs to your iPad, Apple TV or phone. ", garmin, " ");
  } else if (!state.browserZwift) {
    hint.append("Zwift: move the .zwo file to ", el("code", {}, `${zwiftPath()}<your Zwift ID>`), " and restart Zwift. ", garmin,
      " Tip: in Chrome or Edge, Send to Zwift saves it there for you. ");
  } else {
    hint.append(garmin, " ");
  }
  const guide = el("a", { href: "/#ride-it", target: "_blank", rel: "noopener" }, "Step-by-step guide");
  hint.append(guide);
}

async function connectZwiftFolder() {
  try {
    await ZwiftFolder.connect();
    renderZwiftHelp();
    renderZwiftSettings();
    return true;
  } catch (err) {
    if (err.name === "AbortError") return false; // picker closed
    $("#export-error").textContent = err.message;
    $("#export-error").hidden = false;
    return false;
  }
}

async function sendToZwiftFromBrowser() {
  if (!ZwiftFolder.folderName && !(await connectZwiftFolder())) return null;
  const res = await fetch(`/api/workouts/${state.currentId}/export/zwo`);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Export failed.");
  const filename = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "workout.zwo";
  try {
    return await ZwiftFolder.write(filename, await res.blob());
  } catch (err) {
    if (err.name === "NotFoundError") {
      await ZwiftFolder.disconnect();
      renderZwiftHelp();
      throw new Error("Your Zwift folder couldn't be found anymore. Click Send to Zwift again to pick it.");
    }
    throw err;
  }
}

$("#send-zwift").addEventListener("click", async () => {
  const btn = $("#send-zwift");
  $("#export-error").hidden = true;
  $("#send-status").hidden = true;
  btn.disabled = true;
  try {
    if (state.browserZwift) {
      const name = await sendToZwiftFromBrowser();
      if (name) {
        $("#send-status").textContent = `Saved to your Zwift folder as ${name}. Restart Zwift, then find it under Training → Workouts → Custom Workouts.`;
        $("#send-status").hidden = false;
      }
      return;
    }
    const res = await api(`/api/workouts/${state.currentId}/send/zwift`, { method: "POST" });
    $("#send-status").textContent = (res.replaced
      ? `Updated ${res.filename} in Zwift (the previous version was backed up).`
      : `Saved to Zwift as ${res.filename}.`) + " Restart Zwift, then find it under Training → Workouts → Custom Workouts.";
    $("#send-status").hidden = false;
  } catch (err) {
    $("#export-error").textContent = err.message;
    $("#export-error").hidden = false;
  } finally {
    btn.disabled = false;
  }
});

async function exportFile(e, fmt) {
  e.preventDefault();
  $("#export-error").hidden = true;
  const res = await fetch(e.currentTarget.href);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    $("#export-error").textContent = data.detail || "Export failed.";
    $("#export-error").hidden = false;
    return;
  }
  const blob = await res.blob();
  const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || `workout.${fmt}`;
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// --- import ------------------------------------------------------------------------------

let zwiftFiles = [];

function showImportError(message) {
  $("#import-error").textContent = message;
  $("#import-error").hidden = !message;
}

async function openImported(request) {
  showImportError("");
  try {
    const view = await request;
    $("#import").close();
    await loadList();
    setView(view);
  } catch (err) {
    showImportError(err.message);
  }
}

function renderImportList() {
  const q = $("#import-search").value.trim().toLowerCase();
  const list = $("#import-list");
  list.replaceChildren();
  const matches = zwiftFiles.filter((f) => !q || f.name.toLowerCase().includes(q) || f.filename.toLowerCase().includes(q));
  if (!matches.length) {
    list.append(el("li", { class: "empty-row" }, zwiftFiles.length ? "No matches." : "No workouts in your Zwift folder yet."));
    return;
  }
  for (const f of matches) {
    const li = el("li");
    const btn = el("button", { type: "button" });
    const icon = f.sport === "running" ? "🏃 " : f.sport === "cycling" ? "🚴 " : "";
    btn.append(el("span", {}, icon + f.name));
    btn.append(el("span", { class: "meta" }, f.error ? "can't import" : f.duration_s ? fmtDuration(f.duration_s) : ""));
    if (f.error) {
      btn.disabled = true;
      btn.title = f.error;
    }
    btn.addEventListener("click", () => {
      if (!f.file) return openImported(api("/api/import/zwift", { method: "POST", body: { filename: f.filename } }));
      const form = new FormData();
      form.append("file", f.file);
      openImported(api("/api/import/file", { method: "POST", form }));
    });
    li.append(btn);
    list.append(li);
  }
}

$("#import-btn").addEventListener("click", async () => {
  showImportError("");
  $("#import-search").value = "";
  $("#import-file").value = "";
  $("#import-zwift").hidden = !(state.zwiftAvailable || state.browserZwift);
  $("#import").showModal();
  if (state.browserZwift) loadBrowserZwiftList();
  if (state.zwiftAvailable) {
    $("#import-list").replaceChildren(el("li", { class: "empty-row" }, "Loading…"));
    try {
      zwiftFiles = (await api("/api/zwift/workouts")).sort((a, b) => a.name.localeCompare(b.name));
      renderImportList();
    } catch (err) {
      showImportError(err.message);
    }
  }
});

// Workouts from the folder this browser is connected to: read here, uploaded only when picked.
async function loadBrowserZwiftList() {
  const connected = !!ZwiftFolder.folderName;
  $("#import-connect").hidden = connected;
  $("#import-search").hidden = !connected;
  $("#import-list").hidden = !connected;
  if (!connected) return;
  $("#import-list").replaceChildren(el("li", { class: "empty-row" }, "Loading…"));
  try {
    const files = await ZwiftFolder.listWorkouts();
    zwiftFiles = await Promise.all(files.map(async (file) => {
      const doc = new DOMParser().parseFromString(await file.text(), "application/xml");
      const name = doc.querySelector("workout_file > name")?.textContent?.trim() || file.name.replace(/\.zwo$/i, "");
      const run = doc.querySelector("workout_file > sportType")?.textContent?.trim().toLowerCase() === "run";
      return { file, filename: file.name, name, sport: run ? "running" : "cycling", error: null };
    }));
    zwiftFiles.sort((a, b) => a.name.localeCompare(b.name));
    renderImportList();
  } catch (err) {
    showImportError(err.message);
  }
}

$("#import-connect-btn").addEventListener("click", async () => {
  if (await connectZwiftFolder()) loadBrowserZwiftList();
});

$("#import-close").addEventListener("click", () => $("#import").close());
$("#import-search").addEventListener("input", renderImportList);

$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (file.size > 512 * 1024) return showImportError("That file is too large to be a workout.");
  const form = new FormData();
  form.append("file", file);
  openImported(api("/api/import/file", { method: "POST", form }));
});

// --- calendar -------------------------------------------------------------------------

const isoLocal = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const todayIso = () => isoLocal(new Date());

function mondayOf(d) {
  const m = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  m.setDate(m.getDate() - ((m.getDay() + 6) % 7));
  return m;
}

async function setPlannedDate(id, date) {
  const view = await api(`/api/workouts/${id}/plan`, { method: "PUT", body: { date } });
  if (state.currentId === id) {
    state.view.planned_date = view.planned_date;
    renderWorkout();
  }
  await loadList();
}

$("#w-plan-date").addEventListener("change", (e) => setPlannedDate(state.currentId, e.target.value || null));
$("#w-plan-clear").addEventListener("click", () => setPlannedDate(state.currentId, null));

async function loadPlan() {
  if (!state.planStart) state.planStart = mondayOf(new Date());
  try {
    state.plan = await api(`/api/plan?start=${isoLocal(state.planStart)}&days=14`);
    $("#p-error").hidden = true;
    renderPlan();
  } catch (err) {
    if (err.status === 401) return showAuth();
    $("#p-error").textContent = err.message;
    $("#p-error").hidden = false;
  }
}

function renderPlan() {
  const { workouts, transcript } = state.plan;
  const start = state.planStart;
  const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 13);
  $("#p-range").textContent = `${start.toLocaleDateString(undefined, { day: "numeric", month: "short" })} – ${end.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`;
  const today = todayIso();
  const byDay = {};
  for (const w of workouts) (byDay[w.date] ||= []).push(w);

  const box = $("#p-weeks");
  box.replaceChildren();
  for (let wk = 0; wk < 2; wk++) {
    const week = el("div", { class: "p-week" });
    const monday = new Date(start.getFullYear(), start.getMonth(), start.getDate() + wk * 7);
    let secs = 0, tss = 0, count = 0;
    const grid = el("div", { class: "p-days" });
    for (let i = 0; i < 7; i++) {
      const d = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + i);
      const iso = isoLocal(d);
      const cell = el("div", { class: `p-day${iso === today ? " today" : ""}${iso < today ? " past" : ""}` });
      cell.append(el("div", { class: "p-day-head" }, d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })));
      for (const w of byDay[iso] || []) {
        secs += w.duration_s || 0;
        tss += w.tss || 0;
        count++;
        const zone = zoneFor(w.sport || "cycling", w.peak);  // coloured by the hardest part of the session
        const item = el("div", { class: "p-item", role: "button", tabindex: "0", title: `Hardest part: ${zone.name}` });
        item.style.borderLeftColor = zone.color;
        item.append(el("span", { class: "p-name" }, w.name),
          el("span", { class: "p-meta" }, [w.duration_s ? fmtDuration(w.duration_s) : null, w.tss ? `TSS ${Math.round(w.tss)}` : null].filter(Boolean).join(" · ")));
        const open = () => { showView("workouts"); openWorkout(w.id); };
        item.addEventListener("click", open);
        item.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
        const off = el("button", { type: "button", class: "p-off", "aria-label": `Take ${w.name} off the calendar` }, "✕");
        off.addEventListener("click", async (e) => {
          e.stopPropagation();
          await setPlannedDate(w.id, null);
          await loadPlan();
        });
        item.append(off);
        cell.append(item);
      }
      grid.append(cell);
    }
    const head = el("div", { class: "p-week-head" });
    head.append(el("span", {}, `Week of ${monday.toLocaleDateString(undefined, { day: "numeric", month: "short" })}`),
      el("span", { class: "muted" }, count ? `${count} workout${count === 1 ? "" : "s"} · ${fmtDuration(secs)} · TSS ${Math.round(tss)}` : "nothing planned"));
    week.append(head, grid);
    box.append(week);
  }
  renderPlanMessages(transcript);
}

const PLAN_EXAMPLES = [
  "Plan next week: 4 rides, long ride on Saturday, Wednesday off",
  "Build me a 2-week sweet spot block, 3 sessions a week",
  "Make this week easier, I'm tired",
];

function renderPlanMessages(transcript, extra = []) {
  const box = $("#p-messages");
  box.replaceChildren();
  const all = [...transcript, ...extra];
  if (!all.length) {
    const intro = el("div", { class: "msg assistant" });
    intro.textContent = "Tell me how many days you can ride and what you're training for, and I'll put workouts on your calendar. You can move or change them later.";
    const ex = el("div", { class: "examples" });
    for (const text of PLAN_EXAMPLES) {
      const b = el("button", { type: "button" }, text);
      b.addEventListener("click", () => sendPlanMessage(text));
      ex.append(b);
    }
    intro.append(ex);
    box.append(intro);
  }
  for (const m of all) {
    const node = el("div", { class: `msg ${m.role}${m.cls ? " " + m.cls : ""}` });
    if (m.role === "assistant") appendFormatted(node, m.text);
    else node.textContent = m.text;
    box.append(node);
  }
  box.scrollTop = box.scrollHeight;
}

async function sendPlanMessage(text) {
  text = text.trim();
  if (!text) return;
  $("#p-input").value = "";
  $("#p-send").disabled = true;
  const before = state.plan?.transcript || [];
  renderPlanMessages(before, [{ role: "user", text }, { role: "assistant", cls: "pending", text: "Planning…" }]);
  try {
    const r = await api("/api/plan/chat", { method: "POST", body: { message: text, today: todayIso() } });
    renderQuota(r.messages_left_today);
    if (r.changed) await loadList();
    await loadPlan();
  } catch (err) {
    if (err.status === 401) return showAuth();
    renderPlanMessages(before, [{ role: "user", text }, { role: "assistant", cls: "error", text: err.message }]);
    $("#p-input").value = text;
  } finally {
    $("#p-send").disabled = false;
  }
}

$("#p-form").addEventListener("submit", (e) => {
  e.preventDefault();
  sendPlanMessage($("#p-input").value);
});
$("#p-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendPlanMessage($("#p-input").value);
  }
});
const shiftWeeks = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + 7 * n);  // safe across DST
$("#p-prev").addEventListener("click", () => { state.planStart = shiftWeeks(state.planStart, -1); loadPlan(); });
$("#p-next").addEventListener("click", () => { state.planStart = shiftWeeks(state.planStart, 1); loadPlan(); });
$("#p-today").addEventListener("click", () => { state.planStart = mondayOf(new Date()); loadPlan(); });
$("#p-clear-open").addEventListener("click", () => {
  const start = state.planStart;
  const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 13);
  const shown = state.plan?.workouts.length || 0;
  const fmt = (d) => d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  $("#clear-shown-label").textContent = `The two weeks shown (${fmt(start)} – ${fmt(end)}, ${shown} workout${shown === 1 ? "" : "s"})`;
  $("#clear-msg").hidden = true;
  $("#plan-clear").showModal();
});

$("#clear-confirm").addEventListener("click", async () => {
  const f = $("#plan-clear form");
  const scope = f.scope.value, del = f.action.value === "delete";
  const start = state.planStart;
  const body = scope === "shown"
    ? { start: isoLocal(start), end: isoLocal(new Date(start.getFullYear(), start.getMonth(), start.getDate() + 13)), delete: del }
    : { start: todayIso(), delete: del };
  if (del && !confirm("Delete these workouts for good? This can't be undone.")) return;
  try {
    await api("/api/plan/clear", { method: "POST", body });
    $("#plan-clear").close();
    await loadList();
    if (del && !state.workouts.some((w) => w.id === state.currentId)) {  // the open workout was deleted
      if (state.workouts.length) await openWorkout(state.workouts[0].id);
      else await newWorkout();
    }
    await loadPlan();
  } catch (err) {
    $("#clear-msg").textContent = err.message;
    $("#clear-msg").hidden = false;
  }
});

$("#p-clear").addEventListener("click", async () => {
  if (!confirm("Start the planning chat over? Your planned workouts stay on the calendar.")) return;
  await api("/api/plan/chat", { method: "DELETE" });
  loadPlan();
});

// --- readiness dashboard ----------------------------------------------------------------

const PART_NAMES = { hrv: "HRV", sleep: "Sleep", recovery: "Recovery time", resting_hr: "Resting heart rate", body_battery: "Body Battery", load: "Training load" };

function showView(view) {
  state.viewName = view;
  $("#app").classList.toggle("view-full", view !== "workouts");
  $("#readiness").hidden = view !== "readiness";
  $("#plan").hidden = view !== "plan";
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  if (view === "readiness") loadReadiness(false);
  if (view === "plan") loadPlan();
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

const SUGGEST_PROMPT = "Suggest a workout for today that fits my readiness, recovery time and recent training load.";

$("#r-suggest").addEventListener("click", async () => {
  showView("workouts");
  await newWorkout();
  sendMessage(SUGGEST_PROMPT);
});
$("#r-refresh").addEventListener("click", () => loadReadiness(true));

async function loadReadiness(refresh) {
  const btn = $("#r-refresh");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  if (!state.readiness) $("#r-loading").hidden = false;
  try {
    state.readiness = await api(`/api/readiness${refresh ? "?refresh=1" : ""}`);
    renderReadiness(state.readiness);
  } catch (err) {
    $("#r-error").textContent = err.message;
    $("#r-error").hidden = false;
  } finally {
    $("#r-loading").hidden = true;
    btn.disabled = false;
    btn.textContent = "Refresh from Garmin";
  }
}

function fmtDay(iso, opts = { weekday: "short", day: "numeric", month: "short" }) {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, opts);
}

function renderReadiness(d) {
  $("#r-error").hidden = !d.sync_error;
  if (d.sync_error) $("#r-error").textContent = `${d.sync_error} Showing the data stored so far.`;
  $("#r-synced").textContent = d.synced_at
    ? `Last synced with Garmin ${new Date(d.synced_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`
    : "Not synced with Garmin yet";
  $("#r-body").hidden = false;

  const t = d.today;
  $("#r-score").textContent = t?.score ?? "–";
  const status = $("#r-status");
  status.replaceChildren();
  if (t?.verdict) {
    status.append(el("span", { class: `r-dot status-${t.verdict.key}`, "aria-hidden": "true" }), t.verdict.title);
    $("#r-advice").textContent = t.capped
      ? `${t.verdict.advice} Capped at ${t.score} while recovery time is over 36 h.`
      : t.verdict.advice;
  } else {
    status.textContent = "Not enough data yet";
    $("#r-advice").textContent = "Wear your watch overnight and sync it; readiness needs at least two of the inputs.";
  }
  $("#r-day").textContent = !t ? "" : d.is_today
    ? `For today, ${fmtDay(t.day)}`
    : `Based on ${fmtDay(t.day)}: no data for today yet. Sync your watch, then refresh.`;

  const list = $("#r-components");
  list.replaceChildren();
  for (const c of t?.components || []) {
    const li = el("li", { class: `r-part${c.score == null ? " missing" : ""}` });
    const head = el("div", { class: "r-part-head" });
    head.append(el("span", {}, `${PART_NAMES[c.key]} · ${Math.round(c.weight * 100)}%`), el("span", {}, c.score == null ? "–" : String(c.score)));
    const bar = el("div", { class: "r-bar" });
    const fill = el("i");
    fill.style.width = `${c.score ?? 0}%`;
    bar.append(fill);
    li.append(head, bar, el("div", { class: "r-part-note" }, c.note));
    list.append(li);
  }

  const load = t?.load;
  const grid = $("#r-load");
  grid.replaceChildren();
  const stat = (label, value) => {
    const box = el("div");
    box.append(el("span", { class: "stat-label" }, label), el("span", { class: "stat" }, value));
    grid.append(box);
  };
  stat("7-day avg", load ? String(Math.round(load.acute)) : "–");
  stat("28-day avg", load ? String(Math.round(load.chronic)) : "–");
  stat("Ratio", load?.ratio != null ? `${load.ratio.toFixed(2)}×` : "–");
  stat("Last 48 h", load ? String(load.last_48h) : "–");
  const r = load?.ratio;
  $("#r-load-note").textContent = r == null ? "Needs a few weeks of activities with training load."
    : r > 1.5 ? "Load is ramping up fast compared with your usual. Plan some easier days."
    : r > 1.3 ? "Load is a bit higher than usual: fine for a block, watch your recovery."
    : r < 0.8 ? "Lighter than usual: you're fresh, but fitness may slip if it lasts."
    : "Load is in a sustainable range.";

  renderTrends(d.trend, d.recovery, d.recovery_windows);
  renderActivities(d.activities);
}

const TRENDS = [
  { key: "readiness", title: "Readiness", unit: "", kind: "line", min: 0, max: 100 },
  { key: "hrv", title: "HRV (overnight)", unit: " ms", kind: "line", band: ["hrv_low", "hrv_high"], bandLabel: "usual range" },
  { key: "resting_hr", title: "Resting heart rate", unit: " bpm", kind: "line" },
  { key: "sleep_hours", title: "Sleep", unit: " h", kind: "bar", min: 0, extra: "sleep_score", extraLabel: "score",
    format: (h) => `${Math.floor(h)} h ${String(Math.round((h % 1) * 60)).padStart(2, "0")} min` },
  { key: "body_battery", title: "Body Battery (overnight high)", unit: "", kind: "bar", min: 0, max: 100 },
  { key: "load", title: "Daily training load", unit: "", kind: "bar", min: 0 },
];

function renderTrends(trend, recovery, windows) {
  const box = $("#r-trends");
  box.replaceChildren();
  for (const spec of TRENDS) box.append(trendChart(spec, trend));
  if (recovery) box.append(recoveryTimeline(recovery, windows || []));  // once Garmin's recovery time has been read
}

const HOUR = 3600 * 1000;

// Recovery windows of the last 5 days: one bar per activity from its end until its recovery time is up,
// stacked into as few rows as possible, with a line at "now".
function recoveryTimeline(rec, windows) {
  const card = el("article", { class: "card r-chart r-wide" });
  card.append(el("h3", {}, "Recovery time, last 5 days"));
  const line = el("p", { class: "r-latest" });
  line.append(el("span", { class: `r-dot small light-${rec.light}`, "aria-hidden": "true" }),
    `Now: ${rec.hours_left > 0 ? `${Math.round(rec.hours_left)} h left` : "recovered"} · ${rec.title.toLowerCase()}`);
  card.append(line);

  const now = Date.now();
  const day0 = new Date(now - 4 * 24 * HOUR);
  day0.setHours(0, 0, 0, 0);
  const t0 = day0.getTime();
  const t1 = Math.max(now, ...windows.map((w) => Date.parse(w.end))) + 2 * HOUR;
  const lanes = [];
  const bars = windows.map((w) => {
    const s = Math.max(Date.parse(w.start), t0), e = Date.parse(w.end);
    let lane = lanes.findIndex((end) => end <= s);
    if (lane < 0) lane = lanes.push(0) - 1;
    lanes[lane] = e;
    return { w, s, e, lane };
  });

  const W = window.innerWidth < 700 ? 340 : 640, laneH = 16, gap = 6, padT = 16, padB = 22;
  const H = padT + Math.max(1, lanes.length) * (laneH + gap) + padB;
  const x = (t) => ((t - t0) / (t1 - t0)) * W;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Recovery windows of the last 5 days");
  const add = (tag, attrs, text) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    svg.append(node);
    return node;
  };
  for (let d = new Date(t0); d.getTime() <= t1; d.setDate(d.getDate() + 1)) {
    const xd = x(d.getTime());
    add("line", { x1: xd, x2: xd, y1: padT, y2: H - padB, class: "gridline" });
    add("text", { x: xd + 2, y: H - 6, class: "axis-label" }, d.toLocaleDateString(undefined, { day: "numeric", month: "short" }));
  }

  const tip = el("div", { class: "tooltip", hidden: "" });
  for (const { w, s, e, lane } of bars) {
    const y = padT + lane * (laneH + gap);
    const kind = (w.type_key || "").includes("running") ? "run" : /cycling|biking|ride/.test(w.type_key || "") ? "ride" : "other";
    const bar = add("rect", { x: x(s), y, width: Math.max(3, x(e) - x(s)), height: laneH, rx: 4, class: `rt-bar rt-${kind}` });
    if (x(e) - x(s) > 34) add("text", { x: x(s) + 5, y: y + 12, class: "rt-label" }, `${Math.round(w.hours)} h`);
    const until = new Date(e).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
    bar.addEventListener("mouseenter", () => {
      tip.textContent = `${w.name || w.type_key} · ${fmtDay(w.day)} · ${Math.round(w.hours)} h, until ${until}`;
      const rect = bar.getBoundingClientRect(), cardRect = card.getBoundingClientRect();
      tip.style.left = `${rect.left - cardRect.left + rect.width / 2}px`;
      tip.style.top = `${rect.top - cardRect.top}px`;
      tip.hidden = false;
    });
    bar.addEventListener("mouseleave", () => { tip.hidden = true; });
  }
  add("line", { x1: x(now), x2: x(now), y1: padT - 3, y2: H - padB, class: "rt-now" });
  add("text", { x: x(now), y: 10, "text-anchor": "middle", class: "rt-now-label" }, "now");

  card.append(svg, tip);
  const legend = el("p", { class: "r-latest rt-legend" });
  for (const [kind, label] of [["ride", "ride"], ["run", "run"], ["other", "other"]]) {
    const item = el("span");
    item.append(el("i", { class: `rt-key rt-${kind}` }), label);
    legend.append(item);
  }
  legend.append(el("span", {}, "Bars run from the end of each activity until its recovery time is up."));
  card.append(legend);
  return card;
}

function trendChart(spec, trend) {
  const card = el("article", { class: "card r-chart" });
  const values = trend.map((p) => p[spec.key]);
  const show = (v) => (spec.format ? spec.format(v) : `${v}${spec.unit}`);
  const latest = [...trend].reverse().find((p) => p[spec.key] != null);
  card.append(el("h3", {}, spec.title));
  card.append(el("p", { class: "r-latest" }, latest
    ? `Latest ${fmtDay(latest.day, { day: "numeric", month: "short" })}: ${show(latest[spec.key])}`
      + (spec.band && latest[spec.band[0]] ? ` · usual ${latest[spec.band[0]]}–${latest[spec.band[1]]}${spec.unit}` : "")
    : "No data yet"));

  const W = 320, H = 138, padL = 30, padR = 4, padT = 8, padB = 26;  // room for 11–12.5px axis labels
  const nums = values.filter((v) => v != null);
  if (spec.band) trend.forEach((p) => { if (p[spec.band[0]]) nums.push(p[spec.band[0]], p[spec.band[1]]); });
  let lo = spec.min ?? Math.min(...nums), hi = spec.max ?? Math.max(...nums);
  if (!nums.length) { lo = 0; hi = 1; }
  if (spec.min == null) lo = Math.floor(lo - (hi - lo) * 0.15 - 1);
  if (spec.max == null) hi = Math.ceil(hi + (hi - lo) * 0.1 + 1);
  const n = trend.length;
  const step = (W - padL - padR) / n;
  const x = (i) => padL + step * (i + 0.5);
  const y = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo || 1));

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${spec.title}, last 30 days`);
  const add = (tag, attrs, text) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    svg.append(node);
    return node;
  };

  // Recessive grid: three lines with labels on the left.
  for (const v of [lo, (lo + hi) / 2, hi]) {
    add("line", { x1: padL, x2: W - padR, y1: y(v), y2: y(v), class: "gridline" });
    add("text", { x: padL - 4, y: y(v) + 3, "text-anchor": "end", class: "axis-label" }, Math.round(v));
  }
  [0, 14, n - 1].forEach((i) => add("text", { x: x(i), y: H - 3, "text-anchor": "middle", class: "axis-label" },
    fmtDay(trend[i].day, { day: "numeric", month: "short" })));

  if (spec.band) {
    const pts = trend.map((p, i) => p[spec.band[0]] ? [x(i), y(p[spec.band[1]]), y(p[spec.band[0]])] : null).filter(Boolean);
    if (pts.length > 1) {
      const top = pts.map((p) => `${p[0]},${p[1]}`).join(" ");
      const bottom = pts.reverse().map((p) => `${p[0]},${p[2]}`).join(" ");
      add("polygon", { points: `${top} ${bottom}`, class: "band" });
    }
  }

  if (spec.kind === "line") {
    // Break the line at missing days instead of bridging gaps.
    let seg = [];
    const flush = () => { if (seg.length > 1) add("polyline", { points: seg.join(" "), class: "line" }); seg = []; };
    values.forEach((v, i) => (v == null ? flush() : seg.push(`${x(i)},${y(v)}`)));
    flush();
    values.forEach((v, i) => {  // lone points between gaps still need a mark
      if (v != null && values[i - 1] == null && values[i + 1] == null) add("circle", { cx: x(i), cy: y(v), r: 2.5, class: "hover-dot" });
    });
  } else {
    const bw = Math.max(2, step - 2);  // 2px gap between bars
    values.forEach((v, i) => {
      if (v == null || v <= 0) return;
      const top = y(v), base = y(Math.max(lo, 0));
      const r = Math.min(2, bw / 2);
      add("path", { class: "bar", d: `M${x(i) - bw / 2},${base} V${top + r} q0,-${r} ${r},-${r} H${x(i) + bw / 2 - r} q${r},0 ${r},${r} V${base} Z` });
    });
  }

  // Hover: a hit column per day, wider than the mark; crosshair + tooltip.
  const hoverLine = add("line", { class: "hover-line", y1: padT, y2: H - padB, visibility: "hidden" });
  const hoverDot = add("circle", { r: 3.5, class: "hover-dot", visibility: "hidden" });
  const tip = el("div", { class: "tooltip", hidden: "" });
  trend.forEach((p, i) => {
    const hit = add("rect", { x: x(i) - step / 2, y: 0, width: step, height: H, fill: "transparent" });
    hit.addEventListener("mouseenter", () => {
      const v = p[spec.key];
      hoverLine.setAttribute("x1", x(i));
      hoverLine.setAttribute("x2", x(i));
      hoverLine.setAttribute("visibility", "visible");
      if (v != null && spec.kind === "line") {
        hoverDot.setAttribute("cx", x(i));
        hoverDot.setAttribute("cy", y(v));
        hoverDot.setAttribute("visibility", "visible");
      } else hoverDot.setAttribute("visibility", "hidden");
      let text = `${fmtDay(p.day)}: ${v == null ? "no data" : show(v)}`;
      if (spec.extra && p[spec.extra] != null) text += ` · ${spec.extraLabel} ${p[spec.extra]}`;
      if (spec.band && p[spec.band[0]]) text += ` · ${spec.bandLabel} ${p[spec.band[0]]}–${p[spec.band[1]]}`;
      tip.textContent = text;
      // Position relative to the card (SVG elements have no offsetTop).
      const rect = svg.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      const left = rect.left - cardRect.left + (x(i) / W) * rect.width;
      tip.style.left = `${Math.min(Math.max(left, 110), cardRect.width - 110)}px`;
      tip.style.top = `${rect.top - cardRect.top + ((v != null && spec.kind === "line" ? y(v) : padT) / H) * rect.height - 6}px`;
      tip.hidden = false;
    });
  });
  svg.addEventListener("mouseleave", () => {
    hoverLine.setAttribute("visibility", "hidden");
    hoverDot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
  card.append(svg, tip);
  return card;
}

function renderActivities(acts) {
  const body = $("#r-acts");
  body.replaceChildren();
  if (!acts.length) {
    const tr = el("tr");
    const td = el("td", { colspan: "7", class: "muted" }, "No activities in the last 14 days.");
    tr.append(td);
    body.append(tr);
    return;
  }
  const n = (v, digits = 0) => (v == null ? "–" : Number(v).toFixed(digits));
  const pace = (speed) => fmtPace(Math.round(1000 / speed)).replace(" /km", "");
  for (const a of acts) {
    const tr = el("tr");
    const effect = a.te_label ? a.te_label.toLowerCase().replace(/_/g, " ") : (a.aerobic_te != null ? `aerobic ${n(a.aerobic_te, 1)}` : "–");
    const running = (a.type_key || "").includes("running");
    const intensity = running && a.avg_speed > 0
      ? `${pace(a.avg_speed)} /km${a.gap_speed > 0 ? ` · GAP ${pace(a.gap_speed)}` : ""}`
      : a.norm_power ? `${n(a.norm_power)} W NP` : "–";
    const link = el("a", { href: `https://connect.garmin.com/modern/activity/${a.activity_id}`, target: "_blank", rel: "noopener" },
      a.name || a.type_key || "Activity");
    const cells = [fmtDay(a.day), link, a.duration_s ? fmtDuration(Math.round(a.duration_s)) : "–", intensity,
      n(a.training_load), a.recovery_min ? `${Math.round(a.recovery_min / 60)} h` : "–", effect];
    cells.forEach((v) => {
      const td = el("td");
      td.append(v);
      tr.append(td);
    });
    body.append(tr);
  }
}

// --- settings --------------------------------------------------------------------------

$("#settings-btn").addEventListener("click", () => {
  const f = $("#settings-form");
  f.ftp.value = state.user.ftp ?? "";
  const p = state.user.threshold_pace_s_per_km;
  f.pace.value = p ? `${Math.floor(p / 60)}:${String(p % 60).padStart(2, "0")}` : "";
  f.lthr_run.value = state.user.lthr_run ?? "";
  f.lthr_bike.value = state.user.lthr_bike ?? "";
  f.endurance_ceiling_pct.value = state.user.endurance_ceiling_pct ?? "";
  f.training_rules.value = state.user.training_rules ?? "";
  f.querySelectorAll(".filled").forEach((i) => i.classList.remove("filled"));
  $("#garmin-fetch").hidden = !state.user.garmin;
  $("#garmin-fetch-msg").textContent = "Fills in FTP and threshold heart rates. Check them, then save.";
  $("#settings-error").hidden = true;
  renderZwiftSettings();
  $("#garmin-status-box").hidden = !state.user.garmin_status;
  $("#garmin-status").textContent = state.user.garmin_status || "";
  $("#admin-invites").hidden = !state.user.is_admin;
  $("#admin-backup").hidden = !state.user.is_admin;
  if (state.user.is_admin) {
    api("/api/admin/restore").then((r) => { $("#restore-box").hidden = !r.allowed; }).catch(() => {});
    api("/api/admin/offsite").then((o) => {
      $("#offsite-status").textContent = !o.configured
        ? "Automatic off-site backups: not set up."
        : o.last_error ? `Automatic off-site backups: last attempt failed (${o.last_error}).`
        : o.last_ok_at ? `Automatic off-site backups: last one ${new Date(o.last_ok_at).toLocaleString()}, kept ${o.keep_days} days.`
        : "Automatic off-site backups: set up; the first one runs within 15 minutes.";
    }).catch(() => {});
  }
  $("#settings").showModal();
});

function renderZwiftSettings() {
  $("#zwift-folder-box").hidden = !state.browserZwift;
  if (!state.browserZwift) return;
  const name = ZwiftFolder.folderName;
  $("#zwift-folder-status").textContent = name
    ? `Connected in this browser: folder ${name}. Send to Zwift saves workouts there.`
    : "Not connected. Connect your Zwift folder so Send to Zwift can save workouts straight into it.";
  $("#zwift-folder-connect").textContent = name ? "Change folder" : "Connect";
  $("#zwift-folder-disconnect").hidden = !name;
}

$("#zwift-folder-connect").addEventListener("click", () => connectZwiftFolder());
$("#zwift-folder-disconnect").addEventListener("click", async () => {
  await ZwiftFolder.disconnect();
  renderZwiftSettings();
  renderZwiftHelp();
});

$("#delete-btn").addEventListener("click", async () => {
  const msg = $("#delete-msg");
  const password = $("#delete-password").value;
  if (!password) return showMsg(msg, "Enter your password to confirm.", "error");
  if (!confirm("Delete your VibeGrind account and all its data? This can't be undone.")) return;
  try {
    await api("/api/me/delete", { method: "POST", body: { password } });
    $("#settings").close();
    forgetAccount();
    showAuth("login");
    showMsg($("#auth-error"), "Your account and its data have been deleted.", "success");
  } catch (err) {
    showMsg(msg, err.message, "error");
  }
});

$("#restore-btn").addEventListener("click", async () => {
  const msg = $("#restore-msg");
  const file = $("#restore-file").files[0];
  msg.className = "small error";
  msg.hidden = false;
  if (!file) return (msg.textContent = "Choose the backup file first.");
  if ($("#restore-confirm").value.trim() !== "RESTORE") return (msg.textContent = "Type RESTORE to confirm.");
  const btn = $("#restore-btn");
  btn.disabled = true;
  msg.className = "small muted";
  msg.textContent = "Restoring…";
  try {
    const form = new FormData();
    form.append("file", file);
    const r = await api("/api/admin/restore", { method: "POST", form });
    msg.className = "small success";
    msg.textContent = `Restored ${r.users} accounts and ${r.workouts} workouts. Sign in again with your old account.`;
    setTimeout(() => location.reload(), 2500);
  } catch (err) {
    msg.className = "small error";
    msg.textContent = err.message;
    btn.disabled = false;
  }
});

$("#garmin-fetch-btn").addEventListener("click", async () => {
  const f = $("#settings-form");
  const btn = $("#garmin-fetch-btn");
  const msg = $("#garmin-fetch-msg");
  btn.disabled = true;
  msg.textContent = "Asking Garmin…";
  try {
    const t = await api("/api/me/garmin-thresholds");
    const filled = [];
    for (const [field, label] of [["ftp", "FTP"], ["lthr_bike", "cycling threshold HR"], ["lthr_run", "running threshold HR"]]) {
      if (t[field] == null) continue;
      f[field].value = t[field];
      f[field].classList.add("filled");
      filled.push(label);
    }
    msg.textContent = filled.length
      ? `Filled in ${filled.join(", ")} from Garmin. Garmin doesn't share threshold pace, so enter that yourself. Save to keep them.`
      : "Garmin had no FTP or threshold heart rates for you yet.";
  } catch (err) {
    msg.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#settings-save").addEventListener("click", async (e) => {
  e.preventDefault();
  const f = $("#settings-form");
  if (!f.reportValidity()) return;
  const pace = f.pace.value ? parsePace(f.pace.value) : null;
  if (f.pace.value && (pace == null || pace < 120 || pace > 900)) {
    $("#settings-error").textContent = "Enter the threshold pace as minutes:seconds per km, e.g. 4:45.";
    $("#settings-error").hidden = false;
    return;
  }
  const num = (input) => (input.value ? Number(input.value) : null);
  try {
    state.user = await api("/api/me/profile", {
      method: "PUT",
      body: {
        ftp: f.ftp.value ? Number(f.ftp.value) : null,
        threshold_pace_s_per_km: pace,
        lthr_run: num(f.lthr_run),
        lthr_bike: num(f.lthr_bike),
        endurance_ceiling_pct: num(f.endurance_ceiling_pct),
        training_rules: f.training_rules.value.trim() || null,
      },
    });
    $("#settings").close();
    if (state.currentId) await openWorkout(state.currentId);
  } catch (err) {
    $("#settings-error").textContent = err.message;
    $("#settings-error").hidden = false;
  }
});

// --- boot ---------------------------------------------------------------------------------

(async function boot() {
  state.config = await api("/api/config").catch(() => ({}));
  setAuthMode(false);
  $("#forgot-wrap").hidden = !state.config.password_reset;
  $("#backup-days").textContent = state.config.backup_keep_days ?? 14;
  if (location.hash.startsWith("#reset=")) return showAuth("reset");
  if (location.hash.startsWith("#invite=")) {  // a shared invite link: open the sign-up with the code filled in
    const code = decodeURIComponent(location.hash.slice(8));
    history.replaceState(null, "", location.pathname);
    showAuth();
    setAuthMode(true);
    $("#auth-form").invite.value = code;
    return;
  }
  try {
    state.user = await api("/api/me");
    await startApp();
  } catch {
    showAuth();
  }
})();
