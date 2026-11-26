# Raunaqi - Phase 1 audit

Audited 2026-11-22 against commit `4441b71`, on a local test server with the live ShopDB data.

## How the audit was done

- **Crawl.** A headless-Edge script opened every page as each role (logged out, shopper, seller, rider and admin), 31 routes in all.
  - Each page was checked at 1440 px in light and dark mode and at 390 px, which is 93 page views per pass. Four passes were run.
  - The crawl covered empty states (empty cart, wishlist, product without a photo), not-found states (unknown product), loading skeletons and success banners.
  - Each view recorded JS errors, failed, slow and pending requests, horizontal overflow, unlabelled controls, emoji and tap-target size.
- **axe-core 4.10.** The accessibility scan covered 18 pages in both themes, testing WCAG 2.1 A/AA plus best practices.
- **Lighthouse 12.2.** Run in mobile mode with default throttling on Home, Category, Product, Checkout (as a shopper) and Admin orders (as an admin).
- **pip-audit.** Run against the 131 packages installed in `.venv`. There is no `npm audit`, because the project has no npm dependencies. The only third-party script is Chart.js 4.4.1, loaded from jsDelivr on two admin pages.
- **Security probes against the running API.**
  - Response headers and cookie flags.
  - Anonymous access and role escalation.
  - IDOR as a shopper, seller and rider.
  - Client-sent prices.
  - Login lockout and error messages.
  - Signup rate limiting.
  - Error leakage.
  - Logout invalidation and CORS.
- **Code greps.** HTML sinks (95 `innerHTML`-type assignments), `|safe` and `dangerouslySetInnerHTML` (0), hard-coded secrets (0), and SQL built with f-strings (26 sites, each read by hand).
- **Database checks.** Reconciliation chain, commission precedence, realism distributions, timezone of stored timestamps.

### Scores today

**Lighthouse** (mobile):

| Page | Performance | Accessibility | Best practices | SEO | LCP | CLS |
|---|---|---|---|---|---|---|
| Home | 99 | 94 | 100 | 100 | 2.0 s | 0 |
| Category | 96 | 94 | 100 | 100 | 2.6 s | 0 |
| Product | 95 | 95 | 100 | 100 | 2.9 s | 0.019 |
| Checkout | 99 | 92 | 100 | 100 | 1.8 s | 0.027 |
| Admin orders | 98 | 100 | 100 | 50 (`noindex`, intended) | 1.7 s | 0.082 |

**axe** found serious or critical issues on 15 of 36 page/theme combinations. They are listed below as A-xx items.

**pip-audit** found no known vulnerabilities.

### What already holds (verified, no action needed)

- **Server-side authorization.**
  - Every protected API returns 401 when logged out and 403 for the wrong role. 7 admin, seller and rider endpoints were tried with a shopper token.
  - A shopper cannot read, cancel, return or pay another shopper's order.
  - A seller cannot edit another store's product (404) or act on its parcels. Bulk actions reject foreign parcels one by one with "not your parcel".
  - A rider cannot pick up or deliver a parcel that isn't theirs.
- **Prices.** Prices come from the database at checkout. The client sends only product IDs and quantities.
- **Login.** "Wrong email or password" is the same message for an unknown email and for a wrong password. 5 failed attempts give a 429 lockout, including for unknown emails, so the lockout doesn't reveal which emails exist.
- **Sessions.**
  - The token is random, only its SHA-256 hash is stored, and the cookie is `HttpOnly; SameSite=Lax`.
  - Logout deletes the session on the server: the old cookie returns 401 afterwards.
  - Passwords are hashed with scrypt.
- **SQL.** All values are bound parameters. The f-string sites interpolate only constants, whitelisted column names or `int()` casts.
- **Error responses.** No stack traces or SQL appear in responses. Validation errors return 422 with field details.
- **Secrets.** `.env` is not tracked by git, and no secrets were found in the repo.
- **Money.** Every amount is stored as `DECIMAL(…,2)`, and one helper, `money()` in `common.js`, formats all prices.
  - Delivered line totals equal seller net plus commission, both Rs 825,494,456.64.
  - Payouts equal the ledger rows marked paid.
  - All 28,628 commission entries match the seller > category > default precedence that applied at delivery.
  - The only order-total mismatch and the only payment-amount mismatch are the deliberately injected bad-data demo rows.
- **Gold layer.** `dbt build` passes (103 PASS, 1 intended WARN).

## Findings

Severity: **critical** = security hole or wrong money; **high** = visibly broken or untrustworthy; **medium** = clear gap from the brief; **low** = polish.
Rows marked **FIXED** are the brief's known issues that were already resolved in earlier commits and re-verified in this audit.

| ID | Page/route | Category | Severity | Problem | Fix plan |
|---|---|---|---|---|---|
| K-01 | Storefront (all) | Data realism | high | Product images didn't match product names; hero cards had empty badge boxes. | **FIXED** (50a73d6, f21e465): 108 hand-reviewed photos, galleries matched by product type, hero cards show photo + price. Remaining gaps are R-03/R-04. |
| K-02 | Storefront, admin | Data realism | high | Faker-style names ("Hall Eco Textbook", "Rice Ultra RC Car"). | **FIXED** (f21e465): invented brands plus type-matched names, e.g. "Velo Sport Lite Trail Running Shoes". 2 legacy customer logins keep pre-switch names (R-07). |
| K-03 | All | Data realism | high | Mixed geography and currency: Berlin/Dubai/Toronto stores, USD prices, Pakistan only as the signup default. | **FIXED** (367e85e): one market, Pakistan and PKR. Every customer, store and rider is in a Pakistani city with a +92 number. History was converted with totals re-derived, and a dbt seed restates old SCD2 prices. |
| K-04 | Admin, analytics | Data realism | high | Unrealistic numbers: riders doing 200+ deliveries a day, order totals clustered around $170. | **FIXED** for history (367e85e): 45 riders, 11–16 deliveries per rider per day, prep median 22 min, rides median 38 min, long-tail totals (p10 Rs 3.7k, median Rs 30k, p99 Rs 438k). Today's simulator volume is still unrealistic (R-01). |
| K-05 | All | Visual inconsistency | high | Emoji used as icons. | **FIXED** (50a73d6): 0 emoji in any page (crawl check). Lucide sprite only. |
| K-06 | Admin | Visual inconsistency | high | Two stacked nav bars in admin. | **FIXED** (367e85e, 50a73d6): one collapsible sidebar plus a slim top bar. Live ops and analytics use the same shell. |
| K-07 | Storefront, login | Visual inconsistency | medium | Heavy purple gradients. | **FIXED**: the storefront hero is a flat tint, and the login and payment pages are neutral. The logo mark keeps its gradient (it's the logo). |
| K-08 | `/login` | UX | high | Role-chooser screen before the store; overlong shopper signup form. | **Partly fixed**: `/` opens the store directly and the header "Sign in" skips the chooser. Still open: `/login` without `?as=` shows the chooser, and shopper signup asks 7 fields. → U-01, U-02. |
| V-01 | All surfaces | Visual inconsistency | high | Three stylesheets (`store.css`, `admin.css`, `style.css`) each implement their own button, status pill, KPI card, table, toast and switch (`.k-btn` / `.btn` / `button`; `.status` / `.pill` / `.badge`). "One implementation per component" is not true yet. | Phase 2: one `tokens.css` plus one `components.css` used by every surface. Retire the duplicate classes and port every page to the shared components. |
| V-02 | All CSS | Visual inconsistency | high | Raw values outside tokens: 36 hex colours, 10 `rgba()`, 92 raw `font-size: …px` (54 in admin.css, 33 in style.css), and 18 inline style attributes with raw colours or sizes. | Phase 2: replace every value with a token, and add a `tools/check_design_tokens.py` grep check that fails on raw hex/px outside the token file. |
| V-03 | All | Visual inconsistency | medium | Type scale is 12/14/16/20/24/32/40, not the specified 12/14/16/18/20/24/30/36/48. Some weights are 800. | Phase 2: adopt the specified scale and weights 400/500/600/700 only. |
| V-04 | All | Visual inconsistency | medium | About 15 different corner radii (2–13 px). Admin buttons are 6 px, portal cards 10 px, store cards 12 px. | Phase 2: radius 8 for inputs and buttons, 12 for cards, 16 for modals, nothing else. |
| V-05 | All | Visual inconsistency | medium | Status colours group statuses (blue = in progress, purple = moving) instead of the specified palette: pending gray, confirmed blue, packing amber, ready teal, assigned indigo, picked up violet, delivered green, cancelled red, returned orange. | Phase 2: one status token set and one StatusPill component used on all five surfaces and the Live ops chart. |
| V-06 | All | Visual inconsistency | medium | The sale colour is #C8361A (light) and #FF7A5C (dark), not #E8462E. White text on #FF7A5C fails contrast (A-01). | Phase 2: sale token #E8462E with a darker text-safe shade for small text, and a dark-mode badge that passes 4.5:1. |
| V-07 | All | Visual inconsistency | low | Shadows are defined ad hoc (hover shadow, drawer shadow, toast shadow). | Phase 2: `--shadow-sm/md/lg` only. |
| B-01 | Any unknown URL | Broken/incomplete | high | Unknown pages return raw JSON `{"detail":"Not Found"}`, and server errors return plain "Internal Server Error". There is no branded 404 or 500 page. | Phase 3: branded 404 and 500 pages for HTML requests. APIs keep their JSON. |
| B-02 | `/static/design.html` | Broken/incomplete | medium | The styleguide is a public static file, shows only part of the system and isn't admin-only. | Phase 2: `/admin/styleguide` behind admin auth, showing every token and component. Remove the public file. |
| B-03 | Storefront | Broken/incomplete | medium | No cart drawer (cart is a page only), checkout is one page rather than 3 steps, no image zoom, and order tracking has no rider map. | Phase 3, as specified. |
| B-04 | Storefront `#/account` | Broken/incomplete | medium | Account has only phone and city. No addresses, returns list or order filters. | Phase 3. Addresses need a new table, see "Approvals needed". |
| B-05 | Auth | Broken/incomplete | high | No sign-in modal, forgot-password/OTP flow, `/sell` or `/ride` landing pages, or onboarding wizards. "Continue with Google" needs OAuth credentials. | Phase 3, Auth brief. OTP and reset tokens need a table. Google needs your OAuth client ID; otherwise the button is left out rather than shipped broken. |
| B-06 | `/vendor` | Broken/incomplete | medium | Seller center has no store-settings page (name, tagline, logo, banner, pickup address, payout details) and no product deletion. | Phase 3. Payout and bank fields need columns, see "Approvals needed". |
| B-07 | `/rider` | Broken/incomplete | medium | Rider app has no week earnings, no pickup/drop navigation links, and the Go online control is a button rather than a large toggle. | Phase 3: maps deep links for pickup and drop, week chart, big toggle. |
| B-08 | Admin | Broken/incomplete | medium | "Act as vendor/rider" actions aren't recorded anywhere (who, what, when). | Phase 4: `admin_audit_log` table plus middleware hook, shown in the order drawer timeline. Needs a schema change. |
| U-01 | `/login` | UX | high | Bare `/login` still shows the 3-way role chooser. Shoppers should never have to pick a role. | Phase 3: shopper sign-in becomes a modal on the store. `/login` redirects to the store and opens it. Seller and rider entry move to `/sell` and `/ride`. |
| U-02 | Shopper signup | UX | medium | 7 fields: first name, last name, email, password, city, country, phone. | Phase 3: full name, email or phone (+92 mask), password only. City and address are asked at first checkout. |
| U-03 | Admin orders | UX | low | The orders table is capped at 500 rows client-side, so older orders can't be reached for the selected statuses. | Phase 3: server-side pagination and search. |
| A-01 | Storefront (dark) | Accessibility | high | Sale badge: white on #FF7A5C is 2.56:1 (axe color-contrast, 17 nodes on home). | Phase 2 colour tokens (V-06). |
| A-02 | Storefront orders, cart (light + dark) | Accessibility | medium | Status chips fail contrast: green on green-tint 4.27:1 (light), red on dark tint 2.57:1, and the red "Remove" ghost button on dark 2.71:1. | Phase 2: status tokens tested to ≥ 4.5:1 in both themes. |
| A-03 | Admin (dark) | Accessibility | medium | Avatar initials: white on #9D8CFF is 2.75:1. | Phase 2: dark text on light-brand backgrounds, or a darker brand shade for avatars. |
| A-04 | Checkout, rider | Accessibility | medium | Muted text on a selected payment option is 4.21:1. Rider "online" subtitle is 3.85:1. Kanban empty text is 4.39:1. | Phase 2: text tokens tested against every surface tint. |
| A-05 | Storefront header (mobile) | Accessibility | high | On phones the "Sign in" link and the account button have no accessible name: the label span is hidden and the icon is `aria-hidden` (Lighthouse link-name and button-name). | Add `aria-label` to the header actions. |
| A-06 | Storefront cards, hero | Accessibility | low | The accessible name doesn't contain the visible text: "Add to cart" button labelled "Add <product> to cart", hero tile labelled with name and price (label-content-name-mismatch, 39 on home). | Visible text first: `aria-label="Add to cart: <product>"`; hero name = visible caption. |
| A-07 | Storefront | Accessibility | low | Heading order skips (footer `<h4>` after `<h2>`). | Footer headings become `<h2 class="sf-h">`. |
| A-08 | `/vendor` | Accessibility | high | Autopilot switch has no label (axe label). The "View your storefront" link is distinguished by colour only (1.26:1 against surrounding text). | Label the switch; underline links inside text. |
| A-09 | Live ops, analytics | Accessibility | medium | Scrollable regions (`main`, wide tables) can't be reached by keyboard (axe scrollable-region-focusable). | `tabindex="0"` plus a label on scroll containers. |
| A-10 | All | Accessibility | medium | Full keyboard pass and visible focus on every interactive element not yet verified end-to-end. | Phase 6 keyboard test script. |
| P-01 | Category, product | Performance | medium | Mobile LCP is 2.6 s and 2.9 s (target ≤ 2.5 s). The product page loads the 960 px image for a phone-width gallery, and card images are served larger than displayed. | Correct `sizes` on gallery and cards, preload the LCP image, give the first row of cards `fetchpriority`. |
| P-02 | Admin orders | Performance | low | CLS 0.082: KPI skeleton height differs from the loaded cards. | Fixed-height KPI skeletons. |
| P-03 | Admin (load) | Performance | medium | Under combined load (autopilot, simulator and pipeline) some admin APIs take 2–4 s. The same queries run in under 0.2 s alone. Readers wait on writers under READ COMMITTED. | Enable READ_COMMITTED_SNAPSHOT on ShopDB (DB setting, needs approval), then re-measure. |
| P-04 | Live ops, analytics | Performance | low | Chart.js loaded from a CDN: an extra origin, and it blocks a strict CSP. | Self-host `chart.umd.min.js` (pinned version) under `/static/vendor/`. |
| P-05 | Static | Performance | low | Font and product images are cached for 7 days (Lighthouse prefers a year). | `immutable` one-year cache for fonts and product photos; photo names already change with content. |
| S-01 | All responses | Security | high | No security headers: CSP, X-Content-Type-Options, Referrer-Policy, frame-ancestors / X-Frame-Options, Permissions-Policy. No HSTS for production. The `Server: uvicorn` header is exposed. | Phase 4: header middleware with a strict CSP (self only, after P-04 and removal of inline handlers), nosniff, Referrer-Policy strict-origin-when-cross-origin, frame-ancestors 'none', HSTS when HTTPS is enabled. |
| S-02 | `/docs`, `/redoc`, `/openapi.json` | Security | high | The API docs and full schema are public (200 when logged out). The brief requires admin-only. | Phase 4: serve docs only to admin sessions; the default routes return 404. |
| S-03 | All state-changing APIs | Security | medium | No CSRF token. Today's protection is `SameSite=Lax` plus JSON bodies; body-less POSTs (cancel, start, pickup) rely on SameSite alone. | Phase 4: Origin/Referer check plus a required custom header on unsafe methods, and a CSRF token for forms. |
| S-04 | Session cookie | Security | medium | No `Secure` flag (fine on local http, required in production). Fixed 7-day sessions with no rotation or idle timeout. | Phase 4: `Secure` when HTTPS or `ENV=production`; rotate the token on login; idle timeout plus sliding refresh. |
| S-05 | Signup, vendor/rider registration | Security | medium | No rate limit: 12 rapid signups were all processed. | Phase 4: per-IP and per-email limits on signup, OTP and reset. |
| S-06 | Product photo upload | Security | medium | Files are checked by magic bytes, capped at 3 MB and given random names, but they're stored as received inside the web root (`/static/uploads`) and not re-encoded. Combined with S-01's missing nosniff, a crafted polyglot file is possible. | Phase 4: re-encode with Pillow to WebP, strip metadata, store outside `static/`, serve through a route with fixed `Content-Type` and nosniff. |
| S-07 | Admin | Security | medium | No audit trail for admin acting as a vendor or rider, for commission changes or for payouts. | Phase 4 with B-08. |
| S-08 | Passwords | Security | low | scrypt is used. The brief names argon2id or bcrypt. scrypt is memory-hard and acceptable, but parameters aren't recorded per hash. | Phase 4: store algorithm and parameters with each hash and upgrade on next login (to argon2id if you prefer). |
| S-09 | Chart.js CDN | Security | low | Third-party script without Subresource Integrity. | Self-host (P-04). |
| S-10 | Frontend | Security | low | 95 HTML-string sinks. All untrusted text goes through `esc()` (independent review found no unescaped data), but it relies on discipline. | Phase 4: lint check that flags template interpolations without `esc()` in `innerHTML` assignments; the CSP from S-01 as a backstop. |
| D-01 | All timestamps | Data accuracy | high | Times are stored in the machine's local time, US Pacific (UTC−8): `SYSDATETIME()` and `datetime.now()`. They are neither UTC nor Pakistan time. "Today" KPIs, evening-peak charts and relative times are all computed on a Pacific day. | Phase 5: store UTC (`SYSUTCDATETIME()` / `datetime.now(timezone.utc)`), convert existing timestamps (+8 h), display in Asia/Karachi with the exact time on hover, and restate SCD2/bronze history in dbt the same way as the currency switch. Data migration, needs approval. |
| D-02 | All surfaces | Data accuracy | medium | Two status vocabularies: order (6 values) and shipment (9 values), with labels decided per page. | Phase 5: one shared status enum module (Python + JS generated from it) with labels and colours. |
| D-03 | Money | Data accuracy | low | Reconciliation is verified by ad-hoc queries, not automated tests. | Phase 5: pytest reconciliation for 100 random orders (cart = checkout = order = seller net + commission = finance) and a commission-precedence test. |
| D-04 | Dates | Data accuracy | low | Date formats vary by page ("Nov 22, 06:29 AM", ISO dates, "2h ago" without hover). | Phase 5: one `formatDate` / `formatRelative` helper with a `title` tooltip. |
| R-01 | Today's live data | Data realism | high | The simulator ran fast today: 763 orders and 1,430 deliveries today against about 50/day in history. KPI deltas show +236%, and riders show 30+ deliveries today. | Phase 5: cap the simulator at realistic peak rates (about 1–3 orders/min in the evening, PKT), and make today's volume follow the historical curve. |
| R-02 | Reviews | Data realism | medium | Only 9 distinct review texts across 1,986 reviews. | Phase 5: about 150 review texts written per product type, with star-appropriate tone and varied length. |
| R-03 | Product photos | Data realism | medium | 49 products have no photo (dress, jacket, moisturizer, serum, sunglasses, RC car) and 130 have fewer than 3. The brief asks for 3–5. | Phase 5: more sourcing and review for weak types. Products without 3 good photos keep the category icon; no wrong photos. |
| R-04 | Catalog prices | Data realism | medium | Prices are US levels × 280 (a paperback at Rs 6,199, an order median of Rs 30k), which is high for Pakistan. | Phase 5: reprice the catalog by local price bands. Past orders keep the prices they were sold at (normal price history; SCD2 handles it). |
| R-05 | Orders | Data realism | low | Return rate is 6.5% (target 2–6%); cancellations are 7.1%. | Phase 5: tune the simulator and seed to about 4% returns and 5% cancellations for new data. |
| R-06 | Stores | Data realism | low | Store names are generic English ("Gadget Galaxy", "FreshMart") with no local flavour. | Phase 5 (optional): local-feeling names, e.g. "Saddar Electronics", "Liberty Threads". |
| R-07 | Logins | Data realism | low | 2 customer accounts created before the market switch keep their old names. One is the demo shopper ("Stacy Mccall"). | Phase 5: rename the demo shopper to a local name (login email unchanged). |

## Approvals needed (schema, data or environment changes)

These are the items above that change the schema, existing data or the environment. I won't start them without your OK:

1. **New tables:**
   - `customer_addresses` (B-04)
   - `admin_audit_log` (B-08 / S-07)
   - `auth_tokens` for OTP and password reset (B-05)
   - store-settings and payout columns on `vendors`: logo, banner, pickup address, bank name, account title, masked IBAN (B-06)
   - rider onboarding fields: plate number, zone, document photo path (Auth brief)

   All are additive (new tables or nullable columns), and the pipeline, bronze, silver and dbt get the matching entries.
2. **UTC migration (D-01):** convert every stored timestamp from Pacific to UTC and switch the app to UTC; dbt restates history.
3. **READ_COMMITTED_SNAPSHOT on ShopDB (P-03):** a database setting that briefly needs exclusive access, so it would interrupt your running app and Dagster for a moment.
4. **Catalog repricing (R-04):** changes current prices only, not past orders.
5. **Google sign-in (B-05):** needs a Google OAuth client ID. Without one I'll leave the button out.

## Evidence

Screenshots for every page, role, viewport and theme (desktop 1440 px light and dark, mobile 390 px), the axe JSON, the Lighthouse JSON reports and the probe output are in the session scratchpad. They aren't committed, because they are regenerated each run.

## Status after the one-pass fixes (2026-11-22)

Fixed and verified in the running app:
- **Security:** S-01 security headers (HSTS only over HTTPS), S-02 admin-only API docs, S-03 Origin-based CSRF block, S-04 `Secure` cookie in production, S-05 signup rate limit.
- **Error pages:** B-01, branded 404 and 500 pages.
- **Accessibility:**
  - A-01: the sale badge now has a text-safe background in both themes.
  - A-02 and A-04: status chips and the danger button use the contrast-checked status palette; also fixed muted text on the payment option, the rider subtitle and kanban empty text.
  - A-03: avatar text on brand-coloured backgrounds.
  - A-05: the mobile header's Sign in link and account button have names.
  - A-06: product button labels start with the visible text; hero tiles take their name from the caption.
  - A-07: footer headings follow the heading order.
  - A-08: the seller autopilot switch has a label, and links inside text are underlined.
  - A-09: scrollable tables and the console's main area can be reached by keyboard.
- **Realism:** R-07, the demo shopper is now "Ayesha Khan" (login email unchanged).

Not done in this pass (limited budget). These still need work or approval:
- **Design system** (Phase 2): V-01 to V-07, B-02.
- **New surfaces and flows** (Phase 3): B-03 to B-07, U-01 to U-03.
- **Remaining security items:** S-06, S-07/B-08, S-08 to S-10.
- **Data:** D-01 (UTC migration), D-02 to D-04.
- **Realism and performance:** R-01 to R-06, P-01 to P-05.
- **QA:** the Phase 6 test suite.

---

# Final frontend audit report (2026-11-25)

Scope covered: every page of the four roles, checked through a real headless browser and against the database. The scripts are in [tests/ui/](tests/ui/README.md). Every number below comes from a run on 2026-11-25 against the local app, unless the line names another date.

## 1. Overall frontend assessment

The app works as a demo marketplace and holds together across all four roles. The full order lifecycle runs through the UI with no JS errors, and money shown on screen matches the database. Layout holds from 360px to 1920px. **It is not yet production-ready:**
- it has never been deployed or load-tested
- the design system is split across three stylesheets
- several flows a real marketplace needs do not exist yet: password reset, seller onboarding wizard, address book, tracking map

Rating: a solid working prototype, not yet a product that competes with the big marketplaces on polish.

## 2. Pages and components inspected

- **Storefront:** home, category, product, search, cart, checkout, orders list, order tracking, account, 404.
- **Sign-in:** shopper/seller/rider login and admin login (separate path).
- **Seller center:** queue tabs, bulk select, autopilot switch, products, earnings.
- **Rider app.**
- **Admin console:** orders and drawer, live shipments kanban, catalog, finance, payouts, riders, command palette, Live ops dashboard, BI analytics.
- **Shared:** header, footer, theme toggle, toasts, skeletons, empty, error and offline states, branded 404/500 pages.

## 3. UI/UX problems discovered

| Sev | Problem | Status |
|---|---|---|
| MEDIUM | BI page: the tab bar stuck at the wrong place; long amounts broke the bar cells; wide charts were squeezed into half-width cards | Fixed |
| MEDIUM | Long or unbroken names (products, sellers, customers) widened the page and pushed columns off screen on hero, product, seller and admin pages | Fixed: global wrapping plus `min-width: 0` on grid children |
| MEDIUM | Product cards: Add-to-cart buttons sat at different heights in the same row | Fixed |
| MEDIUM | Storefront footer misaligned (side padding lost); the summary block leaned to one side | Fixed |
| LOW | Review titles rendered as oversized headings after a heading-order fix | Fixed (`h3.rv-t`) |
| LOW | Admin numeric column headers were left-aligned | Fixed |
| LOW | Seller bulk-select checkboxes were 16px on phones | Fixed (24px) |
| MEDIUM | Three stylesheets (`style.css`, `store.css`, `admin.css`) on top of `ds.css`, with raw colour and spacing values left in places (V-01 to V-07) | **Open** |
| MEDIUM | Missing flows: password reset, sign-in modal, /sell and /ride onboarding wizards, cart drawer, 3-step checkout, saved addresses, tracking map, seller settings | **Open** |

## 4. Improvements implemented

- **Design tokens:** status colour palette (`--st-*`) and light/dark themes from `ds.css`.
- **States:** skeletons at boot, empty states, offline/error state with "Try again", branded 404/500 pages.
- **Wrapping:** long text wraps at every level.
- **Admin:**
  - shell shared by every admin page
  - sticky BI tab bar
  - readable log-scale chart ticks
- **Assets:** versioned with immutable caching, so a deploy never serves stale CSS or JS.
- **Brand:** renamed Kartora to Raunaqi everywhere, data included. Money is shown in Rs (PKR).

## 5. Functional issues discovered and fixed

| Sev | Issue | Fix |
|---|---|---|
| HIGH | Live feed (SSE) stayed open on back/forward cache, so pages restored from history went stale | `liveEvents()` closes on `pagehide` and reopens on `pageshow` |
| MEDIUM | `money()` could show rounding artefacts | Rounds to paisa first |
| MEDIUM | "Show more" on listings could fire twice | Guarded |
| LOW | Recently viewed items lost on rename | Migrated from the old storage key |
| HIGH (self-inflicted) | An edit during this work broke an import in `auth.py` | Caught and fixed before commit |

## 6. Responsive testing results

**84/84** page-width combinations clean: 14 pages at 360, 390, 768, 1024, 1440 and 1920px.

What counted as a failure:
- page side-scroll
- elements past the screen edge
- clipped headings, buttons or prices
- tap targets under 24px on phones
- JS errors

The long-text run injected names over 100 characters and unbroken 80-character strings:
- No page side-scroll on store, category, product, cart, checkout, orders, seller, rider, admin orders, catalog, dashboard or analytics.
- Admin finance scrolls inside its table card, which is intended.

Honest caveat: the run just before the last fix was 77/84. Two of those failures were the checker not knowing about intended horizontal scrollers (the finance table card and the BI tab bar). One was real (the 16px checkboxes).

## 7. Performance improvements

- Lighthouse (2026-11-22): **92 to 100** across performance, accessibility, best practices and SEO on the pages measured.
- Versioned static files with a one-year immutable cache, and a log line for any request slower than 1.5s.
- **Open:** P-01 to P-05, covering image re-encoding on upload, responsive image sizes, API pagination limits and query tuning on large data.
- Not measured: real network latency. Over the Cloudflare quick tunnel, each request took about 0.5 to 1s, mostly tunnel overhead.

## 8. Accessibility findings (WCAG 2.2 AA)

Fixed (A-01 to A-09):
- contrast on sale badges, status chips, danger buttons and muted text
- accessible names on header controls, product buttons and the autopilot switch
- heading order
- underlined links in text
- keyboard access to scrollable areas
- visible focus rings
- 24px phone targets

Verified by script:
- Tab reaches the header controls in order, each with a visible focus ring.
- Ctrl+K and Esc work.
- Enter opens the order drawer, and Esc returns focus to the row.

**Open (MEDIUM):** no screen-reader pass yet (NVDA or VoiceOver), and charts have no data-table fallback for screen readers.

## 9. Security concerns

Fixed and probed:
- security headers and CSP
- admin-only API docs
- Origin-based CSRF block
- `Secure` cookie in production
- signup rate limit (5 per 10 min per IP)
- login lockout (5 failures lock an email for 5 min)
- admin console on its own path
- strong admin password (old sessions cleared)
- `pip-audit` clean
- `password_hash` never enters the data lake

Open:

| Sev | Concern |
|---|---|
| MEDIUM | Login lockout and signup limit live in memory: they reset on restart and won't hold across more than one server. There is no per-IP login limit (S-06) |
| MEDIUM | Uploaded images are stored as uploaded, not re-encoded (S-07/B-08) |
| MEDIUM | No admin audit log; needs a schema change, waiting on approval |
| LOW | Old commits with a co-author trailer are back in GitHub history after the merge. Only a force-push removes them, and that is the owner's call |

## 10. Remaining issues and limitations

| Sev | Item |
|---|---|
| CRITICAL (for launch) | Not deployed. No production database, backups, HTTPS domain or monitoring. Plan: Render plus Azure SQL |
| HIGH | The UI suites in `tests/ui` are scripts, not CI. Nothing runs them on each commit |
| HIGH | No password reset. A shopper who forgets their password is locked out |
| MEDIUM | Timestamps are stored in server local time, not UTC (D-01, needs a migration) |
| MEDIUM | Design-system consolidation and missing flows (see 3) |
| MEDIUM | Charts not accessible to screen readers |
| LOW | Demo realism: simulator volume, review variety, some products without photos, prices not localised (R-01 to R-06) |
| LOW | Test orders **#17150** (delivered, then returned), **#17151** (cancelled) and **#17152** (started, then cancelled) are in the database. The seller name "Raunaqi Demo Store" appears in BI after the next pipeline run |

## 11. Testing performed and its actual results

| Suite | Result |
|---|---|
| `journeys.py`: order, start, accept+pack, deliver, ledger, return, cancel, all via the UI and checked against SQL | 35/36 (2026-11-23). The 1 miss was timing: the autopilot rider moved the parcel before the check. A separate re-run showed the card correctly |
| `qa2.py`: 189 internal links across 5 roles, offline, keyboard | 13/13 |
| `responsive.py`: 14 pages × 6 widths | 84/84 |
| `responsive.py` with long text injected | no page side-scroll (see 6) |
| `accuracy.py`: API vs DB, page vs API, alignment | all passed |
| Lighthouse | 92 to 100 (2026-11-22) |
| Security probes and `pip-audit` | pass / clean (2026-11-22) |
| dbt | PASS=103, WARN=1 |

Not tested:
- real phones (only emulated widths)
- Safari and Firefox (only Edge/Chromium)
- load or concurrency
- screen readers
- visual-regression diffs (screenshots are saved but not compared automatically)

## 12. Final production-readiness assessment

**Ready for a private demo or pilot:** yes. Journeys, money and layout are verified.

**Not ready for real customers yet.** These must happen first:
1. Deploy with HTTPS, a managed database and backups.
2. Password reset.
3. Run the UI suites in CI.
4. UTC timestamps.
5. Persistent rate limiting (database or Redis), not in memory.

After those, finish the design-system consolidation and the missing flows to reach a competitive polish level.
