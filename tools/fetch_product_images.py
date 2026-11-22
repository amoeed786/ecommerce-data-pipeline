"""Give every product a real photo, using openly licensed images from Openverse (api.openverse.org).

For each product type (Headphones, Sneakers, Honey, ...) it downloads a couple of photos into
shop_app/static/img/products/, records who took each photo and its license in credits.json (shown on
product pages, as CC licenses require), and sets products.image_url for products that don't have one.
Products that sellers gave their own photo (uploads) are left alone.

Usage:
    python tools/fetch_product_images.py              # download what's missing, assign photos
    python tools/fetch_product_images.py --per-type 3 # more variety
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from source_app.db import get_connection  # noqa: E402

IMG_DIR = ROOT / "shop_app" / "static" / "img" / "products"
CREDITS = IMG_DIR / "credits.json"
API = "https://api.openverse.org/v1/images/"
UA = {"User-Agent": "learning-project-image-fetcher/1.0"}
MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}

# Product noun (as in product names) -> search phrase that gets clean product photos
TYPES = {
    "Headphones": "headphones", "Smartphone": "smartphone", "Laptop": "laptop computer", "Smartwatch": "smartwatch",
    "Speaker": "bluetooth speaker", "Tablet": "tablet computer", "Camera": "digital camera", "Keyboard": "computer keyboard",
    "T-Shirt": "t-shirt", "Jeans": "jeans denim", "Sneakers": "sneakers", "Jacket": "jacket", "Dress": "dress fashion",
    "Handbag": "handbag", "Sunglasses": "sunglasses", "Blender": "blender kitchen", "Coffee Maker": "coffee maker",
    "Cookware Set": "cookware pots", "Desk Lamp": "desk lamp", "Pillow": "pillow", "Vacuum": "vacuum cleaner",
    "Novel": "novel book", "Cookbook": "cookbook", "Biography": "book cover", "Textbook": "textbook", "Comic": "comic book",
    "Yoga Mat": "yoga mat", "Dumbbells": "dumbbells", "Football": "soccer ball", "Tennis Racket": "tennis racket",
    "Bike Helmet": "bicycle helmet", "Running Shoes": "running shoes", "Moisturizer": "skincare cream",
    "Perfume": "perfume bottle", "Lipstick": "lipstick", "Shampoo": "shampoo bottle", "Face Serum": "serum bottle",
    "Puzzle": "jigsaw puzzle", "Building Blocks": "toy building blocks", "Doll": "doll toy", "RC Car": "rc car toy",
    "Board Game": "board game", "Coffee Beans": "coffee beans", "Green Tea": "green tea", "Olive Oil": "olive oil bottle",
    "Honey": "honey jar", "Dark Chocolate": "dark chocolate",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:  # rate limited: back off
                time.sleep(15 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def fetch_type(noun: str, query: str, n: int, credits: dict) -> list[str]:
    have = sorted(p.name for p in IMG_DIR.glob(f"{slug(noun)}-*.*") if p.suffix in (".jpg", ".png", ".webp"))
    if len(have) >= n:
        return have[:n]
    params = urllib.parse.urlencode({"q": query, "license_type": "commercial", "mature": "false",
                                     "page_size": 12, "aspect_ratio": "square"})
    results = json.loads(get(f"{API}?{params}"))["results"]
    if len(results) < n:  # not enough square photos - accept any shape
        params = params.replace("&aspect_ratio=square", "")
        results = json.loads(get(f"{API}?{params}"))["results"]
    for r in results:
        if len(have) >= n:
            break
        try:
            data = get(r["thumbnail"])
        except Exception as e:  # noqa: BLE001 - skip broken images, try the next result
            print(f"    skip {r['id']}: {e}")
            continue
        ext = next((e for m, e in MAGIC.items() if data.startswith(m)), None)
        if ext is None or len(data) < 3000:  # not an image, or a tiny placeholder
            continue
        name = f"{slug(noun)}-{len(have) + 1}{ext}"
        (IMG_DIR / name).write_bytes(data)
        credits[name] = {"title": r.get("title"), "creator": r.get("creator"), "license": r.get("license"),
                         "license_version": r.get("license_version"), "license_url": r.get("license_url"),
                         "source": r.get("foreign_landing_url")}
        have.append(name)
        time.sleep(1.5)  # be polite to the free API
    return have


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--per-type", type=int, default=2)
    parser.add_argument("--assign-only", action="store_true",
                        help="don't download - just (re)assign the photos already in the folder (use after reviewing them)")
    args = parser.parse_args()
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    credits = json.loads(CREDITS.read_text()) if CREDITS.exists() else {}

    files: dict[str, list[str]] = {}
    for noun, query in TYPES.items():
        if args.assign_only:
            files[noun] = sorted(p.name for p in IMG_DIR.glob(f"{slug(noun)}-*.*") if p.suffix in (".jpg", ".png", ".webp"))
            continue
        try:
            files[noun] = fetch_type(noun, query, args.per_type, credits)
            print(f"  {noun:<16} {len(files[noun])} photo(s)")
        except Exception as e:  # noqa: BLE001
            print(f"  {noun:<16} failed: {e}")
            files[noun] = []
        CREDITS.write_text(json.dumps(credits, indent=1))
        time.sleep(1.5)

    conn = get_connection()
    cur = conn.cursor()
    products = cur.execute("SELECT product_id, product_name FROM dbo.products "
                           "WHERE image_url IS NULL OR image_url LIKE '/static/img/products/%'").fetchall()
    assigned = 0
    for pid, name in products:
        noun = max((n for n in TYPES if n in name), key=len, default=None)
        options = files.get(noun) or []
        if options:
            cur.execute("UPDATE dbo.products SET image_url = ?, updated_at = SYSDATETIME() WHERE product_id = ? "
                        "AND (image_url IS NULL OR image_url <> ?)",
                        f"/static/img/products/{options[pid % len(options)]}", pid,
                        f"/static/img/products/{options[pid % len(options)]}")
            assigned += cur.rowcount
        else:  # no approved photo for this type: back to the generated art (seller uploads are never touched)
            cur.execute("UPDATE dbo.products SET image_url = NULL, updated_at = SYSDATETIME() WHERE product_id = ? "
                        "AND image_url LIKE '/static/img/products/%'", pid)
    conn.commit()
    conn.close()
    print(f"Photos: {sum(map(len, files.values()))} files · {assigned} products updated · credits in {CREDITS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
