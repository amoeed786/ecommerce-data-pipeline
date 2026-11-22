// Shared helpers for every Kartora page.
try { const t = localStorage.getItem("ka.theme"); if (t && t !== "system") document.documentElement.dataset.theme = t; } catch {}
// ---- Brand: change the name here and every page follows. ----
const BRAND = { name: "Kartora", tagline: "Everything you love, delivered." };
const LOGO_SVG = (size = 30) => `<svg width="${size}" height="${size}" viewBox="0 0 40 40" aria-hidden="true">
  <defs><linearGradient id="kg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6d4bff"/><stop offset="1" stop-color="#ff6a3d"/></linearGradient></defs>
  <rect width="40" height="40" rx="11" fill="url(#kg)"/>
  <path d="M13 15h14l-1.4 15.2a2 2 0 0 1-2 1.8h-7.2a2 2 0 0 1-2-1.8z" fill="#fff"/>
  <path d="M16 15v-2a4 4 0 0 1 8 0v2" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
  <path d="M18 19.5v8M18 23.5l4-4M19.3 22.2l3 5.3" stroke="#5b3df5" stroke-width="2" stroke-linecap="round" fill="none"/></svg>`;
const brandMark = (sub = "", size = 30) =>
  `<span class="brandmark">${LOGO_SVG(size)}<span class="bm-name">${BRAND.name}</span>${sub ? `<span class="bm-sub">${sub}</span>` : ""}</span>`;
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

// One market: Pakistan, PKR. Prices are whole rupees ("Rs 4,999"); amounts with paisa keep 2 decimals.
const CURRENCY = { symbol: "Rs", locale: "en-PK" };
const money = (v) => {
  const n = Number(v || 0), whole = Math.abs(n % 1) < 0.005;
  return `${n < 0 ? "−" : ""}${CURRENCY.symbol} ${Math.abs(n).toLocaleString(CURRENCY.locale, { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2 })}`;
};
const moneyShort = (v) => { const a = Math.abs(v), s = v < 0 ? "−" : "";
  return `${s}${CURRENCY.symbol} ${a >= 1e7 ? (a / 1e7).toFixed(1) + " Cr" : a >= 1e5 ? (a / 1e5).toFixed(1) + " Lac" : a >= 1e3 ? (a / 1e3).toFixed(0) + "k" : a.toFixed(0)}`; };
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

// Read-through cache for public catalog data: back/forward and repeat visits render instantly, and two
// parts of a page asking for the same thing share one request. Never used for carts, orders or anything private.
const _apiCache = new Map();
function apiCached(path, ttlMs = 30000) {
  const hit = _apiCache.get(path);
  if (hit && Date.now() - hit.t < ttlMs) return hit.p;
  const p = api(path).catch((e) => { _apiCache.delete(path); throw e; });
  _apiCache.set(path, { t: Date.now(), p });
  if (_apiCache.size > 300) _apiCache.delete(_apiCache.keys().next().value);
  return p;
}
const prefetch = (path) => { apiCached(path).catch(() => {}); };

// Loading placeholders that match the real layout, so the page doesn't jump when data arrives
const skeletonCards = (n = 8) => `<div class="pgrid">${Array.from({ length: n }, () =>
  `<div class="pcard sk-card"><div class="sk sk-img"></div><div class="sk sk-line"></div><div class="sk sk-line short"></div><div class="sk sk-line price"></div></div>`).join("")}</div>`;
const skeletonBlock = (h = 120) => `<div class="sk" style="height:${h}px; border-radius:12px; margin-bottom:14px"></div>`;

// Replace an element's content only if it actually changed. Live pages refresh often; skipping identical
// redraws means no flicker, and hover/scroll/focus stay where they were. Returns true if it redrew.
function setHTML(el, html) {
  if (!el || el._html === html) return false;
  el.innerHTML = html;
  el._html = html;
  return true;
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

// ------------------------------------------------------------------ product art (photo, or a category icon)
// icon(), CATEGORY_ICON and starRating() come from ds.js (loaded on every page after this file).
const _icon = (name, cls = "") => (typeof icon === "function" ? icon(name, cls) : "");
const _catIcon = (cat) => (typeof CATEGORY_ICON === "object" && CATEGORY_ICON[cat]) || "shopping-bag";
// Product picture: the real photo when there is one, with the category icon underneath as a fallback
// (if the photo can't load, it removes itself and the icon shows).
function productArt(p, size = "") {
  const src = p.image_url || (p.images && p.images[0] && (p.images[0].src || p.images[0]));
  return `<div class="art ${size} ${src ? "photo" : ""}">
    ${p.discount_pct ? `<span class="off">-${p.discount_pct}%</span>` : ""}
    ${src ? `<img src="${esc(src)}" alt="${esc(p.product_name)}" loading="lazy" decoding="async"
      onload="this.classList.add('in')" onerror="this.parentElement.classList.remove('photo'); this.remove()">` : ""}
    ${_icon(_catIcon(p.category_name))}</div>`;
}
// Photo credits for the openly licensed stock photos (CC licenses require attribution)
let CREDITS;
async function photoCredit(p) {
  if (!p.image_url || !p.image_url.startsWith("/static/img/products/")) return "";
  CREDITS ||= await fetch("/static/img/products/credits.json").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
  const file = p.image_url.split("/").pop(), c = CREDITS[file.replace(/-(240|480|960)\.webp$/, "")] || CREDITS[file];
  if (!c) return "";
  return `<div class="credit">Photo: <a href="${esc(c.source)}" target="_blank" rel="noopener">${esc(c.title || "untitled")}</a>
    by ${esc(c.creator || "unknown")} · ${esc((c.license || "").toUpperCase())} ${esc(c.license_version || "")}</div>`;
}
function categoryArt(name) { return `<div class="art">${_icon(_catIcon(name))}</div>`; }
function stars(rating) {
  if (typeof starRating === "function") return starRating(rating).replace("pc-rating", "stars");
  return `<span class="stars">${Number(rating || 0).toFixed(1)} / 5</span>`;
}

// ------------------------------------------------------------------ signed-in user
let ME = undefined;  // undefined = not loaded yet, null = signed out
async function loadMe() {
  if (ME !== undefined) return ME;
  try { ME = await api("/api/auth/me"); } catch { ME = null; }
  return ME;
}
// The admin area's location (ADMIN_PATH in .env) is never written into public pages: the server tells
// a signed-in admin where it is (ME.admin_path).
const adminBase = () => ME?.admin_path || "";
async function logout() {
  const adminLogin = ME?.role === "admin" ? adminBase() + "/login" : null;
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  location.href = adminLogin || "/login";  // admins go back to their own sign-in page
}
const ROLE_HOME = { customer: "/", vendor: "/vendor", rider: "/rider" };
const roleHome = (me) => (me?.role === "admin" ? me.admin_path : ROLE_HOME[me?.role]) || "/";
const ROLE_LINKS = {
  admin: [["", "Control room"], ["/dashboard", "Live ops"], ["/analytics", "Business intelligence"]],  // relative to admin_path
  vendor: [["/vendor", "Seller center"], ["/", "Storefront"]],
  rider: [["/rider", "Rider app"]],
  customer: [["/", "Shop"], ["/#/orders", "My orders"], ["/#/account", "My account"]],
};
const roleLinks = (me) => me?.role === "admin"
  ? [...ROLE_LINKS.admin.map(([p, t]) => [me.admin_path + p, t]), ["/", "Storefront"], ["/docs", "API docs"]]
  : ROLE_LINKS[me?.role] || [];
function initials(name) { return (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase(); }

// Portal header: brand + role links + user chip with sign out
async function renderPortalHeader(subtitle) {
  const me = await loadMe();
  const links = roleLinks(me).map(([href, text]) =>
    `<a href="${href}" class="${location.pathname === href ? "active" : ""}">${text}</a>`).join("");
  // in-page admin links are written as data-admin-href="/analytics" and resolved here
  $$("[data-admin-href]").forEach((a) => (a.href = adminBase() + a.dataset.adminHref));
  $("header").innerHTML = `
    <a href="${roleHome(me)}" style="color:inherit">${brandMark(esc(subtitle), 28)}</a>
    <nav>${links}</nav><div class="spacer"></div>
    <span id="live" class="live">connecting…</span>
    ${me ? `<span class="user-chip"><span class="avatar">${initials(me.display_name)}</span>
      <span><div>${esc(me.display_name)}</div><div class="role-tag">${esc(me.role)}</div></span></span>
      <button class="small" onclick="logout()">Sign out</button>` : `<a href="/login">Sign in</a>`}`;
  return me;
}

// Admin pages outside the console (live ops, analytics) get the console's sidebar + top bar, so every admin
// screen looks and navigates the same. Needs ds.css + admin.css + ds.js on the page.
const ADMIN_NAV = [
  ["Operations", [["orders", "Orders", "inbox"], ["shipments", "Live shipments", "kanban"], ["riders", "Riders", "bike"], ["@live", "Live ops", "activity"]]],
  ["Marketplace", [["vendors", "Vendors", "store"], ["catalog", "Catalog", "boxes"], ["users", "Users", "users"]]],
  ["Money", [["finance", "Finance", "landmark"], ["payouts", "Payouts", "send"]]],
  ["Data", [["pipeline", "Pipeline", "workflow"], ["@analytics", "Analytics", "chart-column"], ["@docs", "API docs", "file-code"]]],
];
const adminHref = (k, base) => (k === "@live" ? `${base}/dashboard` : k === "@analytics" ? `${base}/analytics` : k === "@docs" ? "/docs" : `${base}#/${k}`);
async function mountAdminShell(active, title) {
  const me = await loadMe();
  if (!me || me.role !== "admin") { location.href = "/"; return null; }
  const base = me.admin_path, I = (n, c = "") => _icon(n, c);
  document.body.classList.add("k");
  const theme = (() => { try { return localStorage.getItem("ka.theme") || "system"; } catch { return "system"; } })();
  const app = document.createElement("div");
  app.className = "app"; app.id = "app";
  try { if (localStorage.getItem("ka.side") === "c") app.classList.add("collapsed"); } catch {}
  app.innerHTML = `<aside class="side" aria-label="Admin navigation"><div class="side-head">${LOGO_SVG(26)}<span class="name">${BRAND.name}</span><span class="tag">Admin</span></div>
    <nav>${ADMIN_NAV.map(([g, items], gi) => `${gi ? '<div class="grp-sep"></div>' : ""}<div class="grp">${g}</div>${items.map(([k, l, ic]) => `<a class="nav ${k === active ? "on" : ""}" href="${adminHref(k, base)}" title="${l}" ${k === "@docs" ? 'target="_blank" rel="noopener"' : ""}>${I(ic)}<span class="lbl">${l}</span>${k === "@docs" ? I("external-link", "ext") : ""}</a>`).join("")}`).join("")}</nav>
    <div class="side-foot"><a class="nav" href="#" id="shell-collapse">${I("panel-left")}<span class="lbl">Collapse</span></a></div></aside>
    <header class="top"><span class="crumb">${esc(title)}</span>
      <a class="cmdk-btn" href="${base}#/orders" aria-label="Search orders">${I("search")}<span class="lbl">Search orders, pages, actions…</span><kbd>Ctrl K</kbd></a>
      <span class="live" id="live">connecting</span>
      <a class="ibtn" href="/" target="_blank" rel="noopener" title="Storefront" aria-label="Open storefront in a new tab">${I("external-link")}</a>
      <div class="menu" id="shell-me"><button class="ibtn" aria-label="Admin menu"><span class="avatar" style="background:var(--k-brand)">${esc(initials(me.display_name))}</span></button>
        <div class="pop"><div class="hd">${esc(me.display_name)}<div style="text-transform:none; font-weight:400; letter-spacing:0">${esc(me.email)}</div></div><div class="sep"></div>
          <div style="padding:6px 10px"><div class="muted" style="font-size:11px; margin-bottom:6px">Theme</div><div class="seg">${[["light", "sun", "Light"], ["dark", "moon", "Dark"], ["system", "monitor", "Auto"]].map(([v, ic, l]) => `<button class="${theme === v ? "on" : ""}" data-theme-set="${v}">${I(ic, "xs")}${l}</button>`).join("")}</div></div>
          <div class="sep"></div><button class="mi" onclick="logout()">${I("log-out")}Sign out</button></div></div>
    </header><main class="page"><div class="wrap" id="shell-wrap"></div></main>`;
  const old = $("main");
  $$("body > header").forEach((h) => h.remove());
  document.body.prepend(app);
  if (old) { $("#shell-wrap").append(...old.childNodes); old.remove(); }
  $("#shell-collapse").onclick = (e) => { e.preventDefault(); const c = app.classList.toggle("collapsed"); try { localStorage.setItem("ka.side", c ? "c" : ""); } catch {} };
  $("#shell-me > button").onclick = (e) => { e.stopPropagation(); $("#shell-me").classList.toggle("open"); };
  $$("[data-theme-set]").forEach((b) => (b.onclick = (e) => {
    e.stopPropagation(); const t = b.dataset.themeSet;
    try { localStorage.setItem("ka.theme", t); } catch {}
    if (t === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    $$("[data-theme-set]").forEach((x) => x.classList.toggle("on", x === b));
    document.dispatchEvent(new CustomEvent("k-theme"));
  }));
  document.title = `${title} · ${BRAND.name} admin`;
  return me;
}

function describeEvent(ev) {
  if (ev.kind === "order") {
    return ev.action === "created"
      ? `${_icon("shopping-cart")} New order <b>#${ev.order_id}</b> by ${esc(ev.name)} · ${money(ev.amount)} ${badge(ev.status)}`
      : `${_icon("package")} Order <b>#${ev.order_id}</b> → ${badge(ev.status)}`;
  }
  if (ev.kind === "shipment") {
    const who = ev.status === "assigned" || ev.status === "picked_up" || ev.status === "delivered" ? (ev.detail ? ` · rider ${esc(ev.detail)}` : "") : "";
    return `${_icon("truck")} #${ev.order_id} · ${esc(ev.name)} → ${badge(ev.status)}${who}`;
  }
  if (ev.kind === "customer") {
    return ev.action === "created" ? `${_icon("user")} New customer <b>${esc(ev.name)}</b> (${esc(ev.detail)})` : `${_icon("map-pin")} ${esc(ev.name)} moved to ${esc(ev.detail)}`;
  }
  if (ev.kind === "product") return `${_icon("tag")} ${esc(ev.name)} now ${money(ev.amount)}`;
  return esc(JSON.stringify(ev));
}

const SHIPMENT_STEPS = [
  ["placed", "Placed", "created_at", "receipt"], ["confirmed", "Confirmed", "confirmed_at", "circle-check"], ["packed", "Packed", "ready_at", "package"],
  ["picked", "On the way", "picked_up_at", "truck"], ["delivered", "Delivered", "delivered_at", "house"],
];
function shipmentStepper(s) {
  if (s.status === "cancelled") return `<p class="muted">${_icon("x", "sm")} Cancelled ${fmtDateTime(s.cancelled_at)}</p>`;
  const reached = SHIPMENT_STEPS.map(([, , col]) => !!s[col]);
  const nowIdx = reached.lastIndexOf(true) + 1;
  return `<div class="steps">${SHIPMENT_STEPS.map(([, text, col, icon], i) => `
    <div class="step ${reached[i] ? "done" : i === nowIdx && s.status !== "returned" ? "now" : ""}">
      <div class="dot">${_icon(reached[i] ? "check" : icon)}</div>${text}<time>${s[col] ? fmtDateTime(s[col]) : ""}</time></div>`).join("")}</div>`;
}

document.addEventListener("DOMContentLoaded", () => {
  // favicon on every page
  const icon = document.createElement("link");
  icon.rel = "icon"; icon.href = "data:image/svg+xml," + encodeURIComponent(LOGO_SVG(40).replace('aria-hidden="true"', 'xmlns="http://www.w3.org/2000/svg"'));
  document.head.append(icon);
  $$("[data-brandmark]").forEach((el) => (el.innerHTML = brandMark(el.dataset.brandmark, +el.dataset.size || 30)));
  $$("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === location.pathname));
  document.addEventListener("click", (e) => {  // close dropdown menus when clicking elsewhere
    $$(".menu.open").forEach((m) => { if (!m.contains(e.target)) m.classList.remove("open"); });
  });
});
