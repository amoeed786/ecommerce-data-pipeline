// Shared helpers for all ShopLite pages.
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const money = (v) => "$" + Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const int = (v) => Number(v || 0).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
const badge = (status) => `<span class="badge s-${esc(status)}">${esc(status)}</span>`;

// fetch wrapper: JSON in, JSON out, throws Error(detail) on HTTP errors
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : res.statusText);
  }
  return data;
}

function toast(msg, kind = "info", ms = 3500) {
  let box = $("#toasts");
  if (!box) { box = document.createElement("div"); box.id = "toasts"; document.body.append(box); }
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  box.append(t);
  setTimeout(() => t.remove(), ms);
}

// Subscribe to the server's live change feed (Server-Sent Events). The browser reconnects by itself.
function liveEvents(onEvent) {
  const dot = $("#live");
  const es = new EventSource("/api/events");
  es.onopen = () => { dot?.classList.add("on"); if (dot) dot.textContent = "live"; };
  es.onerror = () => { dot?.classList.remove("on"); if (dot) dot.textContent = "reconnecting…"; };
  es.onmessage = (e) => onEvent(JSON.parse(e.data));
  return es;
}

// Run fn at most once per `ms`, but always run the last call (for "refresh on every event").
function throttle(fn, ms) {
  let last = 0, timer = null;
  return (...args) => {
    const wait = ms - (Date.now() - last);
    clearTimeout(timer);
    if (wait <= 0) { last = Date.now(); fn(...args); }
    else timer = setTimeout(() => { last = Date.now(); fn(...args); }, wait);
  };
}

function describeEvent(ev) {
  if (ev.kind === "order") {
    return ev.action === "created"
      ? `🛒 New order <b>#${ev.id}</b> by ${esc(ev.name)} · ${money(ev.amount)} ${badge(ev.status)}`
      : `📦 Order <b>#${ev.id}</b> → ${badge(ev.status)}`;
  }
  if (ev.kind === "customer") {
    return ev.action === "created" ? `👤 New customer <b>${esc(ev.name)}</b> (${esc(ev.detail)})`
                                   : `🏠 ${esc(ev.name)} moved to ${esc(ev.detail)}`;
  }
  if (ev.kind === "product") return `🏷️ ${esc(ev.name)} now ${money(ev.amount)}`;
  return esc(JSON.stringify(ev));
}

// Highlight the current page in the nav
document.addEventListener("DOMContentLoaded", () => {
  $$("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === location.pathname));
});
