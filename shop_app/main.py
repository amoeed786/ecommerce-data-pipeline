r"""ShopLite - a small marketplace on ShopDB, with customers, vendors, riders and admins.

Run from the project folder:
    .\.venv\Scripts\uvicorn.exe shop_app.main:app --port 8000
Then open http://localhost:8000  (demo logins are on the sign-in page)

Pages
    /            Storefront     browse by category, search, product pages, cart, checkout, order tracking
    /login       Sign in / create an account (customers)
    /vendor      Vendor portal  accept and pack orders, manage products and stock, autopilot switch
    /rider       Rider portal   go online, pick up and deliver, earnings
    /admin       Admin          order control (start orders / auto-approve), live shipment board,
                                vendors, riders, users, catalog, simulator + pipeline
    /dashboard   Admin          live operations (reads ShopDB directly)
    /analytics   Admin          business analytics from the gold layer
    /docs        Interactive API documentation

Background tasks: the live change feed (SSE) and the fulfillment autopilot (source_app/marketplace.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import duckdb
import secrets

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

from . import auth
from . import shop_db as db
from .live import feed

mp = db.mp
log = logging.getLogger("shop")
ROOT = db.PROJECT_ROOT
STATIC = Path(__file__).parent / "static"
LAKE = ROOT / "data_lake"
STATE = LAKE / "_state"
GOLD = LAKE / "gold"
SIM_LOG = STATE / "simulator.log"
VENV_BIN = Path(sys.executable).parent
TABLES = ["categories", "customers", "products", "inventory", "orders", "order_items", "payments",
          "vendors", "riders", "shipments", "users"]
PIPELINE_STEPS = {  # step -> (command shown to the user, argv)
    "extract": ("python ingestion/extract.py", [sys.executable, "ingestion/extract.py"]),
    "check_bronze": ("python ingestion/check_bronze.py", [sys.executable, "ingestion/check_bronze.py"]),
    "silver": ("python transform/silver.py", [sys.executable, "transform/silver.py"]),
    "dbt_build": ("dbt build --project-dir dbt_shop --profiles-dir dbt_shop",
                  [str(VENV_BIN / "dbt"), "build", "--project-dir", "dbt_shop", "--profiles-dir", "dbt_shop"]),
    "export_gold": ("python transform/export_gold.py", [sys.executable, "transform/export_gold.py"]),
    "inject_bad_data": ("python source_app/inject_bad_data.py", [sys.executable, "source_app/inject_bad_data.py"]),
    "check_db": ("python source_app/check_db.py", [sys.executable, "source_app/check_db.py"]),
}
simulator: dict = {"proc": None, "interval": None}


def _admin_path() -> str:
    """ADMIN_PATH from .env (default /admin): where the admin area lives, e.g. /kartora-control-7f3a.
    Changing it moves the admin sign-in and pages; the public site never links to it."""
    path = "/" + os.getenv("ADMIN_PATH", "/admin").strip().strip("/")
    reserved = ("/login", "/vendor", "/rider", "/api", "/static", "/docs", "/redoc", "/openapi.json")
    if path == "/" or not re.fullmatch(r"(/[A-Za-z0-9_-]+)+", path) or path.lower().startswith(reserved):
        raise RuntimeError(f"ADMIN_PATH={path!r} is not allowed: use letters, digits, - or _ (e.g. /kartora-control) "
                           f"and don't reuse {', '.join(reserved)}")
    return path


ADMIN_PATH = _admin_path()
engine_state: dict = {"last_tick": None, "last_actions": {}, "error": None}


async def run_engine() -> None:
    """Fulfillment autopilot: every 2 seconds, let bot vendors/riders (and auto-approve) move orders."""
    def tick():
        conn = db.get_connection()
        try:
            return mp.engine_tick(conn)
        finally:
            conn.close()
    while True:
        try:
            done = await asyncio.to_thread(tick)
            engine_state.update(last_tick=mp.now().isoformat(), error=None)
            if done:
                engine_state["last_actions"] = done
        except Exception as exc:  # keep running through e.g. a DB restart
            engine_state["error"] = str(exc)
            log.exception("fulfillment engine tick failed")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(feed.run()), asyncio.create_task(run_engine())]
    asyncio.get_running_loop().run_in_executor(None, db.warm_catalog)  # first visitor gets a ready catalog
    yield
    for t in tasks:
        t.cancel()
    if simulator["proc"] and simulator["proc"].poll() is None:
        simulator["proc"].terminate()


app = FastAPI(title="ShopLite", description="Marketplace on ShopDB: customers, vendors, riders, admins",
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# Compress HTML/JS/CSS/JSON (product lists shrink ~5x). Starlette leaves text/event-stream (live feed) alone.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """Photos never change under the same name (seller uploads get a new random name each time), so browsers
    may keep them for a week. Pages, scripts and API responses keep revalidating so updates show at once."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/static/img/", "/static/uploads/")):
        response.headers["Cache-Control"] = "public, max-age=604800"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # use the copy only after checking it's still current (ETag)
    return response


@app.exception_handler(mp.MarketplaceError)
async def shop_error(_: Request, exc: mp.MarketplaceError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(mp.pay.PaymentError)
async def payment_error(_: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ================================================================== pages
def page(file: str, *roles: str):
    """Serve a page; if it needs a role the visitor doesn't have, send them to the right sign-in page
    (admin pages -> the separate /admin/login, everything else -> the public /login)."""
    def handler(request: Request, user: dict | None = Depends(auth.current_user)):
        if roles and (user is None or user["role"] not in roles):
            login = f"{ADMIN_PATH}/login" if "admin" in roles else "/login"
            return RedirectResponse(f"{login}?next={request.url.path}", status_code=303)
        return FileResponse(STATIC / file)
    return handler


for path, file, roles in [
    # public site
    ("/", "store.html", ()), ("/login", "login.html", ()),
    ("/vendor", "vendor.html", ("vendor",)), ("/rider", "rider.html", ("rider",)),
    # admin area - lives under ADMIN_PATH (.env), with its own sign-in page, never linked from the public site
    (f"{ADMIN_PATH}/login", "admin_login.html", ()),
    (ADMIN_PATH, "admin.html", ("admin",)), (f"{ADMIN_PATH}/dashboard", "dashboard.html", ("admin",)),
    (f"{ADMIN_PATH}/analytics", "analytics.html", ("admin",)),
]:
    app.add_api_route(path, page(file, *roles), methods=["GET"], include_in_schema=False)

if ADMIN_PATH == "/admin":  # old bookmarks - only with the default path, so a custom path is never revealed
    for old, new in [("/dashboard", "/admin/dashboard"), ("/analytics", "/admin/analytics")]:
        app.add_api_route(old, (lambda n: lambda: RedirectResponse(n, status_code=308))(new), methods=["GET"],
                          include_in_schema=False)


# ================================================================== auth
class Login(BaseModel):
    email: str
    password: str
    role: str | None = None  # the portal chosen on the sign-in page; a mismatch gets a helpful error


class RegisterVendor(BaseModel):
    store_name: str
    owner_name: str
    email: str
    password: str
    city: str = ""
    country: str = ""
    tagline: str = ""


class RegisterRider(BaseModel):
    full_name: str
    email: str
    password: str
    phone: str = ""
    city: str = ""
    vehicle: str = "bike"


class Register(BaseModel):
    first_name: str
    last_name: str
    email: str
    password: str
    phone: str = ""
    city: str = ""
    country: str = ""


@app.post("/api/auth/login", tags=["auth"])
def login(body: Login, response: Response):
    return auth.login(body.email, body.password, response, expected_role=body.role)


@app.post("/api/auth/register/vendor", tags=["auth"])
def register_vendor(body: RegisterVendor, response: Response):
    return auth.register_vendor(body.store_name, body.owner_name, body.email, body.password,
                                body.city, body.country, body.tagline, response)


@app.post("/api/auth/register/rider", tags=["auth"])
def register_rider(body: RegisterRider, response: Response):
    return auth.register_rider(body.full_name, body.email, body.password, body.phone, body.city,
                               body.vehicle, response)


@app.post("/api/auth/register", tags=["auth"])
def register(body: Register, response: Response):
    return auth.register_customer(body.first_name, body.last_name, body.email, body.password,
                                  body.city, body.country, body.phone, response)


@app.post("/api/auth/logout", tags=["auth"])
def logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}


@app.get("/api/auth/me", tags=["auth"])
def me(user: dict | None = Depends(auth.current_user)):
    if user and user["role"] == "admin":
        user = {**user, "admin_path": ADMIN_PATH}  # only signed-in admins learn where the admin area is
    return user  # null when signed out


# ================================================================== storefront (public)
@app.get("/api/home", tags=["store"])
def home():
    return db.home()


@app.get("/api/categories", tags=["store"])
def categories():
    return db.categories()


@app.get("/api/products", tags=["store"])
def products(category_id: int | None = None, q: str | None = None, vendor_id: int | None = None,
             min_price: float | None = None, max_price: float | None = None, min_rating: float | None = None,
             on_sale: bool = False, in_stock: bool = False, sort: str = "popular",
             limit: int = Query(40, le=100), offset: int = Query(0, ge=0)):
    return db.search_products(category_id, (q or "").strip() or None, vendor_id, min_price, max_price,
                              min_rating, on_sale, in_stock, sort, limit, offset)


@app.get("/api/products/{product_id}", tags=["store"])
def product(product_id: int):
    p = db.product_detail(product_id)
    if p is None:
        raise HTTPException(404, "Product not found")
    return p


@app.get("/api/vendors/{vendor_id}", tags=["store"])
def vendor_store(vendor_id: int):
    v = db.vendor_public(vendor_id)
    if v is None:
        raise HTTPException(404, "Store not found")
    return v


# ================================================================== customer
customer = auth.require("customer")


class CartLine(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=mp.MAX_QTY_PER_LINE)


class Checkout(BaseModel):
    items: list[CartLine]
    payment_method: str = "cash_on_delivery"
    coupon: str = ""
    ship_city: str = ""
    ship_country: str = ""


class Profile(BaseModel):
    phone: str = ""
    city: str = ""
    country: str = ""


@app.get("/api/me/profile", tags=["customer"])
def my_profile(user: dict = Depends(customer)):
    return db.customer_profile(user["customer_id"])


@app.put("/api/me/profile", tags=["customer"])
def update_my_profile(body: Profile, user: dict = Depends(customer)):
    db.update_profile(user["customer_id"], body.phone.strip(), body.city.strip(), body.country.strip())
    return db.customer_profile(user["customer_id"])


@app.post("/api/orders", tags=["customer"])
def checkout(body: Checkout, user: dict = Depends(customer)):
    """Place the order. Online methods return a payment_url: the customer pays on the gateway's page, and the
    order only goes to the stores once the gateway confirms (or is cancelled if never paid)."""
    with db.tx() as cur:
        res = mp.create_order(cur, user["customer_id"], [(i.product_id, i.quantity) for i in body.items],
                              body.payment_method, body.coupon, ship_city=body.ship_city.strip() or None,
                              ship_country=body.ship_country.strip() or None, pay_online_via_gateway=True)
        if res["needs_online_payment"]:
            res["payment_url"] = mp.pay.start_online_payment(cur, res["order_id"])
    db.clear_catalog_cache()  # stock changed
    return res


@app.post("/api/me/orders/{order_id}/pay", tags=["customer"])
def pay_my_order(order_id: int, user: dict = Depends(customer)):
    """Start (or retry) the online payment for one of your unpaid orders."""
    _my_order(order_id, user)
    with db.tx() as cur:
        return {"payment_url": mp.pay.start_online_payment(cur, order_id)}


# ---- hosted payment page (sandbox gateway) + gateway callbacks
class SandboxCard(BaseModel):
    card_number: str = Field(min_length=12, max_length=23)
    expiry: str = Field(pattern=r"^\s*(0[1-9]|1[0-2])\s*/\s*\d{2}\s*$")
    cvc: str = Field(pattern=r"^\d{3,4}$")
    name: str = Field(min_length=2, max_length=80)


@app.get("/pay/{session_ref}", include_in_schema=False)
def payment_page(session_ref: str):
    return FileResponse(STATIC / "pay.html")  # the session ref is long and random: knowing it is the access


@app.get("/api/pay/{session_ref}", tags=["payments"])
def payment_session(session_ref: str):
    with db.tx() as cur:
        info = mp.pay.session_info(cur, session_ref)
    if info is None:
        raise HTTPException(404, "Payment session not found")
    return {**info, "gateway": os.getenv("PAYMENT_GATEWAY", "sandbox")}


@app.post("/api/pay/{session_ref}/sandbox", tags=["payments"])
def sandbox_pay(session_ref: str, body: SandboxCard):
    """The sandbox gateway's 'Pay' button. Card details are only checked here, never stored or logged."""
    with db.tx() as cur:
        return mp.pay.sandbox_pay(cur, session_ref, body.card_number)


@app.post("/api/webhooks/{provider}", tags=["payments"])
async def payment_webhook(provider: str, request: Request):
    """Where a real gateway sends its signed result. The signature is checked before anything happens, and a
    repeated event is recognised and ignored."""
    payload = await request.body()
    signature = request.headers.get("X-Signature", "")
    try:
        with db.tx() as cur:
            return mp.pay.handle_webhook(cur, provider, payload, signature)
    except mp.pay.PaymentError as e:
        raise HTTPException(400, str(e))


@app.get("/api/me/orders", tags=["customer"])
def my_orders(user: dict = Depends(customer)):
    return db.customer_orders(user["customer_id"])


def _my_order(order_id: int, user: dict) -> dict:
    o = db.order_detail(order_id)
    if o is None or o["customer_id"] != user["customer_id"]:
        raise HTTPException(404, "Order not found")  # 404, not 403: don't confirm other people's order ids
    return o


@app.get("/api/me/orders/{order_id}", tags=["customer"])
def my_order(order_id: int, user: dict = Depends(customer)):
    o = _my_order(order_id, user)
    for s in o["shipments"]:  # customers see the rider's first name only
        s.pop("rider_phone", None)
        s["rider_name"] = (s["rider_name"] or "").split(" ")[0] or None
    return o


@app.post("/api/me/orders/{order_id}/cancel", tags=["customer"])
def cancel_my_order(order_id: int, user: dict = Depends(customer)):
    with db.tx() as cur:
        mp.cancel_order(cur, order_id, customer_id=user["customer_id"])
    return {"ok": True}


@app.post("/api/me/orders/{order_id}/return", tags=["customer"])
def return_my_order(order_id: int, user: dict = Depends(customer)):
    with db.tx() as cur:
        mp.return_order(cur, order_id, customer_id=user["customer_id"])
    return {"ok": True}


# ================================================================== vendor portal
vendor = auth.require("vendor")


class ProductPatch(BaseModel):
    unit_price: float | None = None
    list_price: float | None = None
    add_stock: int | None = None
    is_active: bool | None = None


class NewProduct(BaseModel):
    product_name: str
    category_id: int
    unit_price: float = Field(gt=0)
    unit_cost: float = Field(ge=0)
    stock: int = Field(ge=0, le=100_000)
    description: str = ""


class Toggle(BaseModel):
    on: bool


@app.get("/api/vendor/summary", tags=["vendor"])
def vendor_summary(user: dict = Depends(vendor)):
    return db.vendor_summary(user["vendor_id"])


@app.get("/api/vendor/shipments", tags=["vendor"])
def vendor_shipments(status: list[str] = Query(default=["confirmed", "packing", "ready", "assigned"]),
                     user: dict = Depends(vendor)):
    return db.vendor_shipments(user["vendor_id"], status)


@app.post("/api/vendor/shipments/{shipment_id}/{action}", tags=["vendor"])
def vendor_action(shipment_id: int, action: str, user: dict = Depends(vendor)):
    fn = {"accept": mp.vendor_accept, "ready": mp.vendor_ready}.get(action)
    if fn is None:
        raise HTTPException(404, "Unknown action")
    with db.tx() as cur:
        fn(cur, shipment_id, vendor_id=user["vendor_id"])
    return {"ok": True}


class Bulk(BaseModel):
    shipment_ids: list[int] = Field(min_length=1, max_length=200)
    action: str  # accept | ready | accept_ready


@app.post("/api/vendor/shipments/bulk", tags=["vendor"])
def vendor_bulk(body: Bulk, user: dict = Depends(vendor)):
    return db.bulk_vendor_action(user["vendor_id"], body.shipment_ids, body.action)


UPLOADS = STATIC / "uploads" / "products"
IMAGE_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"RIFF": ".webp"}
MAX_IMAGE_BYTES = 3 * 1024 * 1024


async def save_product_image(product_id: int, file: UploadFile) -> str:
    """Store an uploaded product photo. The file type is decided by its bytes, not its name or the
    browser's claim, and the stored name is random - so an upload can never become a script or overwrite another."""
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "Photo too large - 3 MB max")
    ext = next((e for magic, e in IMAGE_TYPES.items() if data.startswith(magic)), None)
    if ext is None or (ext == ".webp" and data[8:12] != b"WEBP"):
        raise HTTPException(400, "Please upload a JPG, PNG or WebP photo")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = f"{product_id}-{secrets.token_hex(8)}{ext}"
    (UPLOADS / name).write_bytes(data)
    url = f"/static/uploads/products/{name}"
    db.set_product_image(product_id, url)
    return url


@app.post("/api/vendor/products/{product_id}/image", tags=["vendor"])
async def vendor_product_image(product_id: int, file: UploadFile = File(...), user: dict = Depends(vendor)):
    if not db.vendor_owns_product(user["vendor_id"], product_id):
        raise HTTPException(404, "Product not found in your store")
    return {"image_url": await save_product_image(product_id, file)}


@app.get("/api/vendor/products", tags=["vendor"])
def vendor_products(user: dict = Depends(vendor)):
    return db.vendor_products(user["vendor_id"])


@app.patch("/api/vendor/products/{product_id}", tags=["vendor"])
def vendor_update_product(product_id: int, body: ProductPatch, user: dict = Depends(vendor)):
    if not db.vendor_owns_product(user["vendor_id"], product_id):
        raise HTTPException(404, "Product not found in your store")
    db.update_product(product_id, body.unit_price, body.list_price, body.add_stock, body.is_active)
    return {"ok": True}


@app.post("/api/vendor/products", tags=["vendor"])
def vendor_add_product(body: NewProduct, user: dict = Depends(vendor)):
    pid = db.add_product(user["vendor_id"], body.product_name, body.category_id, body.unit_price,
                         body.unit_cost, body.stock, body.description)
    return {"product_id": pid}


@app.get("/api/vendor/earnings", tags=["vendor"])
def vendor_earnings(user: dict = Depends(vendor)):
    return db.vendor_earnings(user["vendor_id"])


@app.post("/api/vendor/autopilot", tags=["vendor"])
def vendor_autopilot(body: Toggle, user: dict = Depends(vendor)):
    db.set_flag("vendors", "vendor_id", user["vendor_id"], "is_bot", int(body.on))
    return db.vendor_summary(user["vendor_id"])


# ================================================================== rider portal
rider = auth.require("rider")


class RiderStatus(BaseModel):
    status: str


@app.get("/api/rider/summary", tags=["rider"])
def rider_summary(user: dict = Depends(rider)):
    return db.rider_summary(user["rider_id"])


@app.get("/api/rider/jobs", tags=["rider"])
def rider_jobs(active: bool = True, user: dict = Depends(rider)):
    return db.rider_jobs(user["rider_id"], active)


@app.post("/api/rider/status", tags=["rider"])
def rider_status(body: RiderStatus, user: dict = Depends(rider)):
    db.set_rider_status(user["rider_id"], body.status)
    return db.rider_summary(user["rider_id"])


@app.post("/api/rider/jobs/{shipment_id}/{action}", tags=["rider"])
def rider_action(shipment_id: int, action: str, user: dict = Depends(rider)):
    fn = {"pickup": mp.rider_pickup, "deliver": mp.rider_deliver}.get(action)
    if fn is None:
        raise HTTPException(404, "Unknown action")
    with db.tx() as cur:
        fn(cur, shipment_id, rider_id=user["rider_id"])
    return {"ok": True}


# ================================================================== admin
admin = auth.require("admin")


class SettingsBody(BaseModel):
    auto_approve: bool | None = None
    sim_speed: float | None = Field(default=None, ge=0.1, le=100)


class Flags(BaseModel):
    is_bot: bool | None = None
    is_active: bool | None = None
    status: str | None = None


class Assign(BaseModel):
    rider_id: int


@app.get("/api/admin/overview", tags=["admin"])
def admin_overview(_: dict = Depends(admin)):
    return {**db.admin_overview(), "engine": engine_state}


@app.put("/api/admin/settings", tags=["admin"])
def admin_settings(body: SettingsBody, _: dict = Depends(admin)):
    with db.tx() as cur:
        if body.auto_approve is not None:
            mp.set_setting(cur, "auto_approve", "true" if body.auto_approve else "false")
        if body.sim_speed is not None:
            mp.set_setting(cur, "sim_speed", str(body.sim_speed))
    return db.settings()


@app.get("/api/admin/orders", tags=["admin"])
def admin_orders(status: list[str] = Query(default=["pending", "processing", "shipped"]),
                 limit: int = Query(100, le=500), _: dict = Depends(admin)):
    return db.order_queue(status, limit)


@app.get("/api/admin/orders/{order_id}", tags=["admin"])
def admin_order(order_id: int, _: dict = Depends(admin)):
    o = db.order_detail(order_id)
    if o is None:
        raise HTTPException(404, "Order not found")
    return o


@app.post("/api/admin/orders/start-all", tags=["admin"])
def admin_start_all(_: dict = Depends(admin)):
    ids = [r["order_id"] for r in db.query(
        "SELECT order_id FROM dbo.orders WHERE status = 'pending' AND order_date <= SYSDATETIME()"
        " AND NOT EXISTS (SELECT 1 FROM dbo.payments p WHERE p.order_id = dbo.orders.order_id AND p.payment_status = 'pending' AND p.payment_method <> 'cash_on_delivery')")]
    started = 0
    for oid in ids:
        try:
            with db.tx() as cur:
                mp.start_order(cur, oid)
            started += 1
        except mp.MarketplaceError:
            pass
    return {"started": started}


@app.post("/api/admin/orders/{order_id}/{action}", tags=["admin"])
def admin_order_action(order_id: int, action: str, _: dict = Depends(admin)):
    fn = {"start": mp.start_order, "cancel": mp.cancel_order, "return": mp.return_order}.get(action)
    if fn is None:
        raise HTTPException(404, "Unknown action")
    with db.tx() as cur:
        fn(cur, order_id)
    return {"ok": True}


@app.post("/api/admin/shipments/{shipment_id}/assign", tags=["admin"])
def admin_assign(shipment_id: int, body: Assign, _: dict = Depends(admin)):
    with db.tx() as cur:
        mp.assign_rider(cur, shipment_id, body.rider_id)
    return {"ok": True}


@app.post("/api/admin/shipments/{shipment_id}/{action}", tags=["admin"])
def admin_shipment_action(shipment_id: int, action: str, _: dict = Depends(admin)):
    """Admin can step in for any vendor or rider."""
    fn = {"accept": mp.vendor_accept, "ready": mp.vendor_ready,
          "pickup": mp.rider_pickup, "deliver": mp.rider_deliver}.get(action)
    if fn is None:
        raise HTTPException(404, "Unknown action")
    with db.tx() as cur:
        fn(cur, shipment_id)
    return {"ok": True}


@app.get("/api/admin/shipments", tags=["admin"])
def admin_shipments(_: dict = Depends(admin)):
    return db.shipments_board()


class CommissionRule(BaseModel):
    scope: str                          # default | category | vendor
    target_id: int | None = None        # category_id or vendor_id
    rate_percent: float | None = Field(default=None, ge=0, le=50)  # null removes a category/vendor override


class PayoutPaid(BaseModel):
    reference: str = Field(min_length=3, max_length=100)


@app.get("/api/admin/finance", tags=["admin"])
def admin_finance(_: dict = Depends(admin)):
    return db.finance_overview()


@app.put("/api/admin/commission", tags=["admin"])
def admin_commission(body: CommissionRule, _: dict = Depends(admin)):
    from decimal import Decimal  # noqa: PLC0415
    rate = None if body.rate_percent is None else (Decimal(str(body.rate_percent)) / 100).quantize(Decimal("0.0001"))
    with db.tx() as cur:
        mp.pay.set_rate(cur, body.scope, body.target_id, rate)
    return db.finance_overview()["rules"]


@app.post("/api/admin/payouts/run", tags=["admin"])
def admin_run_payouts(_: dict = Depends(admin)):
    with db.tx() as cur:
        mp.pay.release_available(cur, mp.now())
        return {"created": mp.pay.run_payouts(cur, mp.now())}


@app.post("/api/admin/payouts/{payout_id}/paid", tags=["admin"])
def admin_payout_paid(payout_id: int, body: PayoutPaid, _: dict = Depends(admin)):
    with db.tx() as cur:
        mp.pay.mark_payout_paid(cur, payout_id, body.reference, mp.now())
    return {"ok": True}


@app.get("/api/admin/vendors", tags=["admin"])
def admin_vendors(_: dict = Depends(admin)):
    return db.vendors_admin()


@app.patch("/api/admin/vendors/{vendor_id}", tags=["admin"])
def admin_vendor_flags(vendor_id: int, body: Flags, _: dict = Depends(admin)):
    if body.is_bot is not None:
        db.set_flag("vendors", "vendor_id", vendor_id, "is_bot", int(body.is_bot))
    if body.is_active is not None:
        db.set_flag("vendors", "vendor_id", vendor_id, "is_active", int(body.is_active))
    return {"ok": True}


@app.get("/api/admin/riders", tags=["admin"])
def admin_riders(_: dict = Depends(admin)):
    return db.riders_admin()


@app.patch("/api/admin/riders/{rider_id}", tags=["admin"])
def admin_rider_flags(rider_id: int, body: Flags, _: dict = Depends(admin)):
    if body.is_bot is not None:
        db.set_flag("riders", "rider_id", rider_id, "is_bot", int(body.is_bot))
    if body.status is not None:
        db.set_rider_status(rider_id, body.status)
    return {"ok": True}


@app.get("/api/admin/users", tags=["admin"])
def admin_users(_: dict = Depends(admin)):
    return db.users_admin()


@app.patch("/api/admin/users/{user_id}", tags=["admin"])
def admin_user_flags(user_id: int, body: Flags, me: dict = Depends(admin)):
    if body.is_active is not None:
        if user_id == me["user_id"] and not body.is_active:
            raise HTTPException(400, "You can't disable your own account")
        db.set_flag("users", "user_id", user_id, "is_active", int(body.is_active))
    return {"ok": True}


@app.post("/api/admin/products/{product_id}/image", tags=["admin"])
async def admin_product_image(product_id: int, file: UploadFile = File(...), _: dict = Depends(admin)):
    return {"image_url": await save_product_image(product_id, file)}


@app.patch("/api/admin/products/{product_id}", tags=["admin"])
def admin_update_product(product_id: int, body: ProductPatch, _: dict = Depends(admin)):
    db.update_product(product_id, body.unit_price, body.list_price, body.add_stock, body.is_active)
    return {"ok": True}


# ================================================================== analytics + live
@app.get("/api/stats", tags=["analytics"])
def stats(_: dict = Depends(admin)):
    return db.dashboard_stats()


@app.get("/api/events", tags=["analytics"])
async def events(user: dict | None = Depends(auth.current_user)):
    """Server-Sent Events: live changes, filtered to what the signed-in user may see."""
    return StreamingResponse(feed.stream(feed.subscribe(user)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/analytics", tags=["analytics"])
def gold_analytics(_: dict = Depends(admin)):
    """Business analytics from the GOLD layer (data_lake/gold/*.parquet, built by dbt + export_gold)."""
    if not (GOLD / "mart_daily_sales.parquet").exists():
        raise HTTPException(404, "No gold data yet - run the pipeline: extract, silver, dbt build, export gold.")
    con = duckdb.connect()

    def g(table: str) -> str:
        return f"read_parquet('{(GOLD / f'{table}.parquet').as_posix()}')"

    def q(sql: str) -> list[dict]:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, map(db._jsonable, row))) for row in cur.fetchall()]

    def has(table: str) -> bool:
        return (GOLD / f"{table}.parquet").exists()

    kpis = q(f"""
        WITH d AS (SELECT * FROM {g('mart_daily_sales')} WHERE date_day < current_date)
        SELECT
          sum(revenue)        FILTER (WHERE date_day >= current_date - 30) AS revenue_30d,
          sum(revenue)        FILTER (WHERE date_day <  current_date - 30 AND date_day >= current_date - 60) AS revenue_prev_30d,
          sum(revenue_orders) FILTER (WHERE date_day >= current_date - 30) AS orders_30d,
          sum(revenue_orders) FILTER (WHERE date_day <  current_date - 30 AND date_day >= current_date - 60) AS orders_prev_30d,
          sum(gross_margin)   FILTER (WHERE date_day >= current_date - 30) AS margin_30d,
          sum(cancelled_orders + returned_orders) FILTER (WHERE date_day >= current_date - 30) AS lost_orders_30d,
          sum(orders)         FILTER (WHERE date_day >= current_date - 30) AS all_orders_30d
        FROM d
    """)[0]
    return {
        "built_at": db._jsonable(datetime.fromtimestamp((GOLD / "mart_daily_sales.parquet").stat().st_mtime)),
        "kpis": kpis,
        "forecast": q(f"SELECT date_day, kind, revenue, forecast FROM {g('mart_revenue_forecast')} ORDER BY date_day"),
        "category_monthly": q(f"""
            SELECT category_name, year_month, revenue FROM {g('mart_category_monthly')}
            WHERE year_month >= strftime(current_date - INTERVAL 6 MONTH, '%Y-%m') ORDER BY year_month, category_name"""),
        "rfm": q(f"""SELECT segment, count(*) AS customers, round(avg(monetary), 2) AS avg_spend,
                     round(avg(recency_days)) AS avg_recency FROM {g('mart_customer_rfm')} GROUP BY segment ORDER BY customers DESC"""),
        "cohorts": q(f"""
            SELECT cohort, months_since_first, cohort_size, retention_pct FROM {g('mart_cohort_retention')}
            WHERE cohort >= strftime(current_date - INTERVAL 9 MONTH, '%Y-%m') AND months_since_first <= 6
            ORDER BY cohort, months_since_first"""),
        "top_products": q(f"""SELECT revenue_rank, product_name, category_name, current_price, units_sold, revenue,
                              margin_pct, return_rate, stock FROM {g('mart_product_performance')} ORDER BY revenue_rank LIMIT 10"""),
        "top_customers": q(f"""
            SELECT l.customer_id, l.full_name, l.country, l.orders, l.historical_ltv, l.predicted_12m_revenue, r.segment
            FROM {g('mart_customer_ltv')} l LEFT JOIN {g('mart_customer_rfm')} r USING (customer_id)
            ORDER BY l.historical_ltv DESC LIMIT 10"""),
        "vendors": q(f"SELECT * FROM {g('mart_vendor_performance')} ORDER BY revenue_30d DESC") if has("mart_vendor_performance") else [],
        "delivery": q(f"SELECT * FROM {g('mart_delivery_daily')} ORDER BY date_day") if has("mart_delivery_daily") else [],
        "platform": q(f"""SELECT date_day, delivered_sales, commission, take_rate, refunds FROM {g('mart_platform_revenue_daily')}
                          WHERE date_day >= current_date - 90 ORDER BY date_day""") if has("mart_platform_revenue_daily") else [],
        "seller_balances": q(f"SELECT * FROM {g('mart_seller_balances')} ORDER BY commission_paid DESC")
                           if has("mart_seller_balances") else [],
        "daily": q(f"""SELECT date_day, orders, revenue_orders, revenue, avg_order_value, margin_pct, revenue_7d_avg,
                              cancelled_orders + returned_orders AS lost_orders
                       FROM {g('mart_daily_sales')} WHERE date_day >= current_date - 90 AND date_day < current_date ORDER BY date_day"""),
        "category_30d": q(f"""
            SELECT p.category_name, sum(f.net_amount) AS revenue, sum(f.gross_margin) AS margin, sum(f.quantity) AS units
            FROM {g('fct_order_items')} f JOIN {g('dim_product')} p USING (product_sk)
            WHERE f.is_revenue AND f.order_ts >= current_date - 30 GROUP BY p.category_name ORDER BY revenue DESC"""),
        "rfm_value": q(f"""SELECT segment, count(*) AS customers, sum(monetary) AS revenue, round(avg(frequency), 1) AS avg_orders
                           FROM {g('mart_customer_rfm')} GROUP BY segment ORDER BY revenue DESC"""),
        "stage_median": q(f"""
            SELECT 'Waiting for approval' AS stage, 1 AS step, quantile_cont(minutes_to_confirm, 0.5) AS minutes FROM {g('fct_shipments')} WHERE created_at >= current_date - 30
            UNION ALL SELECT 'Store accepts', 2, quantile_cont(minutes_to_accept, 0.5) FROM {g('fct_shipments')} WHERE created_at >= current_date - 30
            UNION ALL SELECT 'Packing', 3, quantile_cont(minutes_to_pack, 0.5) FROM {g('fct_shipments')} WHERE created_at >= current_date - 30
            UNION ALL SELECT 'Waiting for rider', 4, quantile_cont(minutes_waiting_rider, 0.5) FROM {g('fct_shipments')} WHERE created_at >= current_date - 30
            UNION ALL SELECT 'Rider to store', 5, quantile_cont(minutes_to_pickup, 0.5) FROM {g('fct_shipments')} WHERE created_at >= current_date - 30
            UNION ALL SELECT 'In transit to customer', 6, quantile_cont(minutes_ride, 0.5) FROM {g('fct_shipments')} WHERE created_at >= current_date - 30
            ORDER BY step""") if has("fct_shipments") else [],
    }


# ================================================================== simulator + pipeline (admin)
def _sim_running() -> bool:
    return simulator["proc"] is not None and simulator["proc"].poll() is None


@app.get("/api/admin/simulator", tags=["admin"])
def simulator_status(_: dict = Depends(admin)):
    tail = SIM_LOG.read_text(errors="replace").splitlines()[-10:] if SIM_LOG.exists() else []
    return {"running": _sim_running(), "interval": simulator["interval"], "log": tail}


@app.post("/api/admin/simulator/start", tags=["admin"])
def simulator_start(interval: float = Query(5, ge=1, le=60), user: dict = Depends(admin)):
    if not _sim_running():
        STATE.mkdir(parents=True, exist_ok=True)
        simulator["proc"] = subprocess.Popen(
            [sys.executable, "-u", "source_app/generator.py", "stream", "--interval", str(interval)],
            cwd=ROOT, stdout=SIM_LOG.open("w"), stderr=subprocess.STDOUT,
        )
        simulator["interval"] = interval
    return simulator_status(user)


@app.post("/api/admin/simulator/stop", tags=["admin"])
def simulator_stop(user: dict = Depends(admin)):
    if _sim_running():
        simulator["proc"].terminate()
        simulator["proc"].wait(timeout=10)
    simulator["proc"] = None
    return simulator_status(user)


@app.post("/api/admin/pipeline/{step}", tags=["admin"])
def run_pipeline_step(step: str, _: dict = Depends(admin)):
    if step not in PIPELINE_STEPS:
        raise HTTPException(404, f"Unknown step. Choose from: {', '.join(PIPELINE_STEPS)}")
    shown, argv = PIPELINE_STEPS[step]
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=600,
                         env={**os.environ, "SHOP_ROOT": str(ROOT)})
    return {"command": shown, "exit_code": out.returncode, "output": (out.stdout + out.stderr).strip()}


@app.get("/api/admin/pipeline-status", tags=["admin"])
def pipeline_status(_: dict = Depends(admin)):
    """The automatic pipeline (Dagster sensor): recent runs, what's waiting, and how fresh gold is."""
    from ingestion.extract import pending_changes  # noqa: PLC0415 - keeps app startup light
    runs_file = STATE / "pipeline_runs.jsonl"
    runs = [json.loads(x) for x in runs_file.read_text().splitlines()[-15:] if x.strip()] if runs_file.exists() else []
    conn = db.get_connection()
    try:
        pending = pending_changes(conn)
    finally:
        conn.close()
    gold = GOLD / "mart_daily_sales.parquet"
    return {
        "runs": runs[::-1],
        "pending_tables": sorted(pending),
        "oldest_pending_change": db._jsonable(min(pending.values())) if pending else None,
        "gold_built_at": db._jsonable(datetime.fromtimestamp(gold.stat().st_mtime)) if gold.exists() else None,
        "dagster_up": _port_open(int(os.getenv("DAGSTER_PORT", "3000"))),
    }


def _port_open(port: int) -> bool:
    import socket  # noqa: PLC0415
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@app.get("/api/analytics/freshness", tags=["analytics"])
def analytics_freshness(_: dict = Depends(admin)):
    gold = GOLD / "mart_daily_sales.parquet"
    return {"gold_built_at": db._jsonable(datetime.fromtimestamp(gold.stat().st_mtime)) if gold.exists() else None}


@app.get("/api/admin/lake", tags=["admin"])
def lake_status(_: dict = Depends(admin)):
    con = duckdb.connect()

    def count(path) -> int:
        return con.execute(f"SELECT count(*) FROM read_parquet('{Path(path).as_posix()}')").fetchone()[0]

    layers = []
    for t in TABLES:
        bronze = LAKE / "bronze" / t
        files = list(bronze.rglob("*.parquet")) if bronze.exists() else []
        silver, quarantine = LAKE / "silver" / f"{t}.parquet", LAKE / "quarantine" / f"{t}.parquet"
        layers.append({
            "table": t, "bronze_files": len(files),
            "bronze_rows": count(bronze / "**" / "*.parquet") if files else 0,
            "silver_rows": count(silver) if silver.exists() else 0,
            "quarantined": count(quarantine) if quarantine.exists() else 0,
        })
    wm, dq_file, log_file = STATE / "watermarks.json", STATE / "dq_log.jsonl", STATE / "ingestion_log.jsonl"
    dq = [json.loads(x) for x in dq_file.read_text().splitlines() if x.strip()] if dq_file.exists() else []
    runs = [json.loads(x) for x in log_file.read_text().splitlines()[-14:]] if log_file.exists() else []
    return {
        "layers": layers,
        "watermarks": json.loads(wm.read_text()) if wm.exists() else {},
        "dq_latest": [r for r in dq if dq and r["run_at"] == dq[-1]["run_at"]],
        "recent_extracts": runs[::-1],
    }
