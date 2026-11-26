"""Accounts, logins and role checks.

- Passwords: scrypt with a random salt per user (source_app/passwords.py).
- Sessions: a random 256-bit token in an httpOnly, SameSite=Lax cookie. The database only stores the
  token's SHA-256, so a leaked user_sessions table can't be used to log in. Sessions last 7 days and
  are deleted on logout.
- Brute force: 5 failed logins for an email locks it for 5 minutes, and 30 failures from one address lock that
  address for 15 minutes (in memory - fine for one server). Unknown emails take as long as known ones, so the
  timing doesn't reveal who has an account. Failures and lockouts go to the "shop.security" log.
- Admin sessions last 12 hours; everyone else stays signed in for 7 days.
- In production (APP_ENV=production) the demo accounts (@raunaqi.test, published passwords) can't sign in.
- Roles: customer, vendor, rider, admin. Routes declare who may call them with Depends(require(...)).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, Response

from . import shop_db as db
from source_app.passwords import hash_password, verify_password  # noqa: E402

COOKIE = "shoplite_session"
SESSION_DAYS = 7
ADMIN_SESSION_HOURS = 12
MAX_FAILS, LOCK_SECONDS = 5, 300            # per email
MAX_IP_FAILS, IP_LOCK_SECONDS = 30, 900     # per client address: slows spraying one password across many emails
_fails: dict[str, list[float]] = {}
_ip_fails: dict[str, list[float]] = {}
DEMO_DOMAIN = "@raunaqi.test"
security_log = logging.getLogger("shop.security")
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))   # checked for unknown emails, so they cost the same time

USER_SQL = """
    SELECT u.user_id, u.email, u.role, u.display_name, u.customer_id, u.vendor_id, u.rider_id, u.is_active,
           v.vendor_name, r.full_name AS rider_name
    FROM dbo.users u
    LEFT JOIN dbo.vendors v ON v.vendor_id = u.vendor_id
    LEFT JOIN dbo.riders r ON r.rider_id = u.rider_id
"""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _recent(store: dict[str, list[float]], key: str, window: float) -> list[float]:
    now = time.time()
    if len(store) > 50_000:   # keep memory bounded under a flood of made-up emails/addresses
        for k in [k for k, v in store.items() if not v or now - v[-1] > window]:
            store.pop(k, None)
    recent = [t for t in store.get(key, []) if now - t < window]
    if recent:
        store[key] = recent
    else:
        store.pop(key, None)
    return recent


def _locked(email: str, ip: str | None = None) -> bool:
    return (len(_recent(_fails, email, LOCK_SECONDS)) >= MAX_FAILS
            or (ip is not None and len(_recent(_ip_fails, ip, IP_LOCK_SECONDS)) >= MAX_IP_FAILS))


def _failed(email: str, ip: str | None, reason: str) -> None:
    _fails.setdefault(email, []).append(time.time())
    if ip is not None:
        _ip_fails.setdefault(ip, []).append(time.time())
    security_log.warning("login_failed reason=%s email=%s ip=%s", reason, email[:80], ip)


def login(email: str, password: str, response: Response, expected_role: str | None = None,
          ip: str | None = None) -> dict:
    email = email.strip().lower()
    if _locked(email, ip):
        security_log.warning("login_locked email=%s ip=%s", email[:80], ip)
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    row = db.query_one("SELECT user_id, password_hash, is_active, role FROM dbo.users WHERE email = ?", email)
    if os.getenv("APP_ENV") == "production" and email.endswith(DEMO_DOMAIN):
        row = None   # demo accounts have published passwords: never usable on a real deployment
    if not verify_password(password, row["password_hash"] if row else _DUMMY_HASH) or row is None:
        _failed(email, ip, "bad_credentials")
        raise HTTPException(401, "Wrong email or password")  # same message either way: don't reveal which emails exist
    if not row["is_active"]:
        security_log.warning("login_disabled_account email=%s ip=%s", email[:80], ip)
        raise HTTPException(403, "This account is disabled")
    # Admins sign in only on the separate admin page (/admin/login), and that page only accepts admins.
    # Either mismatch gets the same answer as a wrong password, so neither page reveals that the other kind
    # of account exists.
    if (row["role"] == "admin") != (expected_role == "admin"):
        _failed(email, ip, "wrong_portal")
        raise HTTPException(401, "Wrong email or password")
    if expected_role and row["role"] != expected_role:  # public portal, wrong type: point them to the right one
        names = {"customer": "Shopper", "vendor": "Seller", "rider": "Rider"}
        raise HTTPException(403, f"This is a {names[row['role']]} account - choose “{names[row['role']]}” to sign in")
    _fails.pop(email, None)
    token = secrets.token_urlsafe(32)
    now = datetime.now().replace(microsecond=0)
    lifetime = timedelta(hours=ADMIN_SESSION_HOURS) if row["role"] == "admin" else timedelta(days=SESSION_DAYS)
    db.execute("DELETE FROM dbo.user_sessions WHERE user_id = ? AND expires_at < ?", row["user_id"], now)  # tidy up
    db.execute("INSERT INTO dbo.user_sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
               _token_hash(token), row["user_id"], now, now + lifetime)
    db.execute("UPDATE dbo.users SET last_login_at = ? WHERE user_id = ?", now, row["user_id"])
    if row["role"] == "admin":
        security_log.info("admin_login user_id=%s ip=%s", row["user_id"], ip)
    response.set_cookie(COOKIE, token, max_age=int(lifetime.total_seconds()), httponly=True, samesite="lax", path="/",
                        secure=os.getenv("APP_ENV") == "production")  # HTTPS-only cookie in production
    return user_by_id(row["user_id"])


def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.execute("DELETE FROM dbo.user_sessions WHERE token_hash = ?", _token_hash(token))
    response.delete_cookie(COOKIE, path="/")


def register_customer(first: str, last: str, email: str, password: str, city: str, country: str,
                      phone: str, response: Response) -> dict:
    first, last, email = first.strip(), last.strip(), email.strip().lower()
    if not (first and last and "@" in email and "." in email.split("@")[-1]):
        raise HTTPException(400, "First name, last name and a valid email are required")
    if len(password) < 8 or password.isalpha() or password.isdigit():
        raise HTTPException(400, "Password must be at least 8 characters and mix letters with numbers or symbols")
    if db.scalar("SELECT COUNT(*) FROM dbo.users WHERE email = ?", email) or \
       db.scalar("SELECT COUNT(*) FROM dbo.customers WHERE email = ?", email):
        raise HTTPException(400, "An account with this email already exists - try signing in")
    now = datetime.now().replace(microsecond=0)
    conn = db.get_connection()
    try:  # customer + login in one transaction: never one without the other
        cur = conn.cursor()
        cid = cur.execute(
            "INSERT INTO dbo.customers (first_name, last_name, email, phone, city, country, signup_date, created_at, "
            "updated_at) OUTPUT INSERTED.customer_id VALUES (?,?,?,?,?,?,?,?,?)",
            first, last, email, phone.strip() or None, city.strip() or None, country.strip() or None, now.date(), now, now,
        ).fetchval()
        cur.execute(
            "INSERT INTO dbo.users (email, password_hash, role, display_name, customer_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", email, hash_password(password), "customer", f"{first} {last}", cid, now, now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return login(email, password, response)


def _check_new_login(email: str, password: str) -> str:
    email = email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Please enter a valid email")
    if len(password) < 8 or password.isalpha() or password.isdigit():
        raise HTTPException(400, "Password must be at least 8 characters and mix letters with numbers or symbols")
    if db.scalar("SELECT COUNT(*) FROM dbo.users WHERE email = ?", email):
        raise HTTPException(400, "An account with this email already exists - try signing in")
    return email


def register_vendor(store_name: str, owner_name: str, email: str, password: str, city: str, country: str,
                    tagline: str, response: Response) -> dict:
    """A new seller opens a store. It starts with autopilot OFF: the seller handles orders in the seller center."""
    store_name, owner_name = store_name.strip(), owner_name.strip()
    if len(store_name) < 3 or not owner_name:
        raise HTTPException(400, "Store name (3+ characters) and your name are required")
    email = _check_new_login(email, password)
    if db.scalar("SELECT COUNT(*) FROM dbo.vendors WHERE vendor_name = ?", store_name):
        raise HTTPException(400, "That store name is taken - try another")
    now = datetime.now().replace(microsecond=0)
    with db.tx() as cur:
        vid = cur.execute(
            "INSERT INTO dbo.vendors (vendor_name, tagline, city, country, rating, is_bot, prep_seconds, created_at, "
            "updated_at) OUTPUT INSERTED.vendor_id VALUES (?,?,?,?,5.0,0,30,?,?)",
            store_name, tagline.strip() or None, city.strip() or None, country.strip() or None, now, now,
        ).fetchval()
        cur.execute(
            "INSERT INTO dbo.users (email, password_hash, role, display_name, vendor_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", email, hash_password(password), "vendor", store_name, vid, now, now,
        )
    return login(email, password, response)


def register_rider(full_name: str, email: str, password: str, phone: str, city: str, vehicle: str,
                   response: Response) -> dict:
    """A new rider joins. They start offline and go online in the rider app when ready."""
    full_name = full_name.strip()
    if not full_name:
        raise HTTPException(400, "Your name is required")
    if vehicle not in ("bike", "car", "van"):
        raise HTTPException(400, "Vehicle must be bike, car or van")
    email = _check_new_login(email, password)
    now = datetime.now().replace(microsecond=0)
    with db.tx() as cur:
        rid = cur.execute(
            "INSERT INTO dbo.riders (full_name, phone, city, vehicle, status, is_bot, created_at, updated_at) "
            "OUTPUT INSERTED.rider_id VALUES (?,?,?,?, 'offline', 0, ?, ?)",
            full_name, phone.strip() or None, city.strip() or None, vehicle, now, now,
        ).fetchval()
        cur.execute(
            "INSERT INTO dbo.users (email, password_hash, role, display_name, rider_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", email, hash_password(password), "rider", full_name, rid, now, now,
        )
    return login(email, password, response)


def user_by_id(user_id: int) -> dict | None:
    return db.query_one(USER_SQL + " WHERE u.user_id = ?", user_id)


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    user = db.query_one(
        USER_SQL + " JOIN dbo.user_sessions s ON s.user_id = u.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > SYSDATETIME() AND u.is_active = 1", _token_hash(token),
    )
    return user


def require(*roles: str):
    """Dependency factory: Depends(require('admin')) -> the user dict, or 401/403."""
    def dep(user: dict | None = Depends(current_user)) -> dict:
        if user is None:
            raise HTTPException(401, "Please sign in")
        if roles and user["role"] not in roles:
            raise HTTPException(403, "You don't have access to this")
        return user
    return dep
