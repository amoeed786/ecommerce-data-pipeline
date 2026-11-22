# Raunaqi - test accounts

Demo logins for local testing. These are **not real credentials** - they exist only in your local ShopDB
(created by `source_app/migrate_marketplace.py`). Accounts you create yourself on the sign-in page are not listed here.

**Shoppers, sellers, riders:** sign in at **http://localhost:8000/login** → choose the account type → enter the
email and password, or click **⚡ Use the demo account** for that type.

**Admin:** has its own sign-in page, **http://localhost:8000/admin/login**. It isn't linked anywhere on the public
site, and admin accounts are refused on the public `/login`. If you set `ADMIN_PATH` in `.env`
(e.g. `/raunaqi-control-7f3a91`), the sign-in page is `<ADMIN_PATH>/login` instead, and `/admin` returns 404.

| Account type | Email | Password | Opens |
|---|---|---|---|
| 🛠️ Admin | `admin@raunaqi.test` | `Admin@123` | sign in at `/admin/login` → `/admin` (control room), `/admin/dashboard`, `/admin/analytics` |
| 🛍️ Shopper | `customer@raunaqi.test` | `Demo@123` | `/` storefront - cart, checkout, live order tracking |
| 🛵 Rider (you deliver) | `rider@raunaqi.test` | `Demo@123` | `/rider` - go online, pick up, deliver |
| 🏪 Seller (you pack) - Raunaqi Demo Store | `vendor@raunaqi.test` | `Demo@123` | `/vendor` - bulk accept + pack, products, photos |

## Seller accounts with autopilot (they pack orders by themselves)

All use the password **`Demo@123`**. Turn a store's autopilot off in its seller center (or in Admin → Vendors)
to handle its orders yourself.

| Store | Email |
|---|---|
| TechHub Electronics | `techhub@raunaqi.test` |
| Gadget Galaxy | `gadget@raunaqi.test` |
| StyleStreet | `stylestreet@raunaqi.test` |
| UrbanThreads | `urbanthreads@raunaqi.test` |
| HomeNest | `homenest@raunaqi.test` |
| PageTurner Books | `pageturner@raunaqi.test` |
| FitLife Sports | `fitlife@raunaqi.test` |
| GlowUp Beauty | `glowup@raunaqi.test` |
| ToyBox | `toybox@raunaqi.test` |
| FreshMart | `freshmart@raunaqi.test` |

## Test cards (sandbox payment gateway)

Pay with a card / PayPal / bank transfer at checkout and you're sent to the payment page. Use any future
expiry (e.g. `12/30`) and any 3-digit CVC.

| Card number | Result |
|---|---|
| `4242 4242 4242 4242` | Payment succeeds |
| `4000 0000 0000 0002` | Declined by the bank |
| `4000 0000 0000 9995` | Insufficient funds |

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
