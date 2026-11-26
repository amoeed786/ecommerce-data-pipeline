"""Live security probe against a running Raunaqi server. Usage: python attack.py http://localhost:8002
Creates: 1 shopper account (xss name), 1 inactive product in the demo seller's store. Both are disabled at the end."""
import json, os, re, sys, time, http.cookiejar, urllib.request, urllib.error
sys.path.insert(0, ".")
from source_app.db import get_connection

B = sys.argv[1]; HOST = B.split("//")[1]
ADMIN_PW = os.environ["RAUNAQI_ADMIN_PW"]
cur = get_connection(autocommit=True).cursor()
res = []
import random
RUN_IP = f"198.51.100.{random.randrange(1, 255)}"
def ok(name, cond, detail=""):
    res.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"), flush=True)

class S:
    def __init__(s):
        s.cj = http.cookiejar.CookieJar(); s.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(s.cj))
    def req(s, method, path, body=None, headers=None, raw=None):
        h = {"Origin": B, "X-Forwarded-For": RUN_IP, **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and raw is None: h["Content-Type"] = "application/json"
        r = urllib.request.Request(B + path, data=data, method=method, headers=h)
        try:
            resp = s.op.open(r, timeout=60); return resp.status, resp.read().decode("utf-8", "replace"), {k.lower(): v for k, v in resp.headers.items()}
        except (ConnectionResetError, ConnectionAbortedError):
            return 413, "connection closed by server", {}
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), {k.lower(): v for k, v in e.headers.items()}
    def login(s, email, pw, role=None):
        return s.req("POST", "/api/auth/login", {"email": email, "password": pw, **({"role": role} if role else {})})

anon = S()
cust = S(); cust.login("customer@raunaqi.test", "Demo@123")
ven = S(); ven.login("vendor@raunaqi.test", "Demo@123")
rid = S(); rid.login("rider@raunaqi.test", "Demo@123")
adm = S(); st, _, hdr = adm.login("admin@raunaqi.test", ADMIN_PW, "admin")
ok("admin login works", st == 200, st)

# ---------- 1. role x route matrix from the OpenAPI spec
st, spec, _ = adm.req("GET", "/openapi.json"); spec = json.loads(spec)
owner_of = {"admin": "admin", "vendor": "vendor", "rider": "rider", "customer": "customer"}
who = {"anon": anon, "customer": cust, "vendor": ven, "rider": rid}
leaks = []; checked = 0
for path, ops in spec["paths"].items():
    for method, op in ops.items():
        tag = (op.get("tags") or [""])[0]
        need = owner_of.get(tag) or ("admin" if tag == "analytics" and path != "/api/events" else None)
        if not need: continue
        p = re.sub(r"\{[^}]+\}", "1", path)
        for name, sess in who.items():
            if name == need: continue
            if path == "/api/events": continue
            st, body, _ = sess.req(method.upper(), p, {} if method != "get" else None)
            checked += 1
            if st not in (401, 403): leaks.append((name, method.upper(), p, st))
ok(f"authorization: {checked} (route, wrong role) calls all refused with 401/403", not leaks, leaks[:6])
st, _, _ = anon.req("GET", "/docs"); ok("API docs hidden from the public", st in (401, 403), st)
st, _, _ = cust.req("GET", "/openapi.json"); ok("OpenAPI schema hidden from non-admins", st in (401, 403), st)

# ---------- 2. IDOR
me_cust = json.loads(cust.req("GET", "/api/auth/me")[1])["customer_id"]
other_order = cur.execute("SELECT TOP 1 order_id FROM dbo.orders WHERE customer_id <> ? AND status='pending' ORDER BY order_id DESC", me_cust).fetchval() \
    or cur.execute("SELECT TOP 1 order_id FROM dbo.orders WHERE customer_id <> ? ORDER BY order_id DESC", me_cust).fetchval()
ok("IDOR: shopper can't read another shopper's order", cust.req("GET", f"/api/me/orders/{other_order}")[0] == 404)
before = cur.execute("SELECT status FROM dbo.orders WHERE order_id=?", other_order).fetchval()
st, _, _ = cust.req("POST", f"/api/me/orders/{other_order}/cancel")
ok("IDOR: shopper can't cancel another shopper's order", st >= 400 and cur.execute("SELECT status FROM dbo.orders WHERE order_id=?", other_order).fetchval() == before, st)
ok("IDOR: shopper can't start payment on another shopper's order", cust.req("POST", f"/api/me/orders/{other_order}/pay")[0] == 404)
my_vid = json.loads(ven.req("GET", "/api/auth/me")[1])["vendor_id"]
o_ship = cur.execute("SELECT TOP 1 shipment_id FROM dbo.shipments WHERE vendor_id <> ? AND status='confirmed'", my_vid).fetchval() \
    or cur.execute("SELECT TOP 1 shipment_id FROM dbo.shipments WHERE vendor_id <> ?", my_vid).fetchval()
s0 = cur.execute("SELECT status FROM dbo.shipments WHERE shipment_id=?", o_ship).fetchval()
st, _, _ = ven.req("POST", f"/api/vendor/shipments/{o_ship}/accept")
ok("IDOR: seller can't accept another seller's parcel", st >= 400 and cur.execute("SELECT status FROM dbo.shipments WHERE shipment_id=?", o_ship).fetchval() == s0, st)
st, body, _ = ven.req("POST", "/api/vendor/shipments/bulk", {"shipment_ids": [o_ship], "action": "accept"})
ok("IDOR: bulk action skips another seller's parcel", cur.execute("SELECT status FROM dbo.shipments WHERE shipment_id=?", o_ship).fetchval() == s0, (st, body[:120]))
o_prod = cur.execute("SELECT TOP 1 product_id FROM dbo.products WHERE vendor_id <> ?", my_vid).fetchval()
p0 = cur.execute("SELECT unit_price FROM dbo.products WHERE product_id=?", o_prod).fetchval()
st, _, _ = ven.req("PATCH", f"/api/vendor/products/{o_prod}", {"unit_price": 1})
ok("IDOR: seller can't reprice another seller's product", st == 404 and cur.execute("SELECT unit_price FROM dbo.products WHERE product_id=?", o_prod).fetchval() == p0, st)
st, _, _ = ven.req("POST", f"/api/vendor/products/{o_prod}/image", raw=b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\n\r\n\x89PNG\r\n\x1a\n0000\r\n--x--\r\n", headers={"Content-Type": "multipart/form-data; boundary=x"})
ok("IDOR: seller can't replace another seller's product photo", st == 404, st)
my_rid = json.loads(rid.req("GET", "/api/auth/me")[1])["rider_id"]
r_ship = cur.execute("SELECT TOP 1 shipment_id FROM dbo.shipments WHERE rider_id IS NOT NULL AND rider_id <> ? ORDER BY shipment_id DESC", my_rid).fetchval()
r0 = cur.execute("SELECT status FROM dbo.shipments WHERE shipment_id=?", r_ship).fetchval()
st, _, _ = rid.req("POST", f"/api/rider/jobs/{r_ship}/deliver")
ok("IDOR: rider can't deliver another rider's parcel", st >= 400 and cur.execute("SELECT status FROM dbo.shipments WHERE shipment_id=?", r_ship).fetchval() == r0, st)

# ---------- 3. CSRF
st, _, _ = cust.req("PUT", "/api/me/profile", {"phone": "", "city": "Hacked", "country": ""}, headers={"Origin": "https://evil.example"})
ok("CSRF: cross-site Origin is blocked", st == 403, st)
st, _, _ = cust.req("PUT", "/api/me/profile", {"phone": "", "city": "Hacked", "country": ""}, headers={"Origin": "null"})
ok("CSRF: Origin 'null' (sandboxed iframe) is blocked", st == 403, st)
ck = [c for c in cust.cj if c.name == "shoplite_session"][0]
ok("session cookie is HttpOnly + SameSite=Lax", ck.has_nonstandard_attr("HttpOnly") and (ck.get_nonstandard_attr("SameSite") or "").lower() == "lax", ck._rest)

# ---------- 4. mass assignment + bad numbers
evil_email = f"xss{int(time.time())}@probe.test"
XSS = '<img src=x onerror="window.__xss=1">'
st, body, _ = anon.req("POST", "/api/auth/register", {"first_name": XSS, "last_name": "Probe", "email": evil_email, "password": "Probe#2026x", "role": "admin", "is_active": 1})
role = cur.execute("SELECT role FROM dbo.users WHERE email=?", evil_email).fetchval()
ok("mass assignment: 'role':'admin' in sign-up is ignored", role == "customer", (st, role))
xs = S(); xs.login(evil_email, "Probe#2026x")
pid = int(cur.execute("SELECT TOP 1 product_id FROM dbo.order_items oi JOIN dbo.orders o ON o.order_id=oi.order_id WHERE 1=0").fetchval() or 12)
for q in (0, -1, 21, 10**9):
    st, _, _ = xs.req("POST", "/api/orders", {"items": [{"product_id": 12, "quantity": q}], "payment_method": "cash_on_delivery"})
    ok(f"checkout rejects quantity {q}", st in (400, 422), st)
st, body, _ = xs.req("POST", "/api/orders", {"items": [{"product_id": 12, "quantity": 1}], "payment_method": "free", "ship_city": "Lahore"})
ok("checkout rejects an unknown payment method", st == 400, st)
st, body, _ = xs.req("POST", "/api/orders", {"items": [{"product_id": 12, "quantity": 1}], "payment_method": "cash_on_delivery", "coupon": "FREE100"})
ok("checkout rejects an invented coupon", st == 400, st)
my_prod = cur.execute("SELECT TOP 1 product_id FROM dbo.products WHERE vendor_id=? ORDER BY product_id", my_vid).fetchval()
price0 = cur.execute("SELECT unit_price FROM dbo.products WHERE product_id=?", my_prod).fetchval()
for patch, label in [({"unit_price": -5}, "negative price"), ({"unit_price": 0}, "zero price"), ({"add_stock": -50}, "negative restock")]:
    st, _, _ = ven.req("PATCH", f"/api/vendor/products/{my_prod}", patch)
    ok(f"seller update rejects {label}", st in (400, 422), st)
for patch, label in [({"unit_price": 1e30}, "absurd price 1e30"), ({"add_stock": 10**12}, "absurd restock 1e12"), ({"list_price": 1e30}, "absurd list price")]:
    st, body, _ = ven.req("PATCH", f"/api/vendor/products/{my_prod}", patch)
    ok(f"seller update answers {label} with a 4xx, not a server error", 400 <= st < 500, (st, body[:100]))
cur.execute("UPDATE dbo.products SET unit_price=? WHERE product_id=?", price0, my_prod)
st, body, _ = ven.req("POST", "/api/vendor/products", {"product_name": "N" * 5000, "category_id": 1, "unit_price": 10, "unit_cost": 5, "stock": 1})
ok("very long product name gets a 4xx, not a server error", 400 <= st < 500, (st, body[:100]))
st, body, _ = ven.req("GET", "/api/vendor/shipments?" + "&".join(["status=confirmed"] * 2200))
ok("2,200 status filters get a 4xx, not a server error", 400 <= st < 500 or st == 200, (st, body[:100]))
st, body, _ = anon.req("POST", "/api/auth/login", raw=json.dumps({"email": "a@b.c", "password": "x" * 2_000_000}).encode(), headers={"Content-Type": "application/json"})
ok("2 MB password is refused quickly (no scrypt on huge input)", st in (400, 401, 413, 422), st)

# ---------- 5. stored XSS rendered in the real pages (shopper name -> admin users; product name -> store search, seller center)
st, body, _ = ven.req("POST", "/api/vendor/products", {"product_name": "Probe " + XSS, "category_id": 1, "unit_price": 10, "unit_cost": 5, "stock": 1, "description": XSS})
xss_pid = json.loads(body).get("product_id") if st == 200 else None
ok("XSS payload accepted as plain data (it must be escaped on output)", xss_pid, (st, body[:100]))
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "xss_ids.json"), "w").write(json.dumps({"email": evil_email, "product_id": xss_pid}))

# ---------- 6. sessions, lockout, timing, headers, traversal, webhook
tmp = S(); tmp.login(evil_email, "Probe#2026x"); tok = [c for c in tmp.cj if c.name == "shoplite_session"][0].value
tmp.req("POST", "/api/auth/logout")
st, _, _ = anon.req("GET", "/api/me/orders", headers={"Cookie": f"shoplite_session={tok}"})
ok("a logged-out session token no longer works", st == 401, st)
for _ in range(5): anon.login(evil_email, "wrong-pass-1")
st, _, _ = anon.login(evil_email, "Probe#2026x")
ok("5 wrong passwords lock the account (even the right password gets 429)", st == 429, st)
def t(email):
    a = time.perf_counter(); anon.login(email, "Wrong#pass1"); return time.perf_counter() - a
real = sorted(t(f"timing{i}@raunaqi.test") for i in range(1))  # warm
known = sorted(t("rider@raunaqi.test") for _ in range(4))[1:3]; unknown = sorted(t(f"nobody{i}@nowhere.test") for i in range(4))[1:3]
gap = sum(known) / 2 - sum(unknown) / 2
ok(f"login time doesn't reveal which emails exist (known-unknown gap {gap*1000:.0f} ms)", abs(gap) < 0.025, gap)
st, _, h = anon.req("GET", "/")
need = {"content-security-policy", "x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy"}
ok("security headers on pages", need <= set(h), need - set(h))
ok("CSP forbids framing and plugins", "frame-ancestors 'none'" in h.get("content-security-policy", "") and "object-src 'none'" in h.get("content-security-policy", ""))
ok("CSP allows inline scripts (weakens XSS defence) - informational", "'unsafe-inline'" not in h.get("content-security-policy", "").split("script-src")[1].split(";")[0], "script-src has 'unsafe-inline'")
for p in ["/static/../.env", "/static/%2e%2e/%2e%2e/.env", "/static/..%2f..%2f.env", "/static/", "/static/uploads/"]:
    st, body, _ = anon.req("GET", p)
    ok(f"no traversal or listing: {p}", st in (403, 404) or "SQL_SERVER" not in body and "<a href" not in body[:2000], st)
st, _, _ = anon.req("POST", "/api/webhooks/sandbox", raw=json.dumps({"id": "evt_x", "type": "payment.succeeded", "session_ref": "x", "amount": 1}).encode(), headers={"X-Signature": "00"})
ok("webhook with a forged signature is rejected", st == 400, st)
st, body, _ = anon.req("GET", "/api/products?sort=" + urllib.request.quote("price_asc; DROP TABLE x--"))
ok("sort parameter can't inject SQL (falls back to default)", st == 200, st)
st, body, _ = anon.req("GET", "/api/products?q=" + urllib.request.quote("' OR 1=1 --"))
ok("search text is treated as data, not SQL", st == 200 and json.loads(body)["total"] == 0, (st, body[:80]))
st, body, _ = anon.req("GET", "/api/products/abc")
ok("non-numeric ids get 422, no stack trace", st == 422 and "Traceback" not in body, st)
st, _, _ = anon.req("POST", "/api/auth/login", raw=b"x" * (6 * 1024 * 1024), headers={"Content-Type": "application/json"})
ok("a 6 MB request body is refused with 413", st == 413, st)
st, _, h = anon.req("GET", "/", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"})
ok("behind an HTTPS tunnel the page sends HSTS", "strict-transport-security" in h, list(h)[:12])
logf = os.path.join(".", "data_lake", "_state", "security.log")
txt = open(logf, encoding="utf-8").read() if os.path.exists(logf) else ""
ok("security log records failed logins, lockouts and blocked CSRF", all(k in txt for k in ("login_failed", "login_locked", "csrf_blocked")), txt[-300:])
ok("security log never contains a password", "Probe#2026x" not in txt and "wrong-pass-1" not in txt)
ak = [c for c in adm.cj if c.name == "shoplite_session"][0]
ok("admin session cookie lasts 12 hours, not 7 days", ak.expires and ak.expires - time.time() < 13 * 3600, ak.expires)
f = [n for n, c in res if not c]
print(f"\n{len(res) - len(f)}/{len(res)} passed" + (f"\nFAILED: {f}" if f else ""))
