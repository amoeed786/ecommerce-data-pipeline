"""Import your own brand photos in bulk.

1. Put product photos in the `brand_photos/` folder (project root). Name each file by the product's
   SKU (e.g. ELE-00012.jpg - shown in the seller center and on product pages) or its product id (e.g. 12.png).
2. Run:  python tools/import_brand_photos.py

Each photo is checked to really be a JPG/PNG/WebP (by its bytes) and under 5 MB, copied into
shop_app/static/uploads/products/ under a random name, and set as that product's photo. Your photos
replace stock photos and generated art, and later stock-photo runs never overwrite them.
"""
import re
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from source_app.db import get_connection  # noqa: E402

SRC = ROOT / "brand_photos"
DEST = ROOT / "shop_app" / "static" / "uploads" / "products"
MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"RIFF": ".webp"}
MAX_BYTES = 5 * 1024 * 1024


def main() -> None:
    SRC.mkdir(exist_ok=True)
    files = [p for p in SRC.iterdir() if p.is_file() and not p.name.startswith(".")]
    if not files:
        print(f"No photos found. Put files named by SKU (e.g. ELE-00012.jpg) or product id (e.g. 12.jpg) in {SRC}")
        return
    DEST.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    cur = conn.cursor()
    by_sku = {sku.upper(): pid for pid, sku in cur.execute("SELECT product_id, sku FROM dbo.products").fetchall()}
    ids = set(by_sku.values())
    done, skipped = 0, []
    for f in sorted(files):
        key = f.stem.strip().upper()
        pid = by_sku.get(key) or (int(key) if re.fullmatch(r"\d+", key) and int(key) in ids else None)
        if pid is None:
            skipped.append(f"{f.name}: no product with that SKU or id")
            continue
        head = f.open("rb").read(12)
        ext = next((e for m, e in MAGIC.items() if head.startswith(m)), None)
        if ext is None or (ext == ".webp" and head[8:12] != b"WEBP"):
            skipped.append(f"{f.name}: not a JPG/PNG/WebP image")
            continue
        if f.stat().st_size > MAX_BYTES:
            skipped.append(f"{f.name}: larger than 5 MB")
            continue
        name = f"{pid}-{secrets.token_hex(8)}{ext}"
        shutil.copyfile(f, DEST / name)
        cur.execute("UPDATE dbo.products SET image_url = ?, updated_at = SYSDATETIME() WHERE product_id = ?",
                    f"/static/uploads/products/{name}", pid)
        done += 1
    conn.commit()
    conn.close()
    print(f"Imported {done} photo(s).")
    for s in skipped:
        print("  skipped", s)


if __name__ == "__main__":
    main()
