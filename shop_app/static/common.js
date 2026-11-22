// Shared helpers for every ShopLite page.
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const money = (v) => "$" + Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const int = (v) => Number(v || 0).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
const label = (s) => String(s ?? "").replace(/_/g, " ");
const badge = (status) => `<span class="badge s-${esc(status)}">${esc(label(status))}</span>`;
const ago = (iso) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
};
const duration = (sec) => (sec == null ? "–" : sec < 90 ? `${Math.round(sec)}s` : sec < 5400 ? `${Math.round(sec / 60)} min` : `${(sec / 3600).toFixed(1)} h`);

// fetch wrapper: JSON in, JSON out, throws Error(detail) on HTTP errors
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data && data.detail;
    const err = new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : res.statusText);
    err.status = res.status;
    throw err;
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

// Live change feed (Server-Sent Events). The server only sends what this user may see.
function liveEvents(onEvent) {
  const dot = $("#live");
  const es = new EventSource("/api/events");
  es.onopen = () => { dot?.classList.add("on"); if (dot) dot.textContent = "live"; };
  es.onerror = () => { dot?.classList.remove("on"); if (dot) dot.textContent = "reconnecting…"; };
  es.onmessage = (e) => onEvent(JSON.parse(e.data));
  return es;
}

function throttle(fn, ms) {
  let last = 0, timer = null;
  return (...args) => {
    const wait = ms - (Date.now() - last);
    clearTimeout(timer);
    if (wait <= 0) { last = Date.now(); fn(...args); }
    else timer = setTimeout(() => { last = Date.now(); fn(...args); }, wait);
  };
}

// ------------------------------------------------------------------ product art (no photos needed)
const PRODUCT_EMOJI = {
  Headphones: "🎧", Smartphone: "📱", Laptop: "💻", Smartwatch: "⌚", Speaker: "🔊", Tablet: "📲", Camera: "📷", Keyboard: "⌨️",
  "T-Shirt": "👕", Jeans: "👖", Sneakers: "👟", Jacket: "🧥", Dress: "👗", Handbag: "👜", Sunglasses: "🕶️",
  Blender: "🥤", "Coffee Maker": "☕", "Cookware Set": "🍳", "Desk Lamp": "💡", Pillow: "🛏️", Vacuum: "🧹",
  Novel: "📖", Cookbook: "📕", Biography: "📘", Textbook: "📚", Comic: "📙",
  "Yoga Mat": "🧘", Dumbbells: "🏋️", Football: "⚽", "Tennis Racket": "🎾", "Bike Helmet": "🚴", "Running Shoes": "👟",
  Moisturizer: "🧴", Perfume: "🌸", Lipstick: "💄", Shampoo: "🧴", "Face Serum": "💧",
  Puzzle: "🧩", "Building Blocks": "🧱", Doll: "🪆", "RC Car": "🏎️", "Board Game": "🎲",
  "Coffee Beans": "☕", "Green Tea": "🍵", "Olive Oil": "🫒", Honey: "🍯", "Dark Chocolate": "🍫",
};
const CATEGORY_STYLE = {
  "Electronics": ["#4f7cff", "#22d3ee", "🔌"], "Fashion": ["#f472b6", "#fb923c", "👗"], "Home & Kitchen": ["#34d399", "#a3e635", "🏠"],
  "Books": ["#a78bfa", "#f0abfc", "📚"], "Sports": ["#22c55e", "#06b6d4", "🏅"], "Beauty": ["#fb7185", "#fbbf24", "💄"],
  "Toys": ["#facc15", "#f97316", "🧸"], "Grocery": ["#84cc16", "#f59e0b", "🛒"],
};
function productEmoji(name, category) {
  const hit = Object.keys(PRODUCT_EMOJI).sort((a, b) => b.length - a.length).find((k) => (name || "").includes(k));
  return hit ? PRODUCT_EMOJI[hit] : (CATEGORY_STYLE[category] || [, , "🛍️"])[2];
}
function productArt(p, size = "") {
  const [a, b] = CATEGORY_STYLE[p.category_name] || ["#94a3b8", "#cbd5e1"];
  const angle = 120 + ((p.product_id || 0) * 37) % 120;  // a little variety between products
  return `<div class="art ${size}" style="background: linear-gradient(${angle}deg, ${a}33, ${b}55)">
    ${p.discount_pct ? `<span class="off">-${p.discount_pct}%</span>` : ""}
    <span class="emoji">${productEmoji(p.product_name, p.category_name)}</span></div>`;
}
function categoryArt(name) {
  const [a, b, e] = CATEGORY_STYLE[name] || ["#94a3b8", "#cbd5e1", "🛍️"];
  return `<div class="art" style="background: linear-gradient(135deg, ${a}44, ${b}66)"><span class="emoji">${e}</span></div>`;
}
function stars(rating) {
  const r = Math.round((rating || 0) * 2) / 2;
  return `<span class="stars" title="${rating || 0} out of 5">${"★".repeat(Math.floor(r))}${r % 1 ? "⯪" : ""}${"☆".repeat(5 - Math.ceil(r))}</span>`;
}

// ------------------------------------------------------------------ signed-in user
let ME = undefined;  // undefined = not loaded yet, null = signed out
async function loadMe() {
  if (ME !== undefined) return ME;
  try { ME = await api("/api/auth/me"); } catch { ME = null; }
  return ME;
}
async function logout() {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
}
const ROLE_HOME = { customer: "/", vendor: "/vendor", rider: "/rider", admin: "/admin" };
const ROLE_LINKS = {
  admin: [["/admin", "Admin"], ["/dashboard", "Live dashboard"], ["/analytics", "Analytics"], ["/", "Storefront"], ["/docs", "API docs"]],
  vendor: [["/vendor", "Seller center"], ["/", "Storefront"]],
  rider: [["/rider", "Rider app"]],
  customer: [["/", "Shop"], ["/#/orders", "My orders"], ["/#/account", "My account"]],
};
function initials(name) { return (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase(); }

// Portal header: brand + role links + user chip with sign out
async function renderPortalHeader(subtitle) {
  const me = await loadMe();
  const links = (ROLE_LINKS[me?.role] || []).map(([href, text]) =>
    `<a href="${href}" class="${location.pathname === href ? "active" : ""}">${text}</a>`).join("");
  $("header").innerHTML = `
    <a class="brand" href="${ROLE_HOME[me?.role] || "/"}" style="color:inherit">Shop<span>Lite</span> <span class="muted small">${esc(subtitle)}</span></a>
    <nav>${links}</nav><div class="spacer"></div>
    <span id="live" class="live">connecting…</span>
    ${me ? `<span class="user-chip"><span class="avatar">${initials(me.display_name)}</span>
      <span><div>${esc(me.display_name)}</div><div class="role-tag">${esc(me.role)}</div></span></span>
      <button class="small" onclick="logout()">Sign out</button>` : `<a href="/login">Sign in</a>`}`;
  return me;
}

function describeEvent(ev) {
  if (ev.kind === "order") {
    return ev.action === "created"
      ? `🛒 New order <b>#${ev.order_id}</b> by ${esc(ev.name)} · ${money(ev.amount)} ${badge(ev.status)}`
      : `📦 Order <b>#${ev.order_id}</b> → ${badge(ev.status)}`;
  }
  if (ev.kind === "shipment") {
    const who = ev.status === "assigned" || ev.status === "picked_up" || ev.status === "delivered" ? (ev.detail ? ` · rider ${esc(ev.detail)}` : "") : "";
    return `🚚 #${ev.order_id} · ${esc(ev.name)} → ${badge(ev.status)}${who}`;
  }
  if (ev.kind === "customer") {
    return ev.action === "created" ? `👤 New customer <b>${esc(ev.name)}</b> (${esc(ev.detail)})` : `🏠 ${esc(ev.name)} moved to ${esc(ev.detail)}`;
  }
  if (ev.kind === "product") return `🏷️ ${esc(ev.name)} now ${money(ev.amount)}`;
  return esc(JSON.stringify(ev));
}

const SHIPMENT_STEPS = [
  ["placed", "Placed", "created_at", "🧾"], ["confirmed", "Confirmed", "confirmed_at", "✔"], ["packed", "Packed", "ready_at", "📦"],
  ["picked", "On the way", "picked_up_at", "🛵"], ["delivered", "Delivered", "delivered_at", "🏠"],
];
function shipmentStepper(s) {
  if (s.status === "cancelled") return `<p class="muted">❌ Cancelled ${fmtDateTime(s.cancelled_at)}</p>`;
  const reached = SHIPMENT_STEPS.map(([, , col]) => !!s[col]);
  const nowIdx = reached.lastIndexOf(true) + 1;
  return `<div class="steps">${SHIPMENT_STEPS.map(([, text, col, icon], i) => `
    <div class="step ${reached[i] ? "done" : i === nowIdx && s.status !== "returned" ? "now" : ""}">
      <div class="dot">${reached[i] ? "✓" : icon}</div>${text}<time>${s[col] ? fmtDateTime(s[col]) : ""}</time></div>`).join("")}</div>`;
}

document.addEventListener("DOMContentLoaded", () => {
  $$("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === location.pathname));
  document.addEventListener("click", (e) => {  // close dropdown menus when clicking elsewhere
    $$(".menu.open").forEach((m) => { if (!m.contains(e.target)) m.classList.remove("open"); });
  });
});
