"""Make the operations history realistic (one-time; refuses to run twice).

  - Rider fleet: grows to ~45 riders across Pakistani cities (mostly motorbikes), as autopilot riders.
  - Finished parcels (delivered / returned) get realistic timelines measured from when the store confirmed:
      accept 1-6 min, prep 10-45 min, rider assigned 2-12 min, pickup 5-20 min, ride 20-90 min.
    Seller ledger rows (created at delivery, released 7 days later) and cash-on-delivery payment times move with
    their parcel, so the money stays consistent.
  - Past deliveries are spread over the fleet so a rider does about 8-25 deliveries a day
    (riders that belong to real login accounts keep their own history).
Active parcels (anything not finished) are not touched.

Usage:  python tools/realistic_ops.py
"""
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from source_app.db import get_connection  # noqa: E402

CITIES = ["Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar"]
CITY_W = [26, 24, 12, 9, 9, 7, 6]
FIRST = ["Ahmed", "Ali", "Hamza", "Usman", "Bilal", "Hassan", "Omar", "Saad", "Fahad", "Zain", "Danish", "Imran", "Kashif",
         "Faisal", "Asad", "Junaid", "Talha", "Haris", "Waqas", "Adeel", "Rehan", "Arslan", "Sohail", "Yasir", "Umer", "Shoaib",
         "Naveed", "Tariq", "Zubair", "Jameel", "Irfan", "Sajid", "Akram", "Rizwan", "Aamir", "Shahid"]
LAST = ["Khan", "Ahmed", "Malik", "Hussain", "Butt", "Qureshi", "Chaudhry", "Raza", "Iqbal", "Akhtar", "Mirza", "Javed",
        "Abbasi", "Shah", "Rana", "Aslam", "Farooq", "Anwar", "Baig", "Awan", "Gondal", "Bhatti", "Niazi", "Afridi"]
FLEET = 45


def main() -> None:
    random.seed(7)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("IF OBJECT_ID('dbo.migration_log','U') IS NULL CREATE TABLE dbo.migration_log (name VARCHAR(80) PRIMARY KEY, ran_at DATETIME2(0) NOT NULL)")
    if cur.execute("SELECT 1 FROM dbo.migration_log WHERE name = 'realistic_ops'").fetchone():
        sys.exit("Operations history already made realistic - nothing to do.")
    now = datetime.now().replace(microsecond=0)

    # ---- fleet
    have = cur.execute("SELECT COUNT(*) FROM dbo.riders").fetchval()
    used = {r[0] for r in cur.execute("SELECT full_name FROM dbo.riders").fetchall()}
    for _ in range(max(0, FLEET - have)):
        while (name := f"{random.choice(FIRST)} {random.choice(LAST)}") in used:
            pass
        used.add(name)
        vehicle = random.choices(["bike", "car", "van"], [82, 12, 6])[0]
        cur.execute("INSERT INTO dbo.riders (full_name, phone, city, vehicle, status, is_bot, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
                    name, f"+92 3{random.randint(0, 4)}{random.randint(0, 9)} {random.randint(1000000, 9999999)}",
                    random.choices(CITIES, CITY_W)[0], vehicle, random.choices(["available", "offline"], [70, 30])[0],
                    now - timedelta(days=random.randint(30, 360)), now)
    bots = [r[0] for r in cur.execute("SELECT r.rider_id FROM dbo.riders r WHERE r.is_bot = 1 "
                                      "AND NOT EXISTS (SELECT 1 FROM dbo.users u WHERE u.rider_id = r.rider_id)").fetchall()]
    print(f"riders: fleet of {have + max(0, FLEET - have)} ({len(bots)} autopilot riders available for history)")

    # ---- timelines of finished parcels
    rows = cur.execute("""
        SELECT s.shipment_id, s.confirmed_at, s.delivered_at, s.rider_id, r.is_bot,
               CASE WHEN EXISTS (SELECT 1 FROM dbo.users u WHERE u.rider_id = s.rider_id) THEN 1 ELSE 0 END AS human
        FROM dbo.shipments s LEFT JOIN dbo.riders r ON r.rider_id = s.rider_id
        WHERE s.status IN ('delivered','returned') AND s.confirmed_at IS NOT NULL AND s.delivered_at IS NOT NULL
    """).fetchall()
    updates, by_day = [], defaultdict(list)
    for sid, conf, old_deliv, rider, _bot, human in rows:
        acc = conf + timedelta(minutes=random.uniform(1, 6))
        prep = min(45, max(10, random.lognormvariate(math.log(22), .38)))
        ready = acc + timedelta(minutes=prep)
        assigned = ready + timedelta(minutes=random.uniform(2, 12))
        picked = assigned + timedelta(minutes=random.uniform(5, 20))
        deliv = picked + timedelta(minutes=min(90, max(20, random.lognormvariate(math.log(38), .35))))
        if deliv > now:  # very recent: compress the steps so they fit between confirmation and now
            f = max(0.0, (now - conf).total_seconds() - 60) / (deliv - conf).total_seconds()
            acc, ready, assigned, picked, deliv = (conf + (t - conf) * f for t in (acc, ready, assigned, picked, deliv))
        updates.append([sid, acc, ready, assigned, picked, deliv, old_deliv, rider])
        if not human:
            by_day[deliv.date()].append(len(updates) - 1)
    # spread each day's deliveries over a share of the fleet: ~8-25 per rider
    for day, idx in by_day.items():
        k = min(len(bots), max(1, math.ceil(len(idx) / random.uniform(12, 18))))
        crew = random.sample(bots, k)
        random.shuffle(idx)
        for j, i in enumerate(idx):
            updates[i][7] = crew[j % k]
    cur.execute("CREATE TABLE #t (sid INT PRIMARY KEY, acc DATETIME2(0), ready DATETIME2(0), assigned DATETIME2(0), picked DATETIME2(0), "
                "deliv DATETIME2(0), old_deliv DATETIME2(0), rider INT)")
    cur.fast_executemany = True
    cur.executemany("INSERT INTO #t VALUES (?,?,?,?,?,?,?,?)", [tuple(u) for u in updates])
    cur.execute("""
        UPDATE s SET accepted_at = t.acc, ready_at = t.ready, assigned_at = t.assigned, picked_up_at = t.picked,
                     delivered_at = t.deliv, rider_id = t.rider, updated_at = ?
          FROM dbo.shipments s JOIN #t t ON t.sid = s.shipment_id;
        UPDATE l SET created_at = DATEADD(second, DATEDIFF(second, t.old_deliv, t.deliv), l.created_at),
                     available_at = DATEADD(second, DATEDIFF(second, t.old_deliv, t.deliv), l.available_at), updated_at = ?
          FROM dbo.ledger_entries l JOIN #t t ON t.sid = l.shipment_id WHERE l.entry_type IN ('sale','commission');
        UPDATE p SET paid_at = x.deliv, updated_at = ?
          FROM dbo.payments p JOIN (SELECT s.order_id, MAX(t.deliv) deliv, MAX(t.old_deliv) old_deliv FROM #t t
                                    JOIN dbo.shipments s ON s.shipment_id = t.sid GROUP BY s.order_id) x ON x.order_id = p.order_id
          WHERE p.payment_method = 'cash_on_delivery' AND p.paid_at = x.old_deliv;
    """, now, now, now)
    cur.execute("INSERT INTO dbo.migration_log (name, ran_at) VALUES ('realistic_ops', ?)", now)
    conn.commit()
    conn.close()
    print(f"shipments: {len(updates):,} finished parcels re-timed; deliveries spread over the fleet")


if __name__ == "__main__":
    main()
