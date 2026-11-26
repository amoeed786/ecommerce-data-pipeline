# Raunaqi security audit (2026-11-25)

## Scope

- **Stack:**
  - FastAPI on uvicorn
  - SQL Server via pyodbc
  - static HTML/JS pages
  - DuckDB/Parquet analytics
  - Dagster pipeline
- **Hosting today:** bound to localhost only; the public reaches it through a Cloudflare quick tunnel.
- **Auth:** server-side sessions in an httpOnly cookie.
- **Roles:** customer, vendor (seller), rider, admin.
- **Sensitive data:**
  - customer PII (name, email, phone, city)
  - password hashes
  - seller ledger and payouts
  - payment references
- **Card data:** none stored; the sandbox gateway checks a test card and discards it.

## How it was audited

- **Code read:**
  - every route in `shop_app/main.py`
  - auth in `shop_app/auth.py`
  - every SQL statement in `shop_app/shop_db.py` and `source_app/marketplace.py`
  - payments in `source_app/payments.py`
  - the live feed in `shop_app/live.py`
  - every `innerHTML` sink in `shop_app/static/`
- **Live attacks** against a running server:
  - [tests/security/probe.py](tests/security/probe.py): 52 checks
  - [tests/security/xss_render.py](tests/security/xss_render.py): 7 pages
- **Dependencies:** `pip-audit` over all installed packages.
- **Git history:** scanned for secrets.
- **Database:** checked which privileges the app's login holds.

"Verified" below means a live test showed it. Everything else is marked as read from code or as **Needs verification**.

---

## 1. Recon: the attack surface

| Area | Routes | Auth | Notes |
|---|---|---|---|
| Pages | `/`, `/login`, `/pay/{ref}`, `ADMIN_PATH/login` | public | Static HTML; data comes from the API |
| Role pages | `/vendor`, `/rider`, `ADMIN_PATH`, `ADMIN_PATH/dashboard`, `ADMIN_PATH/analytics` | role | Redirect to sign-in if the role is wrong |
| Auth | `POST /api/auth/login`, `/register`, `/register/vendor`, `/register/rider`, `/logout`; `GET /api/auth/me` | public | Login and sign-up are rate-limited |
| Store | `GET /api/home`, `/categories`, `/config`, `/products`, `/products/{id}`, `/vendors/{id}` | public | Read-only catalog |
| Customer | `GET/PUT /api/me/profile`; `POST /api/orders`; `GET /api/me/orders[/{id}]`; `POST /api/me/orders/{id}/pay`, `/cancel`, `/return` | customer | Ownership checked on every order id |
| Payments | `GET /api/pay/{ref}`; `POST /api/pay/{ref}/sandbox`; `POST /api/webhooks/{provider}` | ref = capability; webhook = HMAC | The ref is 18 random bytes |
| Seller | `/api/vendor/summary`, `/shipments`, `/shipments/{id}/{action}`, `/shipments/bulk`, `/products`, `/products/{id}`, `/products/{id}/image`, `/earnings`, `/autopilot` | vendor | Ownership checked per shipment and product |
| Rider | `/api/rider/summary`, `/jobs`, `/status`, `/jobs/{id}/{action}` | rider | Ownership checked per shipment |
| Admin | 30 routes under `/api/admin/*`, plus `/api/stats`, `/api/analytics*`, `/docs`, `/openapi.json` | admin | Includes running pipeline steps as fixed subprocess argv lists |
| Live feed | `GET /api/events` (SSE) | any | Events filtered per user and role |

- **Dependencies:** pinned in `requirements.txt` (fastapi, uvicorn, pyodbc, SQLAlchemy, duckdb, pyarrow, dbt, dagster, Faker, python-dotenv).
- **Config:**
  - `.env`: gitignored, never committed (verified in history).
  - `.env.example`
  - `data_lake/_state/sandbox_webhook_secret`: gitignored.

## 2. Findings

| ID | Severity | Category | File:line | Description | Status |
|---|---|---|---|---|---|
| F-01 | **High** | Secrets / least privilege | [source_app/db.py:18](source_app/db.py#L18) | The app connects as a Windows login that is **sysadmin** on SQL Server, so any bug that reaches SQL owns the whole server | Code ready; **you must apply** the SQL script |
| F-02 | **High** (prod) | Default credentials | [shop_app/auth.py:35](shop_app/auth.py#L35) | Demo accounts with passwords published in the repo (`Demo@123`) would work on a public deployment | Fixed |
| F-03 | **High** (prod) | Business logic / payments | [source_app/payments.py:114](source_app/payments.py#L114) | The only gateway is the sandbox, which accepts any Luhn-valid test card, so a public deployment would ship goods for free | Fixed |
| F-04 | **Medium** | Auth / rate limiting | [shop_app/auth.py:32](shop_app/auth.py#L32) | Lockout was per email only, so one password could be sprayed across thousands of emails without limit | Fixed |
| F-05 | **Medium** | Auth / enumeration | [shop_app/auth.py:86](shop_app/auth.py#L86) | Login for an unknown email skipped scrypt: 80–110 ms faster, which revealed which emails have accounts | Fixed |
| F-06 | **Medium** | Logging | [shop_app/main.py:53](shop_app/main.py#L53) | Security events were not recorded: no failed-login, lockout, CSRF-block or 403 logging, and app loggers had no handler | Fixed |
| F-07 | **Medium** | Proxy / transport | [shop_app/serve.py:42](shop_app/serve.py#L42) | Behind the tunnel every visitor could appear as `::1`, so the sign-up limit was shared by everyone and HSTS was never sent | Fixed |
| F-08 | **Medium** | XSS defence in depth | [shop_app/main.py:145](shop_app/main.py#L145) | CSP `script-src` includes `'unsafe-inline'`, so an escaping slip would execute. No such slip found (see F-XSS) | **Open** |
| F-09 | **Medium** | Auth | n/a | No MFA for the admin account | **Open**, needs a schema change |
| F-10 | **Medium** | Business logic | [source_app/marketplace.py:27](source_app/marketplace.py#L27) | Coupons `SAVE5/10/20` are hard-coded, never expire and can be reused on every order | **Open**, product decision |
| F-11 | **Low** | Input validation | [shop_app/main.py:556](shop_app/main.py#L556) | Absurd prices or stock, over-long names and 2,200 status filters caused **500** errors (verified) | Fixed |
| F-12 | **Low** | DoS | [shop_app/main.py:66](shop_app/main.py#L66) | No request size limit: a huge JSON or multipart body was read or spooled in full | Fixed |
| F-13 | **Low** | Admin hygiene | [shop_app/main.py:1030](shop_app/main.py#L1030), [:1055](shop_app/main.py#L1055) | The admin console could start the fake-order simulator or the bad-data injector on real data | Fixed (off in production) |
| F-14 | **Low** | Sessions | [shop_app/auth.py:30](shop_app/auth.py#L30) | Admin sessions lasted 7 days; expired session rows were never purged | Fixed (12 h; purged on login) |
| F-15 | **Low** | Memory DoS | [shop_app/auth.py](shop_app/auth.py) `_recent`, [main.py](shop_app/main.py) signup limiter | Lockout and limiter dicts grew without bound under a flood of random emails or addresses | Fixed |
| F-16 | **Low** | Auth | [shop_app/auth.py:66](shop_app/auth.py#L66) | Anyone can lock a known email (including admin) for 5 minutes with 5 bad attempts | Accepted. Admin path is secret and per-address limit added |
| F-17 | **Low** | Enumeration | [shop_app/auth.py:132](shop_app/auth.py#L132) | Sign-up says "an account with this email already exists" | Accepted UX trade-off, rate-limited |
| F-18 | **Low** | Rate limits | `auth.py`, `main.py` | Limits live in memory: they reset on restart and won't hold across more than one server | **Open** (move to DB/Redis when scaling) |
| F-19 | **Low** | Uploads | [shop_app/main.py:623](shop_app/main.py#L623) | Photos are type-checked by magic bytes and stored with random names, but not re-encoded, so EXIF metadata such as GPS stays in the file | **Open** (needs Pillow) |
| F-20 | **Low** | Live feed | [shop_app/main.py:936](shop_app/main.py#L936) | An open SSE stream keeps its user after logout or disable until it reconnects; no per-user connection cap | **Open** |
| F-21 | Needs verification | Payments | [source_app/payments.py:156](source_app/payments.py#L156) | A real gateway's "succeeded" webhook for an order already cancelled for non-payment would mark it paid without a refund. Unreachable with the sandbox, which refuses non-pending orders | Check when a real gateway is added |
| F-22 | Needs verification | Transport | [source_app/db.py](source_app/db.py) | SQL traffic is encrypted but trusts the server's self-signed certificate (`TrustServerCertificate=yes`) | Set `SQL_TRUST_CERT=no` once the server has a real certificate |

### Areas checked and clean (verified live unless marked)

- **Authorization:**
  - **198** (route, wrong role) calls were all refused with 401/403.
  - The routes were enumerated from the OpenAPI spec, so no route was skipped.
  - Role checks are server-side via `Depends(require(...))`.
- **IDOR:** all refused with no data change.
  - shopper reading, cancelling or paying another shopper's order
  - seller accepting another seller's parcel (single and bulk)
  - seller repricing, or uploading a photo to, another seller's product
  - rider delivering another rider's parcel
- **SQL injection:**
  - Every statement is parameterized.
  - The dynamic parts are allowlisted (sort keys, `set_flag` table and column pairs) or `int()`-cast (`TOP`, day counts).
  - Live: `sort=price_asc; DROP TABLE x--` falls back to the default, and `q=' OR 1=1 --` returns 0 rows.
- **Command injection:** pipeline steps are fixed argv lists from a dict, with no shell and no user input. (code)
- **SSTI:** no server-side templating. (code)
- **Deserialization:** JSON only; no pickle or yaml. (code)
- **SSRF:** no user-supplied URL is fetched. (code)
- **Stored XSS:** a shopper name, a product name and a description of `<img src=x onerror=...>` were rendered as text on 7 pages:
  - store search and product page
  - seller center (two views)
  - admin users, catalog and customers

  None executed. Every user field goes through `esc()`, and toasts use `textContent`.
- **CSRF:**
  - A cross-site `Origin`, or `Origin: null`, gets 403.
  - Cookie is `HttpOnly; SameSite=Lax`, plus `Secure` when `APP_ENV=production`.
  - No CORS middleware, so the API is same-origin only.
- **Mass assignment:** `"role":"admin"` and `"is_active"` in a sign-up are ignored, and the account is created as a customer.
- **Business logic:**
  - Rejected: quantities 0, −1, 21 and 10⁹; unknown payment methods; invented coupons; negative or zero prices; negative restock.
  - Prices always come from the database at checkout.
  - Stock rows are locked (`UPDLOCK`) during checkout.
  - Cancel and return lock the order row, so double refunds are impossible.
- **Sessions:**
  - 256-bit random token; only its SHA-256 is stored.
  - New token on every login, so no session fixation.
  - A logged-out token gets 401.
  - A disabled user is cut off on the next request.
- **Passwords:** scrypt (N=2¹⁴, r=8), per-user salt, constant-time compare. Over-long passwords are refused before hashing.
- **Webhooks:** HMAC-SHA256 checked with `compare_digest`, idempotent on event id. A forged signature gets 400.
- **Errors:**
  - A generic 500 page or JSON, with the details in the log.
  - Non-numeric ids get 422 with no stack trace.
  - Database value errors now return 400.
- **Headers:**
  - `Content-Security-Policy` (with `frame-ancestors 'none'`, `object-src 'none'`, `form-action 'self'`)
  - `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`
  - HSTS over HTTPS (now also through the tunnel)
- **Exposure:**
  - `/docs` and `/openapi.json` are admin-only.
  - No directory listing on `/static/` or `/static/uploads/`; traversal attempts get 404.
  - No source maps.
  - Only the cart, wishlist, recent items and theme are in `localStorage`; no tokens.
- **Open redirect:** the `next=` parameter after login is restricted to same-origin paths (`login.html`, `admin_login.html`). (code)
- **Dependencies:** `pip-audit` found **no known vulnerabilities** in any installed package.
- **Secrets:**
  - No secrets in git history (scanned all commits).
  - `.env` never committed.
  - The admin password is not in the repo.

---

## 3. Details, in severity order

### F-01: the app runs as a SQL Server sysadmin (High)

**What is wrong:**
- `get_connection()` used Windows authentication, and that Windows login is `sysadmin` and `db_owner`.
- The app only needs to read and write rows.

**Attack:** any future SQL injection or code-execution bug would get full control of the SQL Server, not just ShopDB. That includes `xp_cmdshell`, other databases and backups.

**Fix:** `db.py` now uses a SQL login when `SQL_USER`/`SQL_PASSWORD` are set, and always encrypts the connection. [source_app/sql/app_login.sql](source_app/sql/app_login.sql) creates `raunaqi_app` with only `SELECT, INSERT, UPDATE, DELETE` and `VIEW CHANGE TRACKING` on `dbo`:

```python
user, password = os.getenv("SQL_USER"), os.getenv("SQL_PASSWORD")
auth = f"UID={user};PWD={{{password}}};" if user else "Trusted_Connection=yes;"
```

I did **not** apply it, because it creates a login and changes credentials.

**To apply:**
1. Run the script in SSMS.
2. Enable mixed-mode authentication.
3. Set `SQL_USER` and `SQL_PASSWORD` in `.env`.
4. Restart the app.

Schema scripts keep running under your admin login.

**Verify:** after switching, `SELECT IS_SRVROLEMEMBER('sysadmin')` from the app returns 0, and `CREATE TABLE x(i int)` fails with permission denied.

### F-02: demo accounts usable in production (High for a public deployment)

**What is wrong:** `customer@`, `vendor@` and `rider@raunaqi.test` all use `Demo@123`, which is written in the README and TEST_ACCOUNTS.md.

**Attack:** anyone reads the README, signs in as the demo seller and changes prices, or as the demo rider and marks parcels delivered.

**Fix:** with `APP_ENV=production`, any `@raunaqi.test` email fails like a wrong password:

```python
if os.getenv("APP_ENV") == "production" and email.endswith(DEMO_DOMAIN):
    row = None   # demo accounts have published passwords: never usable on a real deployment
```

**Behaviour change:** in production only, the demo logins stop working. Development is unchanged.

**Verify:** in-process test with `APP_ENV=production`, where the demo shopper gets 401 (passed).

### F-03: test-card payments in production (High for a public deployment)

**What is wrong:** `PAYMENT_GATEWAY` defaults to `sandbox`, the only gateway installed, which approves any Luhn-valid number.

**Attack:** a visitor pays for real orders with `4242 4242 4242 4242`, and sellers ship them.

**Fix:** in production, starting or completing a sandbox payment raises "Online payment isn't available yet - please choose cash on delivery". The order is rolled back with it. `ALLOW_SANDBOX_PAYMENTS=1` re-enables it for a closed demo.

**Behaviour change:** in production only, online methods error until a real gateway adapter exists. Cash on delivery is unaffected.

**Follow-up:** hide the online options in the store when `/api/config` reports them as off.

**Verify:** in-process tests of both paths (passed).

### F-04: password spraying (Medium)

**What is wrong:** 5 failures locked one *email*, but one address could try `Password1!` against unlimited emails.

**Fix:** 30 failures from one client address in 15 minutes lock that address (`MAX_IP_FAILS`). The per-email lock stays.

**Verify:** 30 failures across 30 emails from `203.0.113.50`, then the correct password from that address gets 429, while another address signs in fine (passed).

### F-05: login timing leaked which emails exist (Medium)

**What is wrong:** an unknown email returned without running scrypt, which was measured as 81–113 ms faster.

**Fix:** unknown emails are checked against a dummy scrypt hash:

```python
if not verify_password(password, row["password_hash"] if row else _DUMMY_HASH) or row is None:
```

**Verify:** the gap is now 5 ms, which is noise (probe check passed).

### F-06: no security logging (Medium)

**What is wrong:** failed logins, lockouts, CSRF blocks and permission denials were not recorded anywhere, and the `shop` loggers had no handler.

**Fix:**
- New `shop.security` logger, writing to the console and to a rotating `data_lake/_state/security.log` (5 × 5 MB, gitignored).
- Events: `login_failed` (with reason), `login_locked`, `login_disabled_account`, `admin_login`, `csrf_blocked`, `signup_rate_limited`, and `denied status=401/403/429` with method and path.
- Passwords are never logged.

**Verify:** the probe confirms all three event types are in the file and that no password appears.

### F-07: client address behind the tunnel (Medium)

**What is wrong:** uvicorn trusted `X-Forwarded-*` only from `127.0.0.1`, but the server also listens on `::1`. A tunnel connecting over IPv6 made every visitor look like the same `::1`:
- One visitor's 5 sign-ups blocked sign-up for everyone.
- `request.url.scheme` stayed `http`, so HSTS was never sent.

**Fix:** `proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1"`. This is safe because the server only accepts connections from this computer.

**Verify:** a request carrying `X-Forwarded-Proto: https` gets `Strict-Transport-Security`, and limits apply per forwarded address (probe passed).

**Needs verification:** confirm cloudflared sends `X-Forwarded-For` on your tunnel. Watch `ip=` in `security.log` during a real visit.

### F-08: CSP allows inline scripts (Medium, open)

Every page keeps its JavaScript in an inline `<script>`, and there are 10 inline `on…=` handlers, so the CSP needs `'unsafe-inline'`. Escaping is consistent (verified), but this removes the safety net.

**Fix plan (not applied):** it touches every page, so it needs a full UI regression.
1. Move the 10 inline handlers to `addEventListener`.
2. Have `versioned_page()` stamp a per-response nonce on each `<script>`.
3. Send that nonce in the CSP:

```python
nonce = secrets.token_urlsafe(16)
html = cached_html.replace("<script>", f'<script nonce="{nonce}">').replace("<script ", f'<script nonce="{nonce}" ')
csp = CSP.replace("script-src 'self' 'unsafe-inline'", f"script-src 'self' 'nonce-{nonce}'")
```

**Verify:** the probe check "CSP allows inline scripts" turns PASS, and the UI suites still pass.

### F-09: no admin MFA (Medium, open)

The admin account is protected only by a strong password, a secret path and rate limits. TOTP needs a `users.totp_secret` column (a schema change, which needs your approval) plus the `pyotp` package.

### F-10: unlimited coupons (Medium, open, product decision)

`SAVE20` gives 20% off every order, forever, for anyone who guesses it. Decide whether coupons stay. If they do, move them into a table with expiry, usage limits and a per-customer limit.

### F-11: bad values caused server errors (Low)

**Verified 500s before the fix:**

| Input | Before | After |
|---|---|---|
| price 1e30 | 500 | 422 |
| restock 10¹² | 500 | 422 |
| list price 1e30 | 500 | 422 |
| 5,000-character product name | 500 | 422 |
| 2,200 `status=` filters (over SQL Server's 2,100-parameter limit) | 500 | 200, deduplicated to the allowlist |

**Fix:**
- `Field(max_length=…, le=…)` on every request model, sized to the database columns.
- Status filters reduced to known values (`_statuses`).
- A `pyodbc.DataError` handler returns 400 for anything the database still rejects.

### F-12 to F-15 (Low): fixed as listed in the table

| ID | Fix |
|---|---|
| F-12 | Bodies over 5 MB get 413 (verified) |
| F-13 | Simulator and bad-data injector return 403 when `APP_ENV=production` |
| F-14 | Admin cookie and session last 12 hours (verified); expired rows are deleted at the next login |
| F-15 | Limiter dicts prune stale keys above 10,000–50,000 entries |

---

## 4. Action plan

**Today:**
1. Apply F-01: create the `raunaqi_app` login and switch `.env` to it.
2. Set `APP_ENV=production` on any public deployment, including a long-lived tunnel. That turns on F-02, F-03, F-13 and the Secure cookie.
3. Commit these fixes (done) and push.

**This week:**
1. F-08: nonce-based CSP.
2. F-10: decide on coupons.
3. Hide online payment options in the store when they are off.
4. Add a password reset flow. It is missing, and it should be rate-limited like login.

**Later:**
1. F-09: admin TOTP (schema change).
2. F-18: rate limits in the database or Redis before running more than one server.
3. F-19: re-encode uploads with Pillow.
4. F-20: SSE revalidation and a per-user connection cap.
5. F-21: late-payment refund when a real gateway is integrated.
6. Run `tests/security/probe.py` in CI.

## 5. What could not be verified from the code (check manually)

- **Network:** Windows Firewall should block 8000/8002 and 1433 from the network.
  - The app binds to localhost, which I verified in code, but SQL Server's listener settings live outside the repo.
  - Check SQL Server Configuration Manager: TCP/IP enabled or not, and which IPs it listens on.
- **SQL Server:**
  - `sa` disabled or strongly passworded.
  - Mixed mode only if you use F-01.
  - Backups exist and are not world-readable.
- **Cloudflare tunnel:** quick tunnels are public and unauthenticated. For anything beyond a demo, use a named tunnel with Cloudflare Access in front of `ADMIN_PATH`.
- **Hosting (when you deploy to Render or Azure):**
  - HTTPS-only redirect at the edge.
  - `APP_ENV=production` set.
  - Secrets in the platform's secret store, not in files.
  - Database firewall restricted to the app.
  - Automated backups.
- **`ADMIN_PATH`:** `.env` still uses `/admin`. Set a hard-to-guess path for any public deployment.
- **The machine:** disk encryption (BitLocker), since `data_lake/` holds customer PII in Parquet. The data lake excludes `password_hash`, but it does contain names, emails and phone numbers.
- **GitHub repo:** confirm it is private, enable secret scanning and Dependabot alerts, and require 2FA on your account.
