# Browser test suites

These scripts drive headless Microsoft Edge over the DevTools protocol, so no Playwright or Selenium install is needed. Each one prints a PASS or FAIL line per check and a total at the end.

| Script | What it checks |
|---|---|
| `journeys.py` | End to end through the real UI, checked against SQL Server: the shopper orders (COD), admin starts the order, the seller accepts and packs it, the rider delivers it, and the ledger records the sale and commission. Then a return (ledger nets to 0) and a cancel (stock restored). Also fails on any JS error. **Creates real orders.** |
| `qa2.py` | Every internal link per role (guest, shopper, seller, rider, admin); offline error state and recovery; keyboard focus rings, Ctrl+K, Enter/Esc on the admin drawer. |
| `responsive.py` | 14 pages × 6 widths (360 to 1920). Looks for page side-scroll, elements wider than the screen, clipped text, tap targets under 24px on phones, and JS errors. Screenshots go to `resp/`. Set `LONGTEXT=1` to inject very long names first. |
| `accuracy.py` | API matches the database (prices, ratings, finance totals, order totals, seller balances), and the page matches the API (card prices, aligned buttons, right-aligned numbers). |

Run these from the repo root against a running app (`python -m shop_app.serve --port 8002`):

```powershell
$env:RAUNAQI_ADMIN_PW = "<admin password>"   # never commit it
.\.venv\Scripts\python.exe tests\ui\responsive.py http://localhost:8002
.\.venv\Scripts\python.exe tests\ui\qa2.py http://localhost:8002
.\.venv\Scripts\python.exe tests\ui\accuracy.py http://localhost:8002 .
.\.venv\Scripts\python.exe tests\ui\journeys.py http://localhost:8002
```

`journeys.py` writes to the database, so run it against a dev copy and never against production.
