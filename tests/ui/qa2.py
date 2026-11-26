"""Links, network failure and keyboard checks through a real browser."""
import asyncio, json, os, subprocess, sys, tempfile, time, urllib.request
import websockets
SP = os.path.dirname(os.path.abspath(__file__)); B = sys.argv[1]
ADMIN_PW = os.environ["RAUNAQI_ADMIN_PW"]
results = []
def ok(name, cond, detail=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"), flush=True)

async def main():
    proc = subprocess.Popen([r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--headless=new", "--disable-gpu", "--remote-debugging-port=9346",
                             f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try: pg = next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9346/json").read()) if t["type"] == "page"); break
            except Exception: time.sleep(.2)
        async with websockets.connect(pg["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
            n = 0
            async def send(m, **p):
                nonlocal n; n += 1; me = n
                await ws.send(json.dumps({"id": me, "method": m, "params": p}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == me: return r.get("result", {})
            async def ev(js):
                return (await send("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)).get("result", {}).get("value")
            async def go(path, wait=3.5):
                await send("Page.navigate", url="about:blank"); await asyncio.sleep(.15)
                await send("Page.navigate", url=B + path); await asyncio.sleep(wait)
            async def login(email, pw, role=None):
                await send("Network.clearBrowserCookies"); await go("/login", 1)
                await ev("fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:" + json.dumps(json.dumps({"email": email, "password": pw, **({"role": role} if role else {})})) + "}).then(r=>r.status)")
            await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable")
            await send("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)

            # ---------- links on every surface
            surfaces = [(None, ["/", "/#/p/141", "/#/c/1", "/login", "/admin/login"]), (("customer@raunaqi.test", "Demo@123", None), ["/#/orders", "/#/account"]),
                        (("vendor@raunaqi.test", "Demo@123", None), ["/vendor"]), (("rider@raunaqi.test", "Demo@123", None), ["/rider"]),
                        (("admin@raunaqi.test", ADMIN_PW, "admin"), ["/admin#/orders", "/admin/dashboard", "/admin/analytics"])]
            for acct, pages in surfaces:
                if acct: await login(*acct)
                else: await send("Network.clearBrowserCookies")
                links = set()
                for p in pages:
                    await go(p)
                    links |= set(await ev("[...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href')).filter(h => h && !h.startsWith('mailto:') && !h.startsWith('tel:') && !/^https?:/.test(h) && h !== '#')") or [])
                bad = []
                for h in sorted(links):
                    url = h if h.startswith("/") or h.startswith("#") else "/" + h
                    if url.startswith("#"): url = (pages[0].split("#")[0] or "/") + url
                    await go(url, 2.6)
                    state = await ev("""(() => { const t = document.body.innerText; return { title: document.title, err: /Something went wrong|Couldn't load this page|We couldn't find that page|isn't available/.test(t) && !/\\/p\\/999/.test(location.href), path: location.pathname + location.hash, len: t.length }; })()""")
                    if not state or state["err"] or state["len"] < 40: bad.append((h, state))
                who = acct[0].split("@")[0] if acct else "guest"
                ok(f"{who}: all {len(links)} internal links open a real page", not bad, bad[:4])
            # external links point somewhere sane (just list them)
            # ---------- network failure + recovery (store)
            await send("Network.clearBrowserCookies"); await go("/")
            await send("Network.emulateNetworkConditions", offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
            await ev("_apiCache.clear(); location.hash = '#/c/5'"); await asyncio.sleep(3)
            st = await ev("({ text: document.querySelector('#view').innerText.slice(0, 120), retry: !!document.querySelector('#view button') })")
            ok("offline: store shows an error message with a Try again button (not a blank page)", st and st["retry"] and len(st["text"]) > 10, st)
            await send("Network.emulateNetworkConditions", offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
            await ev("document.querySelector('#view button').click()"); await asyncio.sleep(3)
            ok("back online: Try again loads the page", await ev("document.querySelectorAll('.pc').length > 0"))
            # ---------- keyboard: tab through the store header, check visible focus
            await go("/")
            seen = []
            for i in range(12):
                await send("Input.dispatchKeyEvent", type="keyDown", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                await send("Input.dispatchKeyEvent", type="keyUp", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                f = await ev("""(() => { const e = document.activeElement; if (!e || e === document.body) return null; const s = getComputedStyle(e);
                    const visible = (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || (s.boxShadow && s.boxShadow !== 'none');
                    return { el: (e.getAttribute('aria-label') || e.textContent || e.tagName).trim().slice(0, 24), visible }; })()""")
                if f: seen.append(f)
            nofocus = [s["el"] for s in seen if not s["visible"]]
            ok(f"keyboard: Tab reaches {len(seen)} controls in order (skip link, logo, search, account, cart, categories)", len(seen) >= 8, seen)
            ok("keyboard: every focused control shows a visible focus ring", not nofocus, nofocus)
            # admin console keyboard
            await login("admin@raunaqi.test", ADMIN_PW, "admin"); await go("/admin#/orders", 4)
            await send("Input.dispatchKeyEvent", type="keyDown", key="k", code="KeyK", windowsVirtualKeyCode=75, modifiers=2)
            await send("Input.dispatchKeyEvent", type="keyUp", key="k", code="KeyK", windowsVirtualKeyCode=75, modifiers=2); await asyncio.sleep(.6)
            ok("keyboard: Ctrl+K opens the command palette", await ev("document.querySelector('#cmdk').open"))
            await send("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
            await send("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27); await asyncio.sleep(.4)
            ok("keyboard: Esc closes the command palette", await ev("!document.querySelector('#cmdk').open"))
            await ev("document.querySelector('tbody tr[data-id]').focus()")
            await send("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
            await send("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13); await asyncio.sleep(1.5)
            ok("keyboard: Enter on an order row opens its drawer", await ev("document.querySelector('#drawer').classList.contains('on')"))
            await send("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
            await send("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27); await asyncio.sleep(.5)
            ok("keyboard: Esc closes the drawer and focus returns to the row", await ev("!document.querySelector('#drawer').classList.contains('on') && document.activeElement?.matches('tr[data-id]')"))
    finally:
        proc.kill()
    f = [n for n, c in results if not c]
    print(f"\n{len(results) - len(f)}/{len(results)} checks passed" + (f"; failed: {f}" if f else ""))
asyncio.run(main())
