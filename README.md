# Ecommerce Data Pipeline (learning project)

An end-to-end, 100% free, local data pipeline:

```
SQL Server (ShopDB)  ->  Data lake (Parquet: bronze/silver/gold)  ->  DuckDB + dbt  ->  Dashboard
        ^                          orchestrated by Dagster
 fake shop activity (Faker)
```

## Roadmap

| Phase | What | Status |
|---|---|---|
| 1 | Source system: `ShopDB` on SQL Server + fake data generator | Done |
| 2 | Ingestion: incremental extract SQL Server -> bronze Parquet | Done |
| 3 | Silver: cleaning, dedup, data-quality checks | Done |
| ★ | ShopLite web app (FastAPI): shop, admin, live dashboard | Done |
| ★ | ShopLite marketplace: logins + 4 roles, vendors, riders, fulfillment flow, store UI, new marts | Done |
| 4 | Gold: dbt + DuckDB star schema (facts/dims), tests, docs | Done |
| 5 | Analytics: revenue, top products, CLV, RFM, cohorts | Done |
| 6 | Orchestration with Dagster | Done |
| 7 | Analytics dashboard on the gold layer (ShopLite `/analytics`; Power BI can read `data_lake/gold`) | Done |
| + | Bonus: SCD2 (dim_customer, dim_product) and revenue forecast | Done |
| + | Bonus: CDC (log-based change capture) | Not started: needs SQL Server Agent |

## Kartora: the storefront brand

The shop app is branded **Kartora** ("cart" + "aura"; tagline *Everything you love, delivered.*). The name and
logo live in one place, `BRAND` and `LOGO_SVG` in `shop_app/static/common.js`, so a rename is a one-line change.
Colors are in `style.css` (violet `--accent`, coral `--deal`).

- **Sign in:** http://localhost:8000/login. Choose Shopper, Seller or Rider first, then sign in or create an
  account. All demo logins are in [TEST_ACCOUNTS.md](TEST_ACCOUNTS.md).
- **Admin area (separate):** sign in at http://localhost:8000/admin/login. Admin pages are `/admin`,
  `/admin/dashboard` and `/admin/analytics`.
  - Nothing on the public site links to the admin area.
  - Admin accounts are refused on the public `/login`, and the admin page refuses non-admins. Both answer "wrong
    email or password", so neither page reveals the other kind of account.
  - Opening an admin page while signed out redirects to `/admin/login`.
  - **The path is configurable:** set `ADMIN_PATH` in `.env` (e.g. `ADMIN_PATH=/kartora-control-7f3a91`) and
    restart. The sign-in page becomes `<ADMIN_PATH>/login`, and `/admin` (plus the old `/dashboard` and
    `/analytics` shortcuts) returns a plain 404.
    - Public pages and scripts never contain the path. Only a signed-in admin's `/api/auth/me` includes it.
    - Invalid values (`/`, spaces, `/login`, `/api/…` and so on) stop the app at startup with a clear message.
    - A hidden path only reduces casual probing. The real protection is the admin-only sign-in, role checks on
      every admin API (`/api/admin/*` needs an admin session) and the failed-login lockout.
- **Product photos:** 37 openly licensed photos from Openverse, **reviewed by hand** (off-topic and unsuitable
  images were removed), with credits shown on each product page. Products without an approved photo show
  generated art.
- **Your own brand photos:** either click a product photo in the seller center to upload one, or do it in bulk:
  ```powershell
  # put files named by SKU (ELE-00012.jpg) or product id (12.png) in .\brand_photos\, then:
  .\.venv\Scripts\python.exe tools\import_brand_photos.py
  ```
- **Seller center:** tick several orders (or "select all"), then **⚡ Accept + pack & hand to rider** in one
  click. There are also separate Accept and Mark packed buttons, and printable **packing slips**.

## Payments, commission and seller payouts

**Online payments** go through a pluggable gateway (`source_app/payments.py`):

```
checkout (card / PayPal / bank) ─► order 'pending', payment 'pending' ─► customer sent to the gateway's page
   ─► gateway sends a SIGNED callback ─► payment 'completed' ─► order can now be started (admin / auto-approve)
   declined → customer retries from the order page · never paid → order cancelled after 30 min, stock restored
```

- The built-in **sandbox gateway** works like a real one: a hosted card page at `/pay/<session>`, test cards
  (see `TEST_ACCOUNTS.md`) and HMAC-signed callbacks.
- Callbacks are verified, stored in `payment_events`, and processed once only, so a gateway retrying the same
  event changes nothing.
- The store never stores card numbers.
- Cancellations and returns refund through the same gateway. Cash on delivery is collected by the rider at drop-off.
- **A real gateway** means one new class with `create_session`, `verify` and `refund`, then `PAYMENT_GATEWAY=<name>`
  in `.env`. Stripe doesn't serve Pakistani businesses, so use JazzCash, Easypaisa or a bank once you have merchant
  credentials.

**Commission and the seller ledger:**
- **Which rate applies:** the seller's own rate, else its category's, else the default.
  Starting rates: 10% default; Electronics 5%; Grocery 6%; Books 8%; Beauty 12%; Fashion 15%.
  Edit them in **Admin → Finance**.
- **Delivery:** each order line credits its seller **+sale** and **−commission**, with the rate in force at that
  moment saved on the entry.
- **Return window:** entries are held for `PAYOUT_HOLD_DAYS` (default 7), then become available.
  A return writes reversals, so the seller nets zero.
- **Payouts:** **Run payouts** groups each seller's available balance (minimum `PAYOUT_MIN_AMOUNT`). Send the
  transfer, then **Mark paid** with the bank reference.
- **Seller view:** sellers see all of this in **Seller center → Earnings**.
- **Past data:** `migrate_marketplace.py` backfilled the ledger for all past deliveries and settled history into
  weekly paid payouts.

**In the pipeline:**
- **Extract and silver:** `commission_rules`, `payouts` and `ledger_entries` are extracted and quality-checked.
- **dbt:** builds `fct_ledger`, `mart_platform_revenue_daily` (commission, take rate, refunds) and `mart_seller_balances`.
- **Reconciliation tests:** every delivered line is credited exactly once, and returned orders net to zero.
- **BI:** shows a **Marketplace revenue** section.

## Run everything (one command)

```powershell
.\.venv\Scripts\python.exe start.py              # shop + autopilot + automatic pipeline
.\.venv\Scripts\python.exe start.py --simulate   # ...plus fake shoppers placing orders
```

Then open http://localhost:8000. **You never have to run the pipeline yourself:**

```
new entry in ShopDB (order, status change, price change, new customer…)
  └─► Dagster sensor `shopdb_changes` notices it (checks every 30 s)
        └─► runs full_pipeline: extract → bronze → silver → dbt gold (+tests) → gold Parquet
              └─► /analytics reloads itself with the new numbers
```

- **Typical delay:** about 1–2 minutes from an entry to gold. That's the 30 s check plus about 1 minute of pipeline.
- **One run at a time:** changes made during a run are picked up by the next one. There's a 30 s cooldown
  between runs (`PIPELINE_COOLDOWN_SECONDS`) so a busy shop doesn't run the pipeline nonstop.
- **Failed steps** are retried twice. Each run is logged to `data_lake/_state/pipeline_runs.jsonl` and shown
  in **Admin → Simulator & pipeline → ⚡ Automatic pipeline**. Dagster's UI (http://localhost:3000) has the full detail.
- **Logs:** each service writes to `data_lake/_state/logs/`. Ctrl+C stops everything.
- **Don't run two Dagsters:** if you already have `dagster dev` or the app running in another terminal, stop it
  first. `start.py` refuses to start on a port that's in use.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # then edit SQL_SERVER if needed
```

## Phase 1 - Source system

```powershell
.\.venv\Scripts\python.exe source_app\setup_db.py              # create ShopDB + tables (--reset to wipe)
.\.venv\Scripts\python.exe source_app\generator.py seed        # ~1 year of history
.\.venv\Scripts\python.exe source_app\migrate_marketplace.py   # vendors, riders, shipments, logins (+ backfill)
.\.venv\Scripts\python.exe source_app\generator.py stream      # live shoppers, Ctrl+C to stop
.\.venv\Scripts\python.exe source_app\check_db.py              # row counts + integrity checks
```

**Tables:** `categories`, `customers`, `products`, `inventory`, `orders`, `order_items`, `payments`,
plus marketplace tables (`schema_marketplace.sql`): `vendors`, `riders`, `shipments`, `users`,
`user_sessions` and `app_settings`. Every table has `updated_at`, which Phase 2 uses as the
watermark for incremental loads.

**Built-in realism:** business growth, weekend and holiday peaks, evening shopping hours,
Pareto best-sellers/loyal customers, order lifecycle (pending -> paid -> shipped -> delivered,
with cancellations and returns), price changes and customer relocations (for SCD2 later).

## Phase 2 - Ingestion (bronze)

```powershell
.\.venv\Scripts\python.exe ingestion\extract.py             # incremental extract, all tables
.\.venv\Scripts\python.exe ingestion\extract.py --full      # ignore watermarks, reload everything
.\.venv\Scripts\python.exe ingestion\check_bronze.py        # reconcile bronze vs ShopDB
```

**How it works:** each table has a watermark (the newest `updated_at` already extracted),
stored in `data_lake/_state/watermarks.json`. A run pulls rows with
`watermark < updated_at <= upper bound`, where the upper bound is SQL Server's clock minus
5 seconds, so rows still being written are picked up next time. The first run is a full load.

**Layout:**

```
data_lake/
  bronze/<table>/ingest_date=YYYY-MM-DD/<table>_<batch_id>.parquet
  _state/watermarks.json        # where each table is up to
  _state/ingestion_log.jsonl    # one line per table per run (rows, timing, range)
```

- **Append-only:** when an order changes status it is written again in a later batch, so bronze
  keeps every version. Silver (Phase 3) keeps the latest version of each record.
- **Lineage columns:** `_batch_id`, `_ingested_at`, `_source_table`.
- **Explicit schema:** Parquet types come from the SQL column types, so money stays an exact
  decimal and every batch has the same schema.
- **Safe writes:** a file only gets its `.parquet` name once it's complete, and the watermark only
  moves after the file is written. If a run crashes, the next one re-extracts that range.
- **Limitation:** rows deleted in the source aren't detected, because a watermark only sees
  inserts and updates. ShopDB never deletes rows. CDC (bonus phase) would handle deletes.
- **Limitation:** a watermark sees each row as it is *at extract time*. If an order goes
  pending -> paid -> shipped between two extracts, bronze only gets `shipped`. CDC fixes this too.

## Phase 3 - Silver (clean, dedup, quality checks)

```powershell
.\.venv\Scripts\python.exe transform\silver.py              # rebuild silver from bronze
.\.venv\Scripts\python.exe source_app\inject_bad_data.py    # optional: add messy rows to see the checks fire
```

For each table, parents first, DuckDB:

1. **Dedup:** keeps the newest version of each primary key (bronze has all versions).
2. **Clean:** trims and collapses whitespace, lowercases emails/status/payment method,
   uppercases SKUs, and maps country variants (`usa`, `U.K.`, `Deutschland`) to one spelling.
3. **Check:**
   - **ERROR** rules send the row to `data_lake/quarantine/<table>.parquet` with a `_dq_reason`.
     Examples: orphan foreign keys, order dated in the future, invalid status, negative price.
     Children of a quarantined row are quarantined too, as orphans.
   - **WARN** rules keep the row but count it. Examples: invalid email, unknown country,
     order total doesn't match its items, payment amount doesn't match the order.
4. **Write:** `data_lake/silver/<table>.parquet`. Results are appended to `data_lake/_state/dq_log.jsonl`.

Silver is fully rebuilt on each run, so it's idempotent: running it twice gives the same result.

## Phase 4 - Gold layer (dbt + DuckDB star schema)

```powershell
.\.venv\Scripts\dbt.exe build --project-dir dbt_shop --profiles-dir dbt_shop   # models + 35 tests
.\.venv\Scripts\python.exe transform\export_gold.py                            # gold -> data_lake/gold/*.parquet
.\.venv\Scripts\dbt.exe docs generate --project-dir dbt_shop --profiles-dir dbt_shop
.\.venv\Scripts\dbt.exe docs serve --project-dir dbt_shop --profiles-dir dbt_shop --port 8081   # lineage + docs site
```

Run dbt from the project root. dbt reads silver Parquet directly and builds `warehouse/shop.duckdb`:

| Schema | Models | What |
|---|---|---|
| `staging` (views) | `stg_*` | One per silver table: rename and type, nothing else |
| `intermediate` | `int_product_versions` | Product history joined to its category |
| `core` (tables) | `dim_date`, `dim_customer`, `dim_product`, `fct_order_items`, `fct_orders` | **Star schema** |
| `analytics` (tables) | `mart_*` | Business answers (Phase 5) |

- **SCD Type 2:** `dim_customer` and `dim_product` keep one row per version, with `valid_from`, `valid_to`
  and `is_current`. Silver writes `*_history.parquet`, which has every captured version run through
  the same cleaning rules. The `scd2()` macro collapses versions where nothing tracked changed.
- **Point-in-time joins:** facts join the version that was valid when the order was placed, so
  `gross_margin` uses the product's cost on that day, and geography uses where the customer lived then.
- **Tests:** uniqueness, not-null, foreign keys to every dimension, accepted status values,
  no SCD2 overlaps, exactly one current version, and an order-total check
  (warn level, so it reports the deliberately broken order without failing the build).
- **History caveat:** SCD2 history is only as fine-grained as your extracts. Two price changes
  between two extracts show up as one change.

## Phase 5 - Analytics marts

| Mart | Answers |
|---|---|
| `mart_daily_sales` | Orders, revenue, margin, average order value, cancellations, 7-day moving average per day |
| `mart_product_performance` | Units, revenue, margin, return rate, overall and in-category rank, stock |
| `mart_category_monthly` | Category revenue per month, month-over-month growth, share of month |
| `mart_customer_rfm` | Recency/Frequency/Monetary scores 1–5, mapped to segments (Champions, At risk, …) |
| `mart_customer_ltv` | Lifetime spend + simple 12-month projection, value decile |
| `mart_cohort_retention` | Monthly cohorts, % who order again N months later |
| `mart_revenue_forecast` | 90 days actual + 30-day forecast (linear trend × weekday seasonality, plain SQL) |

```powershell
.\.venv\Scripts\python.exe tools\lake.py "select segment, count(*) from gold_mart_customer_rfm group by 1 order by 2 desc"
```

## Phase 6 - Orchestration (Dagster)

```powershell
$env:DAGSTER_HOME = "$PWD\.dagster_home"
.\.venv\Scripts\dagster.exe dev -m orchestration.definitions          # UI on http://localhost:3000
.\.venv\Scripts\dagster.exe job execute -m orchestration.definitions -j full_pipeline   # one run, no UI
```

`orchestration/definitions.py` turns the pipeline into an **asset graph**:
`shopdb → bronze → silver/<table> → every dbt model → gold_parquet`.

- `bronze` runs the extract. Its asset check `bronze_reconciles_with_shopdb` runs `check_bronze.py`.
- `silver` is one script producing 9 assets (one per silver file). Their keys match dbt's sources,
  so lineage connects table by table.
- Every dbt model is an asset, and every dbt test is an asset check.
- The `full_pipeline` job runs everything, retrying a failed step twice.
- **Automation:** the `shopdb_changes` sensor (on by default) starts `full_pipeline` whenever ShopDB has
  rows the lake hasn't extracted yet (see "Run everything" above). It replaced the earlier
  30-minute schedule. Two small sensors log each finished run for the shop's admin page. You can
  toggle the sensors in the UI under Automation.

## Phase 7 - Analytics dashboard

ShopLite's **`/analytics`** page reads the gold Parquet files. It shows 30-day KPIs vs the previous
30 days, the revenue forecast, RFM segments, monthly category revenue, a cohort retention heatmap,
top products and most valuable customers. It's as fresh as the last pipeline run.
**`/dashboard`** stays the second-by-second operational view, reading ShopDB directly.

**Power BI (optional):** Get data → Parquet → point at `data_lake\gold\*.parquet`.

## ShopLite marketplace (FastAPI)

```powershell
.\.venv\Scripts\uvicorn.exe shop_app.main:app --port 8000    # then open http://localhost:8000
```

**Demo logins** (also one-click buttons on the sign-in page):

| Role | Email | Password | Lands on |
|---|---|---|---|
| Customer | `customer@kartora.test` | `Demo@123` | `/` storefront |
| Vendor (you pack) | `vendor@kartora.test` | `Demo@123` | `/vendor` seller center, Kartora Demo Store |
| Vendor (autopilot) | `techhub@`, `gadget@`, `stylestreet@`, `homenest@`, … `@kartora.test` | `Demo@123` | `/vendor` |
| Rider | `rider@kartora.test` | `Demo@123` | `/rider` |
| Admin | `admin@kartora.test` | `Admin@123` | `/admin` |

Customers can also create their own account. Store and rider accounts are made by `migrate_marketplace.py`.

**How an order moves.** Each vendor's part of an order is its own **shipment** (parcel):

```
customer places order ─► pending ──(admin presses Start, or auto-approve)──► confirmed
   ─► store accepts ─► packing ─► store marks packed ─► ready
   ─► dispatcher assigns a free rider (online people first) ─► assigned
   ─► rider picks up ─► picked_up ─► rider delivers ─► delivered   (cash on delivery collected here)
order status follows its parcels: pending → processing → shipped → delivered
```

- **You're in control:** auto-approve is off by default, so every order waits for **Start** (or
  **Start all pending**) in Admin → Order control. Turn auto-approve on to let orders flow by themselves.
- **Autopilot vendors and riders** (`is_bot`) act by themselves after realistic delays. Admin →
  Vendors/Riders switches any of them between autopilot and a person. The demo vendor and rider
  are people, so their orders wait for you in `/vendor` and `/rider`.
- **Simulation speed** (admin) speeds every autopilot delay up or down, from 0.5× to 20×.
- **Admin can step in** for any store or rider: accept, pack, assign a rider, pick up, deliver,
  cancel, return.

| Page | What you can do |
|---|---|
| `/` storefront | Hero deals, categories, flash deals with countdown, best sellers, top rated, official stores; category/search pages with price, rating, sale and stock filters plus sorting; product pages; store pages; cart grouped by seller; checkout (address, payment method, coupon); **live order tracking** with a stepper per parcel; account |
| `/vendor` | New-order queue → Accept → Mark packed; KPIs; autopilot switch; products: price, sale price, restock, hide/show, add new |
| `/rider` | Go online/offline, current job (pickup → drop-off, cash to collect), Picked up / Delivered buttons, earnings, history |
| `/admin` | Order control, live shipments board, vendors, riders, users (disable accounts), catalog, simulator and pipeline |
| `/admin/dashboard`, `/admin/analytics` | Admin only: live operations, and gold-layer analytics including vendor leaderboard and delivery-time breakdown |

**Security:**
- **Passwords:** salted scrypt hashes.
- **Sessions:** a random token in an httpOnly, SameSite cookie. Only its SHA-256 is stored, and the session is deleted on logout.
- **Brute force:** an email is locked after 5 failed logins.
- **Access checks:** every API route checks the role, and every vendor/rider action checks that the shipment is theirs. A customer asking for someone else's order gets 404, so order ids can't be probed.
- **Live feed:** each user only receives events they're allowed to see.
- **Lake:** `password_hash` is excluded at extraction, so it never reaches the data lake.

It's still a local demo with no HTTPS or email verification, so don't put it on the internet as-is.

**Code:**
- [source_app/marketplace.py](source_app/marketplace.py): every order-flow rule, plus the autopilot
  `engine_tick()`. It's shared by the app, the simulator, and `python source_app/fulfillment.py` (runs
  the autopilot without the app).
- `shop_app/auth.py`: logins and roles.
- `shop_app/shop_db.py`: SQL per screen.
- `shop_app/live.py`: the SSE feed.
- `shop_app/main.py`: routes and background tasks.
- `static/`: the pages.

**Pipeline:** the marketplace flows through the whole pipeline:
- **Extract:** pulls `vendors`, `riders`, `shipments` and `users`.
- **Silver:** checks them, for example orphan parcels, unknown riders, and steps timestamped out of order.
- **dbt:** adds `dim_vendor`, `dim_rider` and `fct_shipments` (minutes per fulfillment stage), plus
  `mart_vendor_performance` and `mart_delivery_daily`.
- **Dagster:** the graph now has 47 assets and 51 checks.

## Command cheat sheet

All commands are run from the project folder: `cd C:\Projects\ecommerce-data-pipeline`

| Step | Command | What it does |
|---|---|---|
| **Run all** | `.\.venv\Scripts\python.exe start.py [--simulate]` | **Shop + autopilot + automatic pipeline, one command** |
| Setup | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` | Install libraries |
| Source | `.\.venv\Scripts\python.exe source_app\setup_db.py` | Create ShopDB + tables (`--reset` wipes) |
| Source | `.\.venv\Scripts\python.exe source_app\generator.py seed` | One year of fake history |
| Source | `.\.venv\Scripts\python.exe source_app\migrate_marketplace.py` | Marketplace tables, vendors, riders, logins, backfill (safe to re-run) |
| Source | `.\.venv\Scripts\python.exe source_app\generator.py stream` | Live shoppers placing marketplace orders (Ctrl+C stops) |
| Source | `.\.venv\Scripts\python.exe source_app\fulfillment.py` | Autopilot vendors/riders without the web app (the app runs it already) |
| Source | `.\.venv\Scripts\python.exe source_app\inject_bad_data.py` | Add messy rows for silver to catch |
| Source | `.\.venv\Scripts\python.exe source_app\check_db.py` | Row counts + integrity checks |
| Bronze | `.\.venv\Scripts\python.exe ingestion\extract.py` | Incremental extract (`--full` reloads) |
| Bronze | `.\.venv\Scripts\python.exe ingestion\check_bronze.py` | Reconcile bronze vs ShopDB |
| Silver | `.\.venv\Scripts\python.exe transform\silver.py` | Dedup + clean + quality checks |
| Gold | `.\.venv\Scripts\dbt.exe build --project-dir dbt_shop --profiles-dir dbt_shop` | Star schema + marts + tests |
| Gold | `.\.venv\Scripts\python.exe transform\export_gold.py` | Gold -> `data_lake/gold/*.parquet` |
| Docs | `.\.venv\Scripts\dbt.exe docs generate --project-dir dbt_shop --profiles-dir dbt_shop` then `... docs serve --port 8081` | Model docs + lineage graph |
| Orchestrate | `$env:DAGSTER_HOME="$PWD\.dagster_home"; .\.venv\Scripts\dagster.exe dev -m orchestration.definitions` | Dagster UI on http://localhost:3000 |
| App | `.\.venv\Scripts\uvicorn.exe shop_app.main:app --port 8000` | ShopLite on http://localhost:8000 (Ctrl+C stops) |
| Explore | `.\.venv\Scripts\python.exe -c "import duckdb; duckdb.sql(\"select * from 'data_lake/silver/orders.parquet' limit 5\").show()"` | Query any Parquet file with SQL |
