"use strict";

// Admin page: usage numbers, invites (with a shareable sign-up link) and users (with password-reset
// links to share or email). Everything comes from /api/admin/*, which only answers admins.

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : {},
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// SQLite timestamps are UTC without a zone ("2026-10-28 08:15:00").
const parseUtc = (s) => (s ? new Date(s.includes("T") ? s : `${s.replace(" ", "T")}Z`) : null);
const fmtDate = (s) => (s ? parseUtc(s).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "–");

function ago(s) {
  const d = parseUtc(s);
  if (!d) return "never";
  const h = (Date.now() - d.getTime()) / 3600000;
  if (h < 1) return "within the hour";
  if (h < 24) return `${Math.floor(h)} h ago`;
  const days = Math.floor(h / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

let mailAvailable = false;

// --- usage ------------------------------------------------------------------------------

function renderStats(s) {
  const tiles = $("#tiles");
  tiles.replaceChildren();
  const tile = (label, value, note) => {
    const box = el("div", { class: "card tile" });
    box.append(el("span", { class: "stat-label" }, label), el("span", { class: "tile-value" }, String(value)));
    if (note) box.append(el("span", { class: "muted small" }, note));
    tiles.append(box);
  };
  tile("Users", s.users, `${s.users_new_30d} new in 30 days`);
  tile("Active, 7 days", s.active_7d, `${s.active_30d} in 30 days`);
  tile("Workouts", s.workouts, `${s.workouts_30d} in 30 days · ${s.planned} planned`);
  tile("Chat messages, 30 days", s.messages_30d);
  tile("AI cost this month", `$${s.cost_month.toFixed(2)}`, s.budget ? `of $${s.budget} budget` : "no budget set");
  renderChart(s);
}

function renderChart(s) {
  const byDay = Object.fromEntries(s.by_day.map((d) => [d.day, d]));
  const days = [];
  const start = new Date(`${s.since}T12:00:00`);
  for (let i = 0; i < 30; i++) {
    const d = new Date(start.getTime() + i * 86400000);
    const key = d.toISOString().slice(0, 10);
    days.push({ key, label: d.toLocaleDateString(undefined, { day: "numeric", month: "short" }),
      messages: byDay[key]?.messages || 0, users: byDay[key]?.users || 0 });
  }
  const total = days.reduce((a, d) => a + d.messages, 0);
  $("#chart-note").textContent = total ? `${total} messages on ${s.by_day.length} days` : "No chat messages yet.";
  $("#chart").hidden = !total;
  if (!total) return;

  const W = 640, H = 130, padL = 28, padB = 22, padT = 8;
  const max = Math.max(4, ...days.map((d) => d.messages));
  const top = Math.ceil(max / 4) * 4;
  const step = (W - padL) / days.length;
  const y = (v) => padT + (H - padT - padB) * (1 - v / top);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Chat messages per day, last 30 days");
  const add = (tag, attrs, text) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    svg.append(node);
    return node;
  };
  for (const v of [0, top / 2, top]) {
    add("line", { x1: padL, x2: W, y1: y(v), y2: y(v), class: "gridline" });
    add("text", { x: padL - 5, y: y(v) + 4, "text-anchor": "end", class: "axis-label" }, v);
  }
  days.forEach((d, i) => {
    const x = padL + i * step + 1;
    if (d.messages > 0) {
      const bar = add("rect", { x, y: y(d.messages), width: Math.max(2, step - 2), height: y(0) - y(d.messages), rx: 2, class: "bar" });
      const title = document.createElementNS(NS, "title");
      title.textContent = `${d.label}: ${d.messages} messages from ${d.users} ${d.users === 1 ? "user" : "users"}`;
      bar.append(title);
    }
    if (i % 7 === 0) add("text", { x: x + step / 2, y: H - 5, "text-anchor": "middle", class: "axis-label" }, d.label);
  });
  $("#chart").replaceChildren(svg);
}

// --- sharing ------------------------------------------------------------------------------

function openShare({ title, text, link, mailTo = "", subject, body }) {
  $("#share-title").textContent = title;
  $("#share-text").textContent = text;
  $("#share-link").value = link;
  $("#share-mail").href = `mailto:${encodeURIComponent(mailTo)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  const native = $("#share-native");
  native.hidden = typeof navigator.share !== "function";
  native.onclick = () => navigator.share({ title: subject, text: body }).catch(() => {});
  $("#share-msg").hidden = true;
  $("#share").showModal();
}

$("#share-copy").addEventListener("click", async () => {
  const msg = $("#share-msg");
  try {
    await navigator.clipboard.writeText($("#share-link").value);
    msg.textContent = "Copied.";
  } catch {
    $("#share-link").select();
    msg.textContent = "Press Cmd+C or Ctrl+C to copy.";
  }
  msg.className = "small success";
  msg.hidden = false;
});
$("#share-close").addEventListener("click", () => $("#share").close());

// --- invites ------------------------------------------------------------------------------

const inviteLink = (code) => `${location.origin}/app#invite=${encodeURIComponent(code)}`;

function shareInvite(inv) {
  const link = inviteLink(inv.code);
  openShare({
    title: `Invite ${inv.code}`,
    text: inv.note ? `For ${inv.note}. The link opens the sign-up with the code filled in.` : "The link opens the sign-up with the code filled in.",
    link,
    subject: "Your invite to VibeGrind",
    body: `Hi,\n\nhere's your invite to VibeGrind: describe a ride in plain words and get the workout for Zwift and Garmin.\n\nCreate your account here:\n${link}\n\nInvite code: ${inv.code}\n`,
  });
}

async function loadInvites() {
  const body = $("#invites tbody");
  const invites = await api("/api/admin/invites");
  body.replaceChildren();
  if (!invites.length) {
    const tr = el("tr");
    tr.append(el("td", { colspan: "4", class: "muted" }, "No invites yet."));
    body.append(tr);
  }
  for (const inv of invites) {
    const tr = el("tr", inv.used_at ? { class: "used" } : {});
    tr.append(el("td", { class: "mono" }, inv.code), el("td", {}, inv.note || "–"),
      el("td", {}, inv.used_at ? `used by ${inv.used_by_email || "a deleted account"}, ${fmtDate(inv.used_at)}` : `open since ${fmtDate(inv.created_at)}`));
    const actions = el("td", { class: "actions" });
    if (!inv.used_at) {
      const btn = el("button", { type: "button" }, "Share");
      btn.addEventListener("click", () => shareInvite(inv));
      actions.append(btn);
    }
    tr.append(actions);
    body.append(tr);
  }
}

$("#invite-create").addEventListener("click", async () => {
  try {
    const note = $("#invite-note").value.trim() || null;
    const { code } = await api("/api/admin/invites", { method: "POST", body: { note } });
    $("#invite-note").value = "";
    await loadInvites();
    shareInvite({ code, note });
  } catch (err) {
    showError(err.message);
  }
});

// --- users --------------------------------------------------------------------------------

async function resetLink(user, sendEmail) {
  if (sendEmail && !confirm(`Email a password-reset link to ${user.email}?`)) return;
  try {
    const r = await api(`/api/admin/users/${user.id}/reset`, { method: "POST", body: { send_email: sendEmail } });
    if (r.emailed) {
      alert(`Sent a reset link to ${user.email}. It works once, within 24 hours.`);
      return;
    }
    openShare({
      title: `Password reset for ${user.email}`,
      text: "Send this link to them. It works once, within 24 hours, and replaces any earlier reset link. Their old password keeps working until they use it.",
      link: r.link,
      mailTo: user.email,
      subject: "Choose a new VibeGrind password",
      body: `Hi,\n\nhere's a link to choose a new password for your VibeGrind account. It works once, within 24 hours:\n${r.link}\n`,
    });
  } catch (err) {
    showError(err.message);
  }
}

function renderUsers(data) {
  mailAvailable = data.mail;
  $("#mail-note").textContent = data.mail
    ? "Email reset sends the link straight to the user. Get link lets you send it yourself."
    : "Email isn't set up on the server yet, so reset links are shared by hand: Get link, then copy or email it.";
  const body = $("#users tbody");
  body.replaceChildren();
  for (const u of data.users) {
    const tr = el("tr");
    const who = el("td");
    who.append(el("span", {}, u.email));
    if (u.is_admin) who.append(el("span", { class: "pill" }, "admin"));
    if (u.invite_code) who.append(el("span", { class: "muted small block" }, `invite ${u.invite_code}`));
    tr.append(who, el("td", {}, fmtDate(u.created_at)), el("td", {}, ago(u.last_seen_at)),
      el("td", { class: "num" }, String(u.workouts)), el("td", { class: "num" }, String(u.planned)),
      el("td", { class: "num" }, String(u.messages_30d)), el("td", { class: "num" }, `$${u.cost_30d.toFixed(2)}`));
    const actions = el("td", { class: "actions" });
    const get = el("button", { type: "button" }, "Get link");
    get.addEventListener("click", () => resetLink(u, false));
    actions.append(get);
    if (mailAvailable) {
      const send = el("button", { type: "button" }, "Email reset");
      send.addEventListener("click", () => resetLink(u, true));
      actions.append(send);
    }
    tr.append(actions);
    body.append(tr);
  }
}

// --- boot ---------------------------------------------------------------------------------

function showError(text) {
  const box = $("#admin-error");
  box.textContent = text;
  box.hidden = false;
}

(async function boot() {
  try {
    const [stats, users] = await Promise.all([api("/api/admin/stats"), api("/api/admin/users")]);
    renderStats(stats);
    renderUsers(users);
    await loadInvites();
    $("#admin-body").hidden = false;
  } catch (err) {
    if (err.status === 401) location.href = "/app";
    else showError(err.status === 403 ? "This page is for admins only." : err.message);
  }
})();
