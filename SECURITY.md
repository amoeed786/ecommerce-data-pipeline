# Raunaqi - security controls

> Updated 2026-11-25: the full audit, every finding and what is still open are in [SECURITY_AUDIT.md](SECURITY_AUDIT.md). Since then: admin sessions last 12 hours, there is a per-address failed-login limit (30 per 15 min), login timing no longer reveals which emails exist, security events are logged to `data_lake/_state/security.log`, and `APP_ENV=production` blocks the demo accounts and test-card payments. Live probes: `tests/security/probe.py`.

Each control below was tested against the running app on 2026-11-22. The tests are scripted HTTP probes (`secprobe`) plus browser runs.

| Control | Implementation | How it was tested |
|---|---|---|
| Password storage | scrypt with a per-user salt (`source_app/passwords.py`). Passwords are never logged or returned. | Code review. Login responses contain no hash. |
| Sessions | A random 256-bit token is set as an `HttpOnly; SameSite=Lax` cookie, plus `Secure` when `APP_ENV=production`. The database stores only the SHA-256 of the token. Sessions expire after 7 days (admin: 12 hours). | Inspected `Set-Cookie`. After logout, the old cookie gets 401, so the session is deleted on the server. |
| Login brute force | 5 failed attempts lock the email for 5 minutes, with the same message for unknown emails and wrong passwords. | 7 bad logins returned 401 ×5 and then 429. An unknown email and a wrong password got identical 401 messages. |
| Signup abuse | At most 5 registrations per client address per 10 minutes. | 7 rapid signups: the rest returned 429. |
| Authorization | Every API declares its role (`Depends(require(...))`) and data access is scoped to the owner on the server. | Logged out: 401 on 7 protected endpoints. Shopper token on admin, seller and rider endpoints: 403. IDOR attempts all rejected: a shopper on another shopper's order (404/400), a seller on another store's product (404) and parcels ("not your parcel"), a rider on an unassigned parcel ("not assigned to you"). |
| Prices and money | Prices, discounts, totals, commission and payouts are computed on the server. The client sends only product IDs and quantities. | An order containing a fake `unit_price` and an unknown product was rejected. Reconciliation queries: line totals = seller net + commission, and payouts = paid ledger rows. |
| CSRF | State-changing requests whose `Origin` belongs to another site are rejected with 403 (gateway webhooks are exempt). `SameSite=Lax` cookies are the second layer. | A PUT with `Origin: https://evil.example` returned 403. The same request from the site's own origin returned 200. |
| Security headers | CSP (`default-src 'self'`, scripts limited to self plus pinned jsDelivr, `object-src 'none'`, `frame-ancestors 'none'`, `form-action 'self'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY` and `Permissions-Policy` on every response. HSTS is sent when served over HTTPS. | Response header probe. Storefront, admin, live ops (Chart.js), analytics, seller center and API docs all load with no JS errors under the CSP. |
| API docs | `/docs` and `/openapi.json` are admin-only, and `/redoc` is removed. | Logged out: 401. Admin: 200. |
| Output encoding | All untrusted text goes through `esc()` before HTML insertion. There is no `|safe` or `dangerouslySetInnerHTML`. | Grep plus an independent code review of the 95 HTML-string sinks. |
| SQL injection | Every value is a bound parameter. The f-string SQL sites interpolate only constants, whitelisted column names or `int()` casts. | Each of the 26 sites read by hand. |
| File uploads | The type is decided by the file's bytes, not its name or claimed type (JPG/PNG/WebP only), with a 3 MB cap, random file names, and nosniff on every response. | Code review. |
| Errors | Branded 404 and 500 pages for browsers. JSON APIs return `{"detail": ...}` and never a stack trace or SQL; details go to the server log only. | `/nope-page` returns the branded 404 and `/api/nope` returns JSON 404. |
| Secrets | `.env` is not tracked by git. The webhook secret is generated per install and stored outside git. | `git ls-files`, plus a secret-pattern grep (0 hits). |
| Dependencies | pip-audit over all 131 installed packages: no known vulnerabilities. There are no npm dependencies. | `pip-audit -r frozen.txt`. |

## Still open (see AUDIT.md)

- Uploaded photos are not yet re-encoded and are stored inside the web folder (S-06).
- There is no admin audit log for act-as actions; it needs a new table (S-07, B-08).
- No idle timeout or token rotation (S-04).
- The CSP still allows inline scripts, because the pages use inline handlers. Moving them to external files would allow a nonce-based CSP.
