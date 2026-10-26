"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  user: null,
  workouts: [],
  currentId: null,
  view: null,
  registering: false,
  zwiftAvailable: false,
};

const EXAMPLES = [
  "15 min warm-up, 4×8 min at threshold with 3 min easy, 10 min cool-down",
  "Sweet spot ride: 3×12 min at 90% with 5 min recovery, 75 min total",
  "VO2 max: 5×4 min at 115% with 4 min easy, 1 hour total",
  "Easy 90 min endurance ride at 65% with 6×10 s sprints",
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

function showAuth() {
  $("#app").hidden = true;
  $("#auth").hidden = false;
}

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
  state.user = null;
  state.currentId = null;
  showAuth();
});

// --- workouts list -----------------------------------------------------------------

async function startApp() {
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#user-email").textContent = state.user.email;
  renderQuota(state.user.messages_left_today);
  const integrations = await api("/api/integrations").catch(() => ({}));
  state.zwiftAvailable = !!integrations.zwift?.available;
  await loadList();
  if (state.workouts.length) await openWorkout(state.workouts[0].id);
  else await newWorkout();
}

async function loadList() {
  state.workouts = await api("/api/workouts");
  renderList();
}

function renderList() {
  const list = $("#workout-list");
  list.replaceChildren();
  for (const w of state.workouts) {
    const li = el("li", w.id === state.currentId ? { class: "active" } : {});
    li.append(el("span", { class: "sport-dot" }, w.sport === "running" ? "🏃" : w.sport === "cycling" ? "🚴" : "•"));
    li.append(el("span", { class: "w-title" }, w.name));
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
    list.append(li);
  }
}

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
    intro.textContent = "Describe a cycling workout and I'll build it. You can then ask for changes, like \"make the intervals 30 s longer\".";
    const ex = el("div", { class: "examples" });
    for (const text of EXAMPLES) {
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
  $("#send-zwift").hidden = !state.zwiftAvailable;
  $("#export-hint").hidden = state.zwiftAvailable;
  if (!workout) return;

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

$("#send-zwift").addEventListener("click", async () => {
  const btn = $("#send-zwift");
  $("#export-error").hidden = true;
  $("#send-status").hidden = true;
  btn.disabled = true;
  try {
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
    btn.append(el("span", { class: "meta" }, f.error ? "can't import" : fmtDuration(f.duration_s)));
    if (f.error) {
      btn.disabled = true;
      btn.title = f.error;
    }
    btn.addEventListener("click", () =>
      openImported(api("/api/import/zwift", { method: "POST", body: { filename: f.filename } })));
    li.append(btn);
    list.append(li);
  }
}

$("#import-btn").addEventListener("click", async () => {
  showImportError("");
  $("#import-search").value = "";
  $("#import-file").value = "";
  $("#import-zwift").hidden = !state.zwiftAvailable;
  $("#import").showModal();
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

// --- settings --------------------------------------------------------------------------

$("#settings-btn").addEventListener("click", () => {
  const f = $("#settings-form");
  f.ftp.value = state.user.ftp ?? "";
  const p = state.user.threshold_pace_s_per_km;
  f.pace.value = p ? `${Math.floor(p / 60)}:${String(p % 60).padStart(2, "0")}` : "";
  f.max_hr.value = state.user.max_hr ?? "";
  $("#settings-error").hidden = true;
  $("#admin-invites").hidden = !state.user.is_admin;
  if (state.user.is_admin) loadInvites();
  $("#settings").showModal();
});

async function loadInvites() {
  const list = $("#invite-list");
  const invites = await api("/api/admin/invites").catch(() => []);
  list.replaceChildren();
  if (!invites.length) list.append(el("li", { class: "muted" }, "No invites yet."));
  for (const inv of invites) {
    const li = el("li", inv.used_at ? { class: "used" } : {});
    li.append(el("code", {}, inv.code));
    const who = inv.used_at ? `used by ${inv.used_by_email || "a deleted account"}` : "unused";
    li.append(el("span", { class: "muted" }, [inv.note, who].filter(Boolean).join(" · ")));
    list.append(li);
  }
}

$("#invite-create").addEventListener("click", async () => {
  try {
    await api("/api/admin/invites", { method: "POST", body: { note: $("#invite-note").value.trim() || null } });
    $("#invite-note").value = "";
    await loadInvites();
  } catch (err) {
    $("#settings-error").textContent = err.message;
    $("#settings-error").hidden = false;
  }
});

$("#settings-save").addEventListener("click", async (e) => {
  e.preventDefault();
  const f = $("#settings-form");
  if (!f.reportValidity()) return;
  const pace = f.pace.value ? parsePace(f.pace.value) : null;
  try {
    state.user = await api("/api/me/profile", {
      method: "PUT",
      body: {
        ftp: f.ftp.value ? Number(f.ftp.value) : null,
        threshold_pace_s_per_km: pace,
        max_hr: f.max_hr.value ? Number(f.max_hr.value) : null,
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
  try {
    state.user = await api("/api/me");
    await startApp();
  } catch {
    showAuth();
  }
})();
