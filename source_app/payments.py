"""Payments, commission and seller payouts - the money side of the marketplace.

GATEWAYS
  A gateway takes the customer's money on a hosted page and then tells us the result with a signed
  callback (webhook). We never see card numbers. Every gateway implements the same small interface:
      create_session(order) -> (session_ref, url_to_send_the_customer_to)
      verify(payload, signature) -> bool          (is this callback really from the gateway?)
      refund(session_ref, amount) -> refund_ref
  `SandboxGateway` is a built-in, fully working stand-in (test cards, signed callbacks, refunds) so the whole
  flow runs locally. A real Pakistani gateway (JazzCash, Easypaisa, a bank) is one more class with the same
  three methods - nothing else in the app changes. Choose with PAYMENT_GATEWAY in .env (default: sandbox).

COMMISSION + LEDGER
  Rate lookup: the seller's own rate, else the product category's rate, else the default.
  When a parcel is delivered, each of its order lines writes two ledger rows for that seller:
      +line_total (sale)   and   -line_total x rate (commission, the platform's revenue)
  They stay 'pending' through the return window (PAYOUT_HOLD_DAYS), then become 'available'.
  A return writes reversal rows that cancel both. Admin runs payouts: each seller's available rows are
  grouped into one payout ('scheduled'), then marked 'paid' with the bank reference.

All functions take a pyodbc cursor and do not commit, like marketplace.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

HOLD_DAYS = float(os.getenv("PAYOUT_HOLD_DAYS", "7"))           # return window before earnings can be paid out
MIN_PAYOUT = Decimal(os.getenv("PAYOUT_MIN_AMOUNT", "2500"))    # PKR - don't send tiny transfers
PAYMENT_TIMEOUT_MIN = float(os.getenv("PAYMENT_TIMEOUT_MINUTES", "30"))  # unpaid online orders are cancelled after this
DEFAULT_COMMISSION = Decimal("0.10")
ONLINE_METHODS = ("credit_card", "debit_card", "paypal", "bank_transfer")


class PaymentError(Exception):
    pass


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ================================================================== gateways
class SandboxGateway:
    """A local stand-in for a real gateway. The customer is sent to /pay/<session> (our own hosted page),
    enters a test card, and the page posts a callback signed with SANDBOX_WEBHOOK_SECRET - exactly the shape
    of a real gateway's webhook. Test cards: 4242 4242 4242 4242 succeeds, 4000 0000 0000 0002 is declined,
    4000 0000 0000 9995 fails with insufficient funds."""
    name = "sandbox"
    DECLINES = {"4000000000000002": "Card declined by the bank", "4000000000009995": "Insufficient funds"}

    def __init__(self) -> None:
        # A per-install secret: callbacks signed with anything else are rejected.
        self.secret = os.getenv("SANDBOX_WEBHOOK_SECRET") or _local_secret()

    def create_session(self, order_id: int, amount: Decimal) -> tuple[str, str]:
        ref = "sbx_" + secrets.token_urlsafe(18)
        return ref, f"/pay/{ref}"

    def sign(self, payload: bytes) -> str:
        return hmac.new(self.secret.encode(), payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature or "")

    def charge(self, card_number: str) -> tuple[bool, str | None]:
        digits = "".join(ch for ch in card_number if ch.isdigit())
        if len(digits) < 12 or not _luhn_ok(digits):
            return False, "Card number is not valid"
        reason = self.DECLINES.get(digits)
        return reason is None, reason

    def refund(self, session_ref: str, amount: Decimal) -> str:
        return "sbx_rf_" + secrets.token_urlsafe(10)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _local_secret() -> str:
    """A random secret created once per install and kept in data_lake/_state (never committed)."""
    from pathlib import Path
    f = Path(__file__).resolve().parents[1] / "data_lake" / "_state" / "sandbox_webhook_secret"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


GATEWAYS = {"sandbox": SandboxGateway}


def gateway():
    name = os.getenv("PAYMENT_GATEWAY", "sandbox").lower()
    if name not in GATEWAYS:
        raise PaymentError(f"PAYMENT_GATEWAY={name!r} is not available. Installed: {', '.join(GATEWAYS)}")
    return GATEWAYS[name]()


def _refuse_test_money_in_production(gw) -> None:
    """The sandbox accepts any test card: on a real deployment that would hand out goods for free. In production
    online payment needs a real gateway (or an explicit ALLOW_SANDBOX_PAYMENTS=1 for a closed demo)."""
    if (gw.name == "sandbox" and os.getenv("APP_ENV") == "production"
            and os.getenv("ALLOW_SANDBOX_PAYMENTS") != "1"):
        raise PaymentError("Online payment isn't available yet - please choose cash on delivery")


# ================================================================== checkout side
def start_online_payment(cur, order_id: int) -> str:
    """Create a gateway session for an unpaid online order and return the URL to send the customer to."""
    _refuse_test_money_in_production(gateway())
    row = cur.execute("SELECT payment_id, amount, payment_status, payment_method FROM dbo.payments WHERE order_id = ?",
                      order_id).fetchone()
    if row is None:
        raise PaymentError("Order has no payment")
    if row.payment_status == "completed":
        raise PaymentError("This order is already paid")
    if row.payment_method not in ONLINE_METHODS:
        raise PaymentError("This order is paid in cash on delivery")
    status = cur.execute("SELECT status FROM dbo.orders WHERE order_id = ?", order_id).fetchval()
    if status != "pending":
        raise PaymentError("This order can no longer be paid")
    gw = gateway()
    ref, url = gw.create_session(order_id, row.amount)
    cur.execute("UPDATE dbo.payments SET provider = ?, provider_ref = ?, payment_status = 'pending', "
                "failure_reason = NULL, updated_at = ? WHERE payment_id = ?", gw.name, ref, now(), row.payment_id)
    return url


def session_info(cur, session_ref: str) -> dict | None:
    row = cur.execute(
        "SELECT p.order_id, p.amount, p.payment_status, p.payment_method, p.failure_reason, o.status AS order_status, "
        "c.first_name FROM dbo.payments p JOIN dbo.orders o ON o.order_id = p.order_id "
        "JOIN dbo.customers c ON c.customer_id = o.customer_id WHERE p.provider_ref = ?", session_ref).fetchone()
    if row is None:
        return None
    return {"order_id": row.order_id, "amount": float(row.amount), "payment_status": row.payment_status,
            "payment_method": row.payment_method, "failure_reason": row.failure_reason,
            "order_status": row.order_status, "customer_first_name": row.first_name}


def handle_webhook(cur, provider: str, payload: bytes, signature: str) -> dict:
    """Process a gateway callback. Verifies the signature, records the event (idempotent on event_id),
    then marks the payment completed or failed. Safe to call twice with the same event."""
    gw = gateway()
    if provider != gw.name or not gw.verify(payload, signature):
        raise PaymentError("Invalid signature")
    ev = json.loads(payload)
    event_id, etype, ref = ev["id"], ev["type"], ev.get("session_ref")
    if cur.execute("SELECT 1 FROM dbo.payment_events WHERE event_id = ?", event_id).fetchone():
        return {"duplicate": True, "event_id": event_id}
    pay = cur.execute("SELECT payment_id, order_id, amount, payment_status FROM dbo.payments WHERE provider_ref = ?",
                      ref).fetchone()
    ts = now()
    result = "ignored"
    if pay is None:
        result = "unknown session"
    elif etype == "payment.succeeded":
        if money(ev.get("amount", 0)) != money(pay.amount):
            result = "amount mismatch - not accepted"
        elif pay.payment_status != "completed":
            cur.execute("UPDATE dbo.payments SET payment_status = 'completed', paid_at = ?, failure_reason = NULL, "
                        "updated_at = ? WHERE payment_id = ?", ts, ts, pay.payment_id)
            cur.execute("UPDATE dbo.orders SET updated_at = ? WHERE order_id = ?", ts, pay.order_id)  # wakes the live feed
            result = "payment completed"
    elif etype == "payment.failed":
        if pay.payment_status == "pending":
            cur.execute("UPDATE dbo.payments SET failure_reason = ?, updated_at = ? WHERE payment_id = ?",
                        (ev.get("reason") or "Payment failed")[:200], ts, pay.payment_id)
            result = "payment failed - customer can retry"
    cur.execute("INSERT INTO dbo.payment_events (event_id, provider, event_type, provider_ref, order_id, amount, "
                "processed_at, result) VALUES (?,?,?,?,?,?,?,?)",
                event_id, provider, etype, ref, pay.order_id if pay else None,
                money(ev.get("amount", 0)) if ev.get("amount") is not None else None, ts, result)
    return {"event_id": event_id, "result": result, "order_id": pay.order_id if pay else None}


def sandbox_pay(cur, session_ref: str, card_number: str) -> dict:
    """What the sandbox's hosted page does when the customer presses Pay: charge the test card, then send
    ourselves the signed callback a real gateway would send."""
    gw = gateway()
    if gw.name != "sandbox":
        raise PaymentError("The sandbox page is only available with PAYMENT_GATEWAY=sandbox")
    _refuse_test_money_in_production(gw)
    info = session_info(cur, session_ref)
    if info is None:
        raise PaymentError("Payment session not found")
    if info["payment_status"] == "completed":
        return {"ok": True, "order_id": info["order_id"], "already_paid": True}
    if info["order_status"] != "pending":
        raise PaymentError("This order can no longer be paid")
    ok, reason = gw.charge(card_number)
    event = {"id": "evt_" + secrets.token_hex(12), "type": "payment.succeeded" if ok else "payment.failed",
             "session_ref": session_ref, "amount": info["amount"], "reason": reason}
    payload = json.dumps(event).encode()
    result = handle_webhook(cur, "sandbox", payload, gw.sign(payload))
    return {"ok": ok, "reason": reason, "order_id": info["order_id"], "result": result["result"]}


def refund_payment(cur, order_id: int, ts: datetime) -> Decimal:
    """Refund what was actually charged online (cash-on-delivery orders that were never collected need nothing)."""
    pay = cur.execute("SELECT payment_id, amount, refunded_amount, payment_status, provider, provider_ref, payment_method "
                      "FROM dbo.payments WHERE order_id = ?", order_id).fetchone()
    if pay is None:
        return Decimal("0")
    if pay.payment_status == "pending":  # never charged: nothing to give back
        cur.execute("UPDATE dbo.payments SET payment_status = 'failed', failure_reason = COALESCE(failure_reason, "
                    "'Order cancelled before payment'), updated_at = ? WHERE payment_id = ?", ts, pay.payment_id)
        return Decimal("0")
    if pay.payment_status != "completed":
        return Decimal("0")
    to_refund = money(pay.amount) - money(pay.refunded_amount)
    if to_refund <= 0:
        return Decimal("0")
    if pay.provider in GATEWAYS and pay.provider_ref:  # money went through a gateway: refund through it
        GATEWAYS[pay.provider]().refund(pay.provider_ref, to_refund)
    cur.execute("UPDATE dbo.payments SET payment_status = 'refunded', refunded_amount = amount, updated_at = ? "
                "WHERE payment_id = ?", ts, pay.payment_id)
    return to_refund


def awaiting_online_payment(cur, order_id: int) -> bool:
    row = cur.execute("SELECT payment_method, payment_status FROM dbo.payments WHERE order_id = ?", order_id).fetchone()
    return bool(row and row.payment_method in ONLINE_METHODS and row.payment_status == "pending")


def expire_unpaid_orders(cur, ts: datetime, cancel) -> int:
    """Cancel online orders nobody paid for within PAYMENT_TIMEOUT_MIN (puts their stock back)."""
    rows = cur.execute(
        "SELECT o.order_id FROM dbo.orders o JOIN dbo.payments p ON p.order_id = o.order_id "
        "WHERE o.status = 'pending' AND p.payment_status = 'pending' AND p.payment_method IN (?,?,?,?) "
        "AND o.order_date < ?",  # only real checkouts leave online payments pending (the simulator pays instantly)
        *ONLINE_METHODS, ts - timedelta(minutes=PAYMENT_TIMEOUT_MIN)).fetchall()
    for (oid,) in rows:
        cancel(cur, oid, ts=ts)
    return len(rows)


# ================================================================== commission
def commission_rates(cur) -> dict:
    rules = cur.execute("SELECT scope, category_id, vendor_id, rate FROM dbo.commission_rules").fetchall()
    out = {"default": DEFAULT_COMMISSION, "category": {}, "vendor": {}}
    for scope, cat, ven, rate in rules:
        if scope == "default":
            out["default"] = Decimal(rate)
        elif scope == "category":
            out["category"][cat] = Decimal(rate)
        else:
            out["vendor"][ven] = Decimal(rate)
    return out


def rate_for(rates: dict, vendor_id: int, category_id: int) -> Decimal:
    return rates["vendor"].get(vendor_id) or rates["category"].get(category_id) or rates["default"]


def set_rate(cur, scope: str, target_id: int | None, rate: Decimal | None) -> None:
    """Create/update a rule, or remove it when rate is None (the default can't be removed)."""
    if scope not in ("default", "category", "vendor"):
        raise PaymentError("Unknown scope")
    if rate is not None and not (Decimal("0") <= rate <= Decimal("0.5")):
        raise PaymentError("Commission must be between 0% and 50%")
    cat = target_id if scope == "category" else None
    ven = target_id if scope == "vendor" else None
    if scope != "default" and target_id is None:
        raise PaymentError("Pick a category or seller")
    where = "scope = ? AND " + ("category_id IS NULL" if cat is None else "category_id = ?") + " AND " + \
            ("vendor_id IS NULL" if ven is None else "vendor_id = ?")
    params = [scope] + ([cat] if cat is not None else []) + ([ven] if ven is not None else [])
    if rate is None:
        if scope == "default":
            raise PaymentError("The default rate can't be removed")
        cur.execute(f"DELETE FROM dbo.commission_rules WHERE {where}", *params)
        return
    if cur.execute(f"UPDATE dbo.commission_rules SET rate = ?, updated_at = SYSDATETIME() WHERE {where}",
                   rate, *params).rowcount == 0:
        cur.execute("INSERT INTO dbo.commission_rules (scope, category_id, vendor_id, rate) VALUES (?,?,?,?)",
                    scope, cat, ven, rate)


# ================================================================== ledger
def record_delivery(cur, shipment_id: int, ts: datetime, rates: dict | None = None) -> int:
    """A parcel was delivered: credit its seller (sale - commission) for each line. Idempotent: the unique index
    on (order_item_id, entry_type) means a replay inserts nothing."""
    rates = rates or commission_rates(cur)
    lines = cur.execute(
        "SELECT i.order_item_id, i.order_id, i.line_total, s.vendor_id, p.category_id, s.delivered_at "
        "FROM dbo.order_items i JOIN dbo.shipments s ON s.shipment_id = i.shipment_id "
        "JOIN dbo.products p ON p.product_id = i.product_id "
        "WHERE i.shipment_id = ? AND NOT EXISTS (SELECT 1 FROM dbo.ledger_entries l "
        "  WHERE l.order_item_id = i.order_item_id AND l.entry_type = 'sale')", shipment_id).fetchall()
    for item_id, order_id, line_total, vendor_id, category_id, delivered_at in lines:
        rate = rate_for(rates, vendor_id, category_id)
        when = delivered_at or ts
        available = when + timedelta(days=HOLD_DAYS)
        status = "available" if available <= ts else "pending"
        commission = money(Decimal(line_total) * rate)
        for etype, amount in (("sale", money(line_total)), ("commission", -commission)):
            cur.execute(
                "INSERT INTO dbo.ledger_entries (vendor_id, order_id, shipment_id, order_item_id, entry_type, amount, "
                "commission_rate, status, available_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                vendor_id, order_id, shipment_id, item_id, etype, amount, rate, status, available, when, ts)
    return len(lines)


def record_return(cur, order_id: int, ts: datetime) -> int:
    """Order returned: reverse every sale and commission recorded for it (the seller earns nothing on it)."""
    rows = cur.execute(
        "SELECT entry_id, vendor_id, shipment_id, order_item_id, entry_type, amount, commission_rate "
        "FROM dbo.ledger_entries WHERE order_id = ? AND entry_type IN ('sale','commission') "
        "AND NOT EXISTS (SELECT 1 FROM dbo.ledger_entries r WHERE r.order_item_id = ledger_entries.order_item_id "
        "  AND r.entry_type = CASE ledger_entries.entry_type WHEN 'sale' THEN 'sale_reversal' ELSE 'commission_reversal' END)",
        order_id).fetchall()
    for _, vendor_id, shipment_id, item_id, etype, amount, rate in rows:
        cur.execute(
            "INSERT INTO dbo.ledger_entries (vendor_id, order_id, shipment_id, order_item_id, entry_type, amount, "
            "commission_rate, status, available_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            vendor_id, order_id, shipment_id, item_id, etype + "_reversal", -Decimal(amount), rate,
            "available", ts, ts, ts)
    return len(rows)


def release_available(cur, ts: datetime) -> int:
    """Return window over -> earnings can be paid out."""
    return cur.execute("UPDATE dbo.ledger_entries SET status = 'available', updated_at = ? "
                       "WHERE status = 'pending' AND available_at <= ?", ts, ts).rowcount


def vendor_balances(cur, vendor_id: int | None = None) -> list[dict]:
    rows = cur.execute(
        "SELECT v.vendor_id, v.vendor_name, "
        "  COALESCE(SUM(CASE WHEN l.status = 'pending' THEN l.amount END), 0) AS pending, "
        "  COALESCE(SUM(CASE WHEN l.status = 'available' AND l.payout_id IS NULL THEN l.amount END), 0) AS available, "
        "  COALESCE(SUM(CASE WHEN l.payout_id IS NOT NULL AND p.status = 'scheduled' THEN l.amount END), 0) AS scheduled, "
        "  COALESCE(SUM(CASE WHEN l.status = 'paid' THEN l.amount END), 0) AS paid, "
        "  COALESCE(SUM(CASE WHEN l.entry_type IN ('sale','sale_reversal') THEN l.amount END), 0) AS gross_sales, "
        "  COALESCE(-SUM(CASE WHEN l.entry_type IN ('commission','commission_reversal') THEN l.amount END), 0) AS commission "
        "FROM dbo.vendors v LEFT JOIN dbo.ledger_entries l ON l.vendor_id = v.vendor_id "
        "LEFT JOIN dbo.payouts p ON p.payout_id = l.payout_id "
        + ("WHERE v.vendor_id = ? " if vendor_id else "") +
        "GROUP BY v.vendor_id, v.vendor_name ORDER BY v.vendor_name", *([vendor_id] if vendor_id else [])).fetchall()
    cols = ["vendor_id", "vendor_name", "pending", "available", "scheduled", "paid", "gross_sales", "commission"]
    return [{c: (float(v) if isinstance(v, Decimal) else v) for c, v in zip(cols, r)} for r in rows]


# ================================================================== payouts
def run_payouts(cur, ts: datetime) -> list[dict]:
    """Group every seller's available, not-yet-paid entries into one scheduled payout (if above the minimum)."""
    created = []
    for vendor_id, total, n in cur.execute(
            "SELECT vendor_id, SUM(amount), COUNT(*) FROM dbo.ledger_entries "
            "WHERE status = 'available' AND payout_id IS NULL GROUP BY vendor_id").fetchall():
        if Decimal(total) < MIN_PAYOUT:
            continue
        pid = cur.execute("INSERT INTO dbo.payouts (vendor_id, amount, entry_count, status, created_at, updated_at) "
                          "OUTPUT INSERTED.payout_id VALUES (?,?,?, 'scheduled', ?, ?)",
                          vendor_id, money(total), n, ts, ts).fetchval()
        cur.execute("UPDATE dbo.ledger_entries SET payout_id = ?, updated_at = ? "
                    "WHERE vendor_id = ? AND status = 'available' AND payout_id IS NULL", pid, ts, vendor_id)
        created.append({"payout_id": pid, "vendor_id": vendor_id, "amount": float(money(total)), "entries": n})
    return created


def mark_payout_paid(cur, payout_id: int, reference: str, ts: datetime) -> None:
    reference = (reference or "").strip()
    if not reference:
        raise PaymentError("Enter the bank transfer reference")
    if cur.execute("UPDATE dbo.payouts SET status = 'paid', reference = ?, paid_at = ?, updated_at = ? "
                   "WHERE payout_id = ? AND status = 'scheduled'", reference[:100], ts, ts, payout_id).rowcount == 0:
        raise PaymentError("Payout not found or already paid")
    cur.execute("UPDATE dbo.ledger_entries SET status = 'paid', updated_at = ? WHERE payout_id = ?", ts, payout_id)
