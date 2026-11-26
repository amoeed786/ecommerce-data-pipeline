"""End-to-end journeys driven through the real UI (typing + clicking), verified against the database.
1) order -> start -> seller accept+pack -> rider pickup/deliver -> shopper sees delivered -> ledger
2) return flow  3) cancel flow (stock restored).  Logs JS errors on every step."""
import asyncio, json, os, subprocess, sys, tempfile, time, urllib.request
import websockets
sys.path.insert(0, "."); from source_app.db import get_connection
SP = os.path.dirname(os.path.abspath(__file__)); B = sys.argv[1]
ADMIN_PW = os.environ["RAUNAQI_ADMIN_PW"]
PRODUCT = 12
db = lambda: get_connection().cursor()
results = []
def ok(name, cond, detail=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"), flush=True)

async def main():
    proc = subprocess.Popen([r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--headless=new", "--disable-gpu", "--remote-debugging-port=9345",
                             f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try: pg = next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9345/json").read()) if t["type"] == "page"); break
            except Exception: time.sleep(.2)
        async with websockets.connect(pg["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
            n = 0; errors = []
            async def send(m, **p):
                nonlocal n; n += 1; me = n
                await ws.send(json.dumps({"id": me, "method": m, "params": p}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("method") == "Runtime.exceptionThrown":
                        d = r["params"]["exceptionDetails"]; errors.append((d.get("exception", {}).get("description") or d.get("text", ""))[:140])
                    if r.get("id") == me: return r.get("result", {})
            async def ev(js):
                r = await send("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)
                return r.get("result", {}).get("value")
            async def until(js, timeout=20, every=.4):
                t = time.time()
                while time.time() - t < timeout:
                    v = await ev(js)
                    if v: return v
                    await asyncio.sleep(every)
                return None
            async def go(path):
                await send("Page.navigate", url=B + path); await asyncio.sleep(.3)
                await until("document.readyState === 'complete'", 15)
                await send("Runtime.evaluate", expression="window.confirm = () => true;")   # accept confirm() dialogs like a user clicking OK
            async def fill(sel, val):
                return await ev(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return false; e.focus(); e.value = {json.dumps(val)}; e.dispatchEvent(new Event('input', {{bubbles:true}})); return true; }})()")
            async def click(sel):
                return await ev(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e || e.disabled) return false; e.click(); return true; }})()")
            async def ui_login(page, email, pw, expect_path):
                await send("Network.clearBrowserCookies"); await go(page)
                await until("!!document.querySelector('form input[name=email]') && document.querySelector('form input[name=email]').offsetParent", 10)
                await fill("form:not(.hidden) input[name=email]", email); await fill("form:not(.hidden) input[name=password]", pw)
                await ev("document.querySelector('form:not(.hidden) input[name=password]').form.requestSubmit()")
                return await until(f"location.pathname === {json.dumps(expect_path)}", 15)
            await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable")
            await send("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)

            async def place_cod_order(qty_plus=True):
                await go("/#/p/%d" % PRODUCT); await ev("localStorage.setItem('shoplite.cart','[]')"); await go("/?r=%f#/p/%d" % (time.time(), PRODUCT))
                ok("product page shows Add to cart", await until("!!document.querySelector('#add') && !document.querySelector('#add').disabled", 15))
                await click("#add")
                ok("Add to cart updates the header count to 1", await until("document.querySelector('#cartcount').textContent.trim() === '1'", 8), await ev("document.querySelector('#cartcount').textContent"))
                await go("/#/cart"); ok("cart page lists the item", await until("document.querySelectorAll('.line-item').length === 1", 10))
                if qty_plus:
                    await click(".line-item .qty button[aria-label=Increase]")
                    ok("cart + button raises quantity to 2", await until("document.querySelector('.line-item .qty output')?.textContent.trim() === '2'", 8))
                await go("/#/checkout"); ok("checkout renders with Place order", await until("!!document.querySelector('#place')", 15))
                await click("input[name=pay][value=cash_on_delivery]"); await fill("#city", "")
                await click("#place"); await asyncio.sleep(1.2)
                ok("checkout refuses an empty city (validation)", (await ev("location.hash")).startswith("#/checkout"))
                await fill("#city", "Lahore"); await click("#place")
                h = await until("(/^#\\/orders\\/(\\d+)\\?placed=1/.exec(location.hash) || [])[1]", 20)
                ok("placing a COD order opens its tracking page", h, await ev("location.hash"))
                if h: ok("success banner shown after placing", await until("document.body.innerText.includes('your order is placed')", 8))
                return int(h) if h else None

            # ---------- journey 1: full fulfilment
            ok("shopper signs in through the sign-in form", await ui_login("/login?as=customer", "customer@raunaqi.test", "Demo@123", "/"))
            stock0 = db().execute("SELECT quantity_on_hand FROM dbo.inventory WHERE product_id=?", PRODUCT).fetchval()
            oid = await place_cod_order()
            if not oid: raise SystemExit("cannot continue without an order")
            r = db().execute("SELECT o.status, o.total_amount, (SELECT SUM(line_total) FROM dbo.order_items WHERE order_id=o.order_id), (SELECT quantity_on_hand FROM dbo.inventory WHERE product_id=?) FROM dbo.orders o WHERE o.order_id=?", PRODUCT, oid).fetchone()
            ok("database: order pending, total = lines, stock reduced by 2", r[0] == "pending" and abs(float(r[1]) - float(r[2])) < .01 and r[3] == stock0 - 2, tuple(r))
            sid = db().execute("SELECT shipment_id FROM dbo.shipments WHERE order_id=?", oid).fetchval()

            ok("admin signs in through the admin sign-in form", await ui_login("/admin/login", "admin@raunaqi.test", ADMIN_PW, "/admin"))
            await go("/admin#/orders")
            ok("new order appears in the admin Orders table", await until(f"!!document.querySelector('tr[data-id=\"{oid}\"]')", 15))
            await click(f'tr[data-id="{oid}"]')
            ok("clicking the row opens the order drawer", await until("document.querySelector('#drawer.on [data-oact=start]') !== null", 10))
            await click("#drawer [data-oact=start]")
            ok("admin Start sends the order to the store", await until(f"fetch('/api/admin/orders/{oid}').then(r=>r.json()).then(o=>o.status==='processing')", 15))

            ok("seller signs in through the sign-in form", await ui_login("/login?as=vendor", "vendor@raunaqi.test", "Demo@123", "/vendor"))
            ok("parcel shows in the seller's New queue", await until(f"!!document.querySelector('[data-one=accept_ready][data-id=\"{sid}\"]')", 20))
            await click(f'[data-one=accept_ready][data-id="{sid}"]')
            ok("seller 'Accept + pack' moves the parcel to ready/rider", await until(f"fetch('/api/vendor/shipments?status=ready&status=assigned&status=picked_up').then(r=>r.json()).then(a=>a.some(s=>s.shipment_id==={sid}))", 20))

            ok("admin signs in again", await ui_login("/admin/login", "admin@raunaqi.test", ADMIN_PW, "/admin"))
            await go("/admin#/shipments")
            ok("parcel card appears on the Live shipments board", await until(f"!!document.querySelector('.kc[data-order=\"{oid}\"]')", 15))
            await click(f'.kc[data-order="{oid}"]'); await until("document.querySelector('#drawer.on') !== null", 10)
            t0 = time.time(); acted = []
            while time.time() - t0 < 120:
                st = await ev(f"fetch('/api/admin/orders/{oid}').then(r=>r.json()).then(o=>o.shipments[0].status)")
                if st == "delivered": break
                for act in ("pickup", "deliver"):
                    if await click(f'#drawer [data-ship="{sid}"][data-sact="{act}"]'): acted.append(act); await asyncio.sleep(1.5)
                await asyncio.sleep(1.5)
            ok(f"parcel delivered (admin acted as rider: {acted or 'autopilot rider did it'})", st == "delivered", st)

            ok("shopper signs in again", await ui_login("/login?as=customer", "customer@raunaqi.test", "Demo@123", "/"))
            await go(f"/#/orders/{oid}")
            ok("shopper's tracking page shows Delivered", await until("[...document.querySelectorAll('.status')].some(e => /delivered/i.test(e.textContent))", 15))
            led = db().execute("SELECT SUM(CASE WHEN entry_type='sale' THEN amount END), SUM(CASE WHEN entry_type='commission' THEN amount END) FROM dbo.ledger_entries WHERE order_id=?", oid).fetchone()
            tot = db().execute("SELECT total_amount FROM dbo.orders WHERE order_id=?", oid).fetchval()
            ok("finance: seller ledger has sale = order total and a commission", led[0] is not None and abs(float(led[0]) - float(tot)) < .01 and led[1] is not None and float(led[1]) < 0, (led, tot))

            # ---------- journey 2: return
            ok("Return button available on a delivered order", await until("!!document.querySelector('#return')", 8))
            await click("#return")
            ok("return completes: order shows Returned", await until("[...document.querySelectorAll('.status')].some(e => /returned/i.test(e.textContent))", 15))
            rv = db().execute("SELECT SUM(amount) FROM dbo.ledger_entries WHERE order_id=?", oid).fetchval()
            ok("finance: return reverses the seller's earnings to 0", rv is not None and abs(float(rv)) < .01, rv)

            # ---------- journey 3: cancel
            s_before = db().execute("SELECT quantity_on_hand FROM dbo.inventory WHERE product_id=?", PRODUCT).fetchval()
            oid2 = await place_cod_order(qty_plus=False)
            await until("!!document.querySelector('#cancel')", 10); await click("#cancel")
            ok("cancel completes: order shows Cancelled", await until("[...document.querySelectorAll('.status')].some(e => /cancelled/i.test(e.textContent))", 15))
            s_after = db().execute("SELECT quantity_on_hand FROM dbo.inventory WHERE product_id=?", PRODUCT).fetchval()
            ok("cancel puts the stock back", s_after == s_before, (s_before, s_after))
            print(f"\ntest orders created: #{oid} (delivered then returned), #{oid2} (cancelled)")
            ok("no JavaScript errors during the journeys", not errors, errors[:4])
    finally:
        proc.kill()
    f = [n for n, c in results if not c]
    print(f"\n{len(results) - len(f)}/{len(results)} checks passed" + (f"; failed: {f}" if f else ""))
asyncio.run(main())
