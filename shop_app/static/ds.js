// Kartora design-system components (storefront). Depends on common.js (esc, money, int).

const icon = (name, cls = "") => `<svg class="ic ${cls}" aria-hidden="true"><use href="/static/icons.svg#i-${name}"/></svg>`;

const CATEGORY_ICON = { "Electronics": "headphones", "Fashion": "shirt", "Home & Kitchen": "sofa", "Books": "book-open",
  "Sports": "dumbbell", "Beauty": "sparkles", "Toys": "toy-brick", "Grocery": "apple" };

// Precise star rating: grey stars with gold stars clipped to the exact percentage on top.
function starRating(rating, reviews) {
  const r = Math.max(0, Math.min(5, Number(rating) || 0));
  const row = Array.from({ length: 5 }, () => icon("star")).join("");
  return `<span class="pc-rating" role="img" aria-label="Rated ${r.toFixed(1)} out of 5${reviews != null ? `, ${int(reviews)} reviews` : ""}">
    <span class="k-stars"><span class="row">${row}</span><span class="fill" style="width:${(r / 5) * 100}%"><span class="row">${row}</span></span></span>
    <span aria-hidden="true">${r ? r.toFixed(1) : "New"}${reviews ? ` <span class="k-muted">(${int(reviews)})</span>` : ""}</span></span>`;
}

const wishlist = new Set(JSON.parse(localStorage.getItem("kartora.wishlist") || "[]"));
function toggleWish(id, btn) {
  wishlist.has(id) ? wishlist.delete(id) : wishlist.add(id);
  localStorage.setItem("kartora.wishlist", JSON.stringify([...wishlist]));
  btn.classList.toggle("on", wishlist.has(id));
  btn.setAttribute("aria-pressed", wishlist.has(id));
  toast(wishlist.has(id) ? "Saved to your wishlist" : "Removed from your wishlist");
}

// Product card. p: product row from /api/products (image_url, optional images[], list_price, discount_pct, stock, ...)
function productCard(p) {
  const images = (p.images && p.images.length ? p.images : [p.image_url]).filter(Boolean);
  const onSale = p.list_price > p.unit_price && p.discount_pct > 0;
  const stock = Number(p.stock ?? 99);
  const url = `#/p/${p.product_id}`;
  const media = images.length
    ? images.slice(0, 2).map((src, i) => `<img class="pc-img${i + 1}" src="${esc(src)}" alt="${i ? "" : esc(p.product_name)}" width="400" height="400"
        loading="lazy" decoding="async" onload="this.classList.add('in')" onerror="this.remove()">`).join("")
    : "";
  return `<article class="pc" data-product="${p.product_id}">
    <a class="pc-media ${images.length > 1 ? "has-2" : ""}" href="${url}" tabindex="-1" aria-hidden="true">
      <span class="pc-fallback">${icon(CATEGORY_ICON[p.category_name] || "shopping-bag")}</span>${media}
      ${onSale ? `<span class="pc-badge k-badge-sale">-${p.discount_pct}%</span>` : ""}
    </a>
    <button class="pc-wish ${wishlist.has(p.product_id) ? "on" : ""}" type="button" aria-pressed="${wishlist.has(p.product_id)}"
      aria-label="Save ${esc(p.product_name)} to wishlist" data-wish="${p.product_id}">${icon("heart", "sm")}</button>
    <div class="pc-body">
      <a class="pc-title" href="${url}">${esc(p.product_name)}</a>
      <span class="pc-store">${icon("store", "sm")}${esc(p.vendor_name || "")}</span>
      ${starRating(p.rating, p.review_count)}
      <div class="pc-price price">
        <span class="now ${onSale ? "sale" : ""}">${money(p.unit_price)}</span>
        ${onSale ? `<s aria-label="was ${money(p.list_price)}">${money(p.list_price)}</s>` : ""}
      </div>
      ${stock <= 0 ? `<span class="pc-stock out">Out of stock</span>` : stock < 5 ? `<span class="pc-stock">Only ${stock} left</span>`
        : `<span class="pc-stock" aria-hidden="true"></span>`}
      <div class="pc-foot"><button class="k-btn pc-add" type="button" data-add="${p.product_id}" ${stock <= 0 ? "disabled" : ""}
        aria-label="Add ${esc(p.product_name)} to cart">${icon("shopping-cart", "sm")}<span>Add to cart</span></button></div>
    </div>
  </article>`;
}

const productCardSkeleton = () => `<div class="pc sk-card" aria-hidden="true"><div class="pc-media k-sk" style="border-radius:0"></div>
  <div class="pc-body"><div class="k-sk" style="height:14px"></div><div class="k-sk" style="height:14px; width:70%"></div>
  <div class="k-sk" style="height:12px; width:50%; margin-top:6px"></div><div class="k-sk" style="height:22px; width:45%; margin-top:6px"></div>
  <div class="k-sk" style="height:40px; margin-top:12px"></div></div></div>`;

// Animate the product photo into the cart icon, then bump the cart.
function flyToCart(fromEl, cartEl) {
  if (!fromEl || !cartEl || matchMedia("(prefers-reduced-motion: reduce)").matches) { cartEl?.classList.add("k-bump"); return; }
  const a = fromEl.getBoundingClientRect(), b = cartEl.getBoundingClientRect();
  const ghost = fromEl.tagName === "IMG" ? fromEl.cloneNode() : document.createElement("div");
  ghost.className = "k-fly";
  Object.assign(ghost.style, { left: `${a.left}px`, top: `${a.top}px`, width: `${a.width}px`, height: `${a.height}px` });
  document.body.append(ghost);
  const dx = b.left + b.width / 2 - (a.left + a.width / 2), dy = b.top + b.height / 2 - (a.top + a.height / 2);
  ghost.animate([
    { transform: "translate(0,0) scale(1)", opacity: 1 },
    { transform: `translate(${dx * .6}px, ${dy - 80}px) scale(.45)`, opacity: .95, offset: .6 },
    { transform: `translate(${dx}px, ${dy}px) scale(.08)`, opacity: .4 },
  ], { duration: 700, easing: "cubic-bezier(.5,0,.2,1)" }).onfinish = () => {
    ghost.remove();
    cartEl.classList.remove("k-bump"); void cartEl.offsetWidth; cartEl.classList.add("k-bump");
  };
}

// One delegated listener for every card on the page: wishlist + add-to-cart.
// onAdd(productId) must add to the cart and return true on success.
function wireProductCards(root, onAdd, cartEl) {
  root.addEventListener("click", (e) => {
    const w = e.target.closest("[data-wish]");
    if (w) { e.preventDefault(); toggleWish(+w.dataset.wish, w); return; }
    const add = e.target.closest("[data-add]");
    if (!add) return;
    e.preventDefault();
    if (onAdd(+add.dataset.add) === false) return;
    const card = add.closest(".pc");
    flyToCart(card.querySelector(".pc-media img.in") || card.querySelector(".pc-media"), typeof cartEl === "function" ? cartEl() : cartEl);
    add.classList.add("added");
    add.innerHTML = `${icon("check", "sm")}<span>Added</span>`;
    setTimeout(() => { add.classList.remove("added"); add.innerHTML = `${icon("shopping-cart", "sm")}<span>Add to cart</span>`; }, 1600);
  });
}
