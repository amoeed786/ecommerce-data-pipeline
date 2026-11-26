import asyncio, json, os, subprocess, sys, tempfile, time, urllib.request
import websockets
B = sys.argv[1]; PW = os.environ["RAUNAQI_ADMIN_PW"]; ids = json.load(open(sys.argv[2]))
async def main():
    p = subprocess.Popen([r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--headless=new", "--disable-gpu", "--remote-debugging-port=9351", f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try: pg = next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9351/json").read()) if t["type"] == "page"); break
            except Exception: time.sleep(.2)
        async with websockets.connect(pg["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            n = 0
            async def send(m, **pp):
                nonlocal n; n += 1; me = n
                await ws.send(json.dumps({"id": me, "method": m, "params": pp}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == me: return r.get("result", {})
            async def ev(js): return (await send("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)).get("result", {}).get("value")
            await send("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
            async def login(e, pw, role=None):
                await send("Network.clearBrowserCookies"); await send("Page.navigate", url=B + "/login"); await asyncio.sleep(1)
                await ev("fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:" + json.dumps(json.dumps({"email": e, "password": pw, **({"role": role} if role else {})})) + "}).then(r=>r.status)")
            checks = [(None, ["/#/search?q=Probe", f"/#/p/{ids['product_id']}"]), (("vendor@raunaqi.test", "Demo@123"), ["/vendor#products", "/vendor"]),
                      (("admin@raunaqi.test", PW, "admin"), ["/admin#/users", "/admin#/catalog", "/admin#/customers"])]
            bad = 0
            for acct, pages in checks:
                if acct: await login(*acct)
                else: await send("Network.clearBrowserCookies")
                for path in pages:
                    await send("Page.navigate", url="about:blank"); await asyncio.sleep(.2)
                    await send("Page.navigate", url=B + path); await asyncio.sleep(4)
                    if "vendor" in path: await ev("document.querySelector('[data-tab=products]')?.click()"); await asyncio.sleep(1.5)
                    v = await ev("({ran: !!window.__xss, imgs: document.querySelectorAll('img[src=\"x\"]').length, shown: document.body.innerText.includes('onerror')})")
                    print(("PASS" if not v["ran"] and not v["imgs"] else "FAIL"), "stored XSS stays inert on", path, v); bad += bool(v["ran"] or v["imgs"])
            print("\nXSS render:", "clean" if not bad else f"{bad} pages executed the payload")
    finally: p.kill()
asyncio.run(main())
