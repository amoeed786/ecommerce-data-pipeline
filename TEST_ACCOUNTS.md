# Kartora - test accounts

Demo logins for local testing. These are **not real credentials** - they exist only in your local ShopDB
(created by `source_app/migrate_marketplace.py`). Accounts you create yourself on the sign-in page are not listed here.

**Shoppers, sellers, riders:** sign in at **http://localhost:8000/login** → choose the account type → enter the
email and password, or click **⚡ Use the demo account** for that type.

**Admin:** has its own sign-in page, **http://localhost:8000/admin/login**. It isn't linked anywhere on the public
site, and admin accounts are refused on the public `/login`. If you set `ADMIN_PATH` in `.env`
(e.g. `/kartora-control-7f3a91`), the sign-in page is `<ADMIN_PATH>/login` instead, and `/admin` returns 404.

| Account type | Email | Password | Opens |
|---|---|---|---|
| 🛠️ Admin | `admin@kartora.test` | `Admin@123` | sign in at `/admin/login` → `/admin` (control room), `/admin/dashboard`, `/admin/analytics` |
| 🛍️ Shopper | `customer@kartora.test` | `Demo@123` | `/` storefront - cart, checkout, live order tracking |
| 🛵 Rider (you deliver) | `rider@kartora.test` | `Demo@123` | `/rider` - go online, pick up, deliver |
| 🏪 Seller (you pack) - Kartora Demo Store | `vendor@kartora.test` | `Demo@123` | `/vendor` - bulk accept + pack, products, photos |

## Seller accounts with autopilot (they pack orders by themselves)

All use the password **`Demo@123`**. Turn a store's autopilot off in its seller center (or in Admin → Vendors)
to handle its orders yourself.

| Store | Email |
|---|---|
| TechHub Electronics | `techhub@kartora.test` |
| Gadget Galaxy | `gadget@kartora.test` |
| StyleStreet | `stylestreet@kartora.test` |
| UrbanThreads | `urbanthreads@kartora.test` |
| HomeNest | `homenest@kartora.test` |
| PageTurner Books | `pageturner@kartora.test` |
| FitLife Sports | `fitlife@kartora.test` |
| GlowUp Beauty | `glowup@kartora.test` |
| ToyBox | `toybox@kartora.test` |
| FreshMart | `freshmart@kartora.test` |

## Coupons (checkout)

| Code | Discount |
|---|---|
| `SAVE5` | 5% |
| `SAVE10` | 10% |
| `SAVE20` | 20% |

## Notes

- Sign-in is per account type: using a seller email under "Shopper" tells you to pick "Seller" instead.
- 5 wrong passwords lock an email for 5 minutes.
- To reset these accounts on a fresh database: `python source_app/setup_db.py`, `python source_app/generator.py seed`,
  `python source_app/migrate_marketplace.py`.
