"""Responsive check: every surface at 360/390/768/1024/1440/1920 px - page side-scroll, elements wider than the
screen, text overflowing its box, tap targets < 24px on phones, JS errors. Screenshots saved per width."""
import asyncio, base64, json, os, subprocess, sys, tempfile, time, urllib.request
import websockets
SP = os.path.dirname(os.path.abspath(__file__)); B = sys.argv[1]; OUT = os.path.join(SP, "resp"); os.makedirs(OUT, exist_ok=True)
WIDTHS = [360, 390, 768, 1024, 1440, 1920]
PAGES = [(None, "home", "/"), (None, "category", "/#/c/1"), (None, "product", "/#/p/141"), (None, "login", "/login?as=customer"),
         ("customer@raunaqi.test|Demo@123|", "cart", "/#/cart"), ("customer@raunaqi.test|Demo@123|", "checkout", "/#/checkout"),
         ("customer@raunaqi.test|Demo@123|", "orders", "/#/orders"), ("vendor@raunaqi.test|Demo@123|", "seller", "/vendor"),
         ("rider@raunaqi.test|Demo@123|", "rider", "/rider"), ("admin@raunaqi.test|" + os.environ["RAUNAQI_ADMIN_PW"] + "|admin", "admin-orders", "/admin#/orders"),
         ("admin@raunaqi.test|" + os.environ["RAUNAQI_ADMIN_PW"] + "|admin", "admin-shipments", "/admin#/shipments"), ("admin@raunaqi.test|" + os.environ["RAUNAQI_ADMIN_PW"] + "|admin", "admin-finance", "/admin#/finance"),
         ("admin@raunaqi.test|" + os.environ["RAUNAQI_ADMIN_PW"] + "|admin", "live-ops", "/admin/dashboard"), ("admin@raunaqi.test|" + os.environ["RAUNAQI_ADMIN_PW"] + "|admin", "analytics", "/admin/analytics")]
LONG = r"""(() => { const L = 'Extraordinarily long product or customer name that keeps going and going to test wrapping in this layout', U = 'Unbrokenstringwithoutanyspacesatallxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx';
  document.querySelectorAll('td:first-child, td:nth-child(2), .pc-title, .t, .name, h1, h2, h3, .kpi .l, .insight, .crumbs span').forEach((e, i) => { if (e.children.length < 3) e.textContent = (i % 2 ? U : L); }); })()"""
JS = r"""(() => { const W = innerWidth, vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width && r.height && s.visibility !== 'hidden'; };
  const scrollers = '.hrow, .card-b, .bi-nav, .dt-scroll, .table-wrap, .kanban, .catnav, .tabs, .thumbs, .chips, pre, .stage-tabs, .side';
  const wide = [...document.querySelectorAll('body *')].filter(e => vis(e) && e.getBoundingClientRect().right > W + 2 && !e.closest(scrollers) && !e.closest('[aria-hidden=true]'))
     .map(e => (typeof e.className === 'string' && e.className ? '.' + e.className.split(' ')[0] : e.tagName.toLowerCase()) + '→' + Math.round(e.getBoundingClientRect().right));
  const clipped = [...document.querySelectorAll('h1,h2,h3,button,.k-btn,.btn,.kpi .v,.kpi .value,.pc-title,.price,.now')].filter(e => vis(e) && e.scrollWidth > e.clientWidth + 2 && getComputedStyle(e).overflow !== 'hidden' && getComputedStyle(e).textOverflow !== 'ellipsis').length;
  const small = W < 600 ? [...document.querySelectorAll('a,button,input,select')].filter(e => { if (!vis(e) || e.closest('table, p, .sf, .crumbs, .legal, .credit, .rv, label')) return false; const r = e.getBoundingClientRect(); return r.width < 24 || r.height < 24; }).length : 0;
  return { sideScroll: document.documentElement.scrollWidth - W, wide: [...new Set(wide)].slice(0, 4), clipped, small }; })()"""

async def main():
    proc = subprocess.Popen([r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--headless=new", "--disable-gpu", "--remote-debugging-port=9344",
                             f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bad = 0; total = 0
    try:
        for _ in range(50):
            try: pg = next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9344/json").read()) if t["type"] == "page"); break
            except Exception: time.sleep(.2)
        async with websockets.connect(pg["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
            n = 0; errors = []
            async def send(m, **p):
                nonlocal n; n += 1; me = n
                await ws.send(json.dumps({"id": me, "method": m, "params": p}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("method") == "Runtime.exceptionThrown": errors.append(r["params"]["exceptionDetails"].get("text", "")[:80])
                    if r.get("id") == me: return r.get("result", {})
            await send("Runtime.enable"); await send("Page.enable")
            acct_now = "x"
            for acct, name, path in PAGES:
                if acct != acct_now:
                    await send("Network.clearBrowserCookies"); await send("Page.navigate", url=B + "/login"); await asyncio.sleep(1)
                    if acct:
                        e, pw, role = acct.split("|")
                        await send("Runtime.evaluate", awaitPromise=True, expression="fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:" + json.dumps(json.dumps({"email": e, "password": pw, **({"role": role} if role else {})})) + "})")
                        if "customer" in e: await send("Runtime.evaluate", expression="localStorage.setItem('shoplite.cart', JSON.stringify([{product_id:141,quantity:1}]))")
                    acct_now = acct
                line = []
                for w in WIDTHS:
                    errors.clear(); total += 1
                    await send("Emulation.setDeviceMetricsOverride", width=w, height=900, deviceScaleFactor=1, mobile=w < 600)
                    await send("Page.navigate", url="about:blank"); await asyncio.sleep(.15)
                    await send("Page.navigate", url=B + path); await asyncio.sleep(3.2)
                    if os.environ.get("LONGTEXT"): await send("Runtime.evaluate", expression=LONG); await asyncio.sleep(.4)
                    v = (await send("Runtime.evaluate", returnByValue=True, expression=JS))["result"]["value"]
                    shot = await send("Page.captureScreenshot", format="jpeg", quality=60)
                    open(os.path.join(OUT, f"{name}_{w}.jpg"), "wb").write(base64.b64decode(shot["data"]))
                    ok = v["sideScroll"] <= 2 and not v["wide"] and v["clipped"] == 0 and v["small"] == 0 and not errors
                    bad += not ok
                    line.append(f"{w}:" + ("ok" if ok else f"X{json.dumps({k: v[k] for k in v if v[k]})}{' js' if errors else ''}"))
                print(f"{name:16} " + "  ".join(line), flush=True)
    finally:
        proc.kill()
    print(f"\n{total - bad}/{total} page-width combinations clean")
asyncio.run(main())
