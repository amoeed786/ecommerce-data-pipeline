"""Quick health check of ShopDB: row counts, order status mix, monthly trend, integrity."""
from db import get_connection

TABLES = ["categories", "customers", "products", "inventory", "orders", "order_items", "payments"]


def main() -> None:
    cur = get_connection().cursor()

    print("Row counts")
    for t in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM dbo.{t}")
        print(f"  {t:12} {cur.fetchone()[0]:>8,}")

    print("\nOrders by status")
    cur.execute("SELECT status, COUNT(*), SUM(total_amount) FROM dbo.orders GROUP BY status ORDER BY 2 DESC")
    for status, n, amount in cur.fetchall():
        print(f"  {status:10} {n:>7,}   {amount:>14,.2f}")

    print("\nOrders per month")
    cur.execute(
        "SELECT FORMAT(order_date, 'yyyy-MM') AS m, COUNT(*) FROM dbo.orders "
        "GROUP BY FORMAT(order_date, 'yyyy-MM') ORDER BY m"
    )
    for month, n in cur.fetchall():
        print(f"  {month}  {n:>6,}  {'#' * (n // 50)}")

    print("\nIntegrity checks")
    cur.execute(
        "SELECT COUNT(*) FROM dbo.orders o WHERE ABS(o.total_amount - "
        "(SELECT SUM(line_total) FROM dbo.order_items i WHERE i.order_id = o.order_id)) > 0.01"
    )
    print(f"  orders where total != sum(items):   {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM dbo.orders o WHERE NOT EXISTS (SELECT 1 FROM dbo.order_items i WHERE i.order_id = o.order_id)")
    print(f"  orders without items:               {cur.fetchone()[0]}")
    cur.execute(
        "SELECT COUNT(*) FROM dbo.orders o WHERE o.status IN ('paid','shipped','delivered') "
        "AND NOT EXISTS (SELECT 1 FROM dbo.payments p WHERE p.order_id = o.order_id)"
    )
    print(f"  paid/shipped/delivered w/o payment: {cur.fetchone()[0]}")


if __name__ == "__main__":
    main()
