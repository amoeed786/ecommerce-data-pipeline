"""Frontend accuracy + alignment check.
1) API vs database: product prices/sale/rating/stock, admin finance totals, shopper order totals, seller balances.
2) Page vs API (headless Edge): the prices printed on product cards and the product page equal the API values;
   'Add to cart' buttons in each card row line up; numeric table cells are right-aligned; nothing overflows its card."""
import asyncio, json, os, subprocess, sys, tempfile, time, urllib.request, http.cookiejar
from decimal import Decimal
import websockets
sys.path.insert(0, ".")
from source_app.db import get_connection

B = sys.argv[1]
cur = get_connection().cursor()
fails = []
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))
    if not ok: fails.append(name)

def session(email, pw, role=None):
    cj = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(urllib.request.Request(B + "/api/auth/login", data=json.dumps({"email": email, "password": pw, **({"role": role} if role else {})}).encode(),
                                   headers={"Content-Type": "application/json", "Origin": B}))
    return lambda p: json.loads(op.open(urllib.request.Request(B + p)).read())
anon = lambda p: json.loads(urllib.request.urlopen(B + p).read())

# ---- 1. API vs database
home = anon("/api/home")
prods = {p["product_id"]: p for k in ("flash_deals", "best_sellers", "top_rated") for p in home[k]}
bad = []
for pid, p in prods.items():
    r = cur.execute("SELECT p.unit_price, p.list_price, p.rating, i.quantity_on_hand FROM dbo.products p LEFT JOIN dbo.inventory i ON i.product_id=p.product_id WHERE p.product_id=?", pid).fetchone()
    if (Decimal(str(p["unit_price"])) != r.unit_price or (p["list_price"] or None) != (float(r.list_price) if r.list_price else None)
            or abs(p["rating"] - float(r.rating or 0)) > 1e-9):
        bad.append(pid)
    if p["list_price"] and p["discount_pct"] != round(100 * (1 - p["unit_price"] / p["list_price"])):
        bad.append(f"{pid} discount")
check(f"home: {len(prods)} products - price, sale price, rating and discount % match the database", not bad, bad[:5])
pd = anon("/api/products/141")
r = cur.execute("SELECT unit_price, review_count FROM dbo.products WHERE product_id=141").fetchone()
check("product page data matches database (price, review count)", Decimal(str(pd["unit_price"])) == r.unit_price and pd["review_count"] == r.review_count)
hist = pd["rating_histogram"]; check("rating histogram adds up to ~100%", 98 <= sum(hist.values()) <= 102, hist)

adm = session("admin@raunaqi.test", os.environ["RAUNAQI_ADMIN_PW"], "admin")
f = adm("/api/admin/finance")
db_comm = float(cur.execute("SELECT COALESCE(-SUM(amount),0) FROM dbo.ledger_entries WHERE entry_type IN ('commission','commission_reversal')").fetchval())
db_paid = float(cur.execute("SELECT COALESCE(SUM(amount),0) FROM dbo.payouts WHERE status='paid'").fetchval())
check("finance: all-time commission matches ledger", abs(f["totals"]["commission_all"] - db_comm) < 0.01, (f["totals"]["commission_all"], db_comm))
check("finance: paid-out total matches payouts", abs(f["totals"]["paid_out_all"] - db_paid) < 0.01, (f["totals"]["paid_out_all"], db_paid))
bal_sum = sum(b["gross_sales"] for b in f["balances"])
db_sales = float(cur.execute("SELECT COALESCE(SUM(amount),0) FROM dbo.ledger_entries WHERE entry_type IN ('sale','sale_reversal')").fetchval())
check("finance: seller balances add up to ledger sales", abs(bal_sum - db_sales) < 0.05, (bal_sum, db_sales))
k = adm("/api/admin/kpis?days=7")["days"]
db_orders7 = cur.execute("SELECT COUNT(*) FROM dbo.orders WHERE order_date >= DATEADD(day,-6,CAST(GETDATE() AS date)) AND order_date <= SYSDATETIME()").fetchval()
check("admin KPI: orders over 7 days match database", sum(d["orders"] for d in k) == db_orders7, (sum(d["orders"] for d in k), db_orders7))

cust = session("customer@raunaqi.test", "Demo@123")
orders = cust("/api/me/orders")
mism = []
for o in orders[:15]:
    t = cur.execute("SELECT SUM(line_total) FROM dbo.order_items WHERE order_id=?", o["order_id"]).fetchval()
    if abs(float(t) - o["total_amount"]) > 0.01: mism.append(o["order_id"])
check(f"shopper: {min(15, len(orders))} order totals = sum of their lines", not mism, mism)

ven = session("vendor@raunaqi.test", "Demo@123")
e = ven("/api/vendor/earnings")
vid = ven("/api/auth/me")["vendor_id"]
db_v = float(cur.execute("SELECT COALESCE(SUM(amount),0) FROM dbo.ledger_entries WHERE vendor_id=?", vid).fetchval())
b = e["balance"]; tot = b["pending"] + b["available"] + b["scheduled"] + b["paid"]
check("seller: balance buckets add up to the seller's ledger", abs(tot - db_v) < 0.05, (tot, db_v))

# ---- 2. Page vs API, alignment (headless Edge)
JS = r"""
(() => {
  const out = {};
  const toNum = (t) => Number(String(t).replace(/[^0-9.\-]/g, ''));
  out.cards = [...document.querySelectorAll('.pc')].map(c => ({ id: +c.dataset.product, price: toNum(c.querySelector('.pc-price .now')?.textContent),
     was: c.querySelector('.pc-price s') ? toNum(c.querySelector('.pc-price s').textContent) : null })).filter(c => c.id);
  // buttons line up within each visual row of a grid
  const rows = {}; let misaligned = 0;
  document.querySelectorAll('.pc-grid .pc').forEach(c => { const top = Math.round(c.getBoundingClientRect().top); const btn = c.querySelector('.pc-add');
    if (btn) (rows[top] ||= []).push(Math.round(btn.getBoundingClientRect().top)); });
  Object.values(rows).forEach(ts => { if (Math.max(...ts) - Math.min(...ts) > 1) misaligned++; });
  out.misalignedRows = misaligned; out.rows = Object.keys(rows).length;
  out.overflow = [...document.querySelectorAll('.pc, .panel, .card, .kpi')].filter(e => e.scrollWidth > e.clientWidth + 1).length;
  out.pageOverflow = document.documentElement.scrollWidth - innerWidth;
  out.numLeft = [...document.querySelectorAll('td.num, th.num')].filter(e => getComputedStyle(e).textAlign !== 'right').length;
  out.pdpPrice = toNum(document.querySelector('.price-block .now')?.textContent || 'NaN');
  return out;
})()
"""
async def page_checks():
    proc = subprocess.Popen([r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--headless=new", "--disable-gpu", "--remote-debugging-port=9343",
                             f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try: pg = next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9343/json").read()) if t["type"] == "page"); break
            except Exception: time.sleep(.2)
        async with websockets.connect(pg["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            n = 0
            async def send(m, **p):
                nonlocal n; n += 1; me = n
                await ws.send(json.dumps({"id": me, "method": m, "params": p}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == me: return r.get("result", {})
            for w in (1440, 390):
                await send("Emulation.setDeviceMetricsOverride", width=w, height=900, deviceScaleFactor=1, mobile=w < 600)
                for path in ["/", "/#/c/1", "/#/p/141"]:
                    await send("Page.navigate", url="about:blank"); await asyncio.sleep(.2)
                    await send("Page.navigate", url=B + path); await asyncio.sleep(4)
                    v = (await send("Runtime.evaluate", returnByValue=True, expression=JS))["result"]["value"]
                    api_items = {}
                    if path == "/": api_items = prods
                    elif path.startswith("/#/c/"): api_items = {p["product_id"]: p for p in anon("/api/products?category_id=1&sort=popular&limit=40")["items"]}
                    else: api_items = {p["product_id"]: p for p in pd["related"]}
                    wrong = [c["id"] for c in v["cards"] if c["id"] in api_items and (abs(c["price"] - round(api_items[c["id"]]["unit_price"], 2)) > 0.5
                             or (c["was"] is not None and abs(c["was"] - api_items[c["id"]]["list_price"]) > 0.5))]
                    tag = f"{path} @{w}px"
                    check(f"{tag}: {len(v['cards'])} card prices on screen = API", not wrong, wrong[:5])
                    check(f"{tag}: Add-to-cart buttons aligned in all {v['rows']} rows", v["misalignedRows"] == 0, v["misalignedRows"])
                    check(f"{tag}: no content overflowing cards/panels and no page side-scroll", v["overflow"] == 0 and v["pageOverflow"] <= 2, (v["overflow"], v["pageOverflow"]))
                    if path.startswith("/#/p/"):
                        check(f"{tag}: product page price = API", abs(v["pdpPrice"] - pd["unit_price"]) < 0.5, (v["pdpPrice"], pd["unit_price"]))
            # admin tables: numbers right-aligned
            await send("Network.clearBrowserCookies")
            await send("Page.navigate", url=B + "/login"); await asyncio.sleep(1)
            await send("Runtime.evaluate", awaitPromise=True, expression="fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:'admin@raunaqi.test',password:'" + os.environ["RAUNAQI_ADMIN_PW"] + "',role:'admin'})})")
            await send("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
            for path in ["/admin#/orders", "/admin#/finance", "/admin#/payouts", "/admin#/riders", "/admin/analytics"]:
                await send("Page.navigate", url=B + path); await asyncio.sleep(4.5)
                v = (await send("Runtime.evaluate", returnByValue=True, expression=JS))["result"]["value"]
                check(f"{path}: numeric columns right-aligned, no overflow", v["numLeft"] == 0 and v["pageOverflow"] <= 2, (v["numLeft"], v["pageOverflow"]))
    finally:
        proc.kill()
asyncio.run(page_checks())
print(f"\n{len(fails)} failed" if fails else "\nall checks passed")
