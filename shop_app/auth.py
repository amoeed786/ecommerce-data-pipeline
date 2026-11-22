"""Accounts, logins and role checks.

- Passwords: scrypt with a random salt per user (source_app/passwords.py).
- Sessions: a random 256-bit token in an httpOnly, SameSite=Lax cookie. The database only stores the
  token's SHA-256, so a leaked user_sessions table can't be used to log in. Sessions last 7 days and
  are deleted on logout.
- Brute force: 5 failed logins for an email locks it for 5 minutes (in memory - fine for one server).
- Roles: customer, vendor, rider, admin. Routes declare who may call them with Depends(require(...)).
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, Response

from . import shop_db as db
from source_app.passwords import hash_password, verify_password  # noqa: E402

COOKIE = "shoplite_session"
SESSION_DAYS = 7
MAX_FAILS, LOCK_SECONDS = 5, 300
_fails: dict[str, list[float]] = {}

USER_SQL = """
    SELECT u.user_id, u.email, u.role, u.display_name, u.customer_id, u.vendor_id, u.rider_id, u.is_active,
           v.vendor_name, r.full_name AS rider_name
    FROM dbo.users u
    LEFT JOIN dbo.vendors v ON v.vendor_id = u.vendor_id
    LEFT JOIN dbo.riders r ON r.rider_id = u.rider_id
"""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _locked(email: str) -> bool:
    recent = [t for t in _fails.get(email, []) if time.time() - t < LOCK_SECONDS]
    _fails[email] = recent
    return len(recent) >= MAX_FAILS


def login(email: str, password: str, response: Response, expected_role: str | None = None) -> dict:
    email = email.strip().lower()
    if _locked(email):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    row = db.query_one("SELECT user_id, password_hash, is_active, role FROM dbo.users WHERE email = ?", email)
    if row is None or not verify_password(password, row["password_hash"]):
        _fails.setdefault(email, []).append(time.time())
        raise HTTPException(401, "Wrong email or password")  # same message either way: don't reveal which emails exist
    if not row["is_active"]:
        raise HTTPException(403, "This account is disabled")
    if expected_role and row["role"] != expected_role:  # signed in on the wrong portal - point them to the right one
        names = {"customer": "Shopper", "vendor": "Seller", "rider": "Rider", "admin": "Admin"}
        raise HTTPException(403, f"This is a {names[row['role']]} account - choose “{names[row['role']]}” to sign in")
    _fails.pop(email, None)
    token = secrets.token_urlsafe(32)
    now = datetime.now().replace(microsecond=0)
    db.execute("INSERT INTO dbo.user_sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
               _token_hash(token), row["user_id"], now, now + timedelta(days=SESSION_DAYS))
    db.execute("UPDATE dbo.users SET last_login_at = ? WHERE user_id = ?", now, row["user_id"])
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/")
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
