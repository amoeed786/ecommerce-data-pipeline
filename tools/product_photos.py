"""Product photo pipeline: find openly licensed photos per product TYPE, review them, publish as WebP.

  fetch    download candidates (Openverse: Wikimedia + Flickr, commercial-use licences) into
           data_lake/_state/photo_candidates/<type>/  (untrusted downloads stay in their own folder)
  sheets   build labelled contact sheets of all candidates, for a human to review
  approve  publish the chosen candidates: square, padded on white, WebP at 240/480/960 px,
           into shop_app/static/img/products/ + credits.json (CC licences require attribution)
  assign   give every product a gallery of its type's approved photos (rotated so neighbours differ)

Usage:
  python tools/product_photos.py fetch --per-type 10
  python tools/product_photos.py sheets
  python tools/product_photos.py approve headphones:1,3,4 sneakers:2,5 ...
  python tools/product_photos.py assign
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
CAND = ROOT / "data_lake" / "_state" / "photo_candidates"
OUT = ROOT / "shop_app" / "static" / "img" / "products"
API = "https://api.openverse.org/v1/images/"
UA = {"User-Agent": "kartora-photo-pipeline/1.0 (local demo store; python-urllib)"}
SIZES = (240, 480, 960)

# product type -> search phrase (biased toward clean product shots)
TYPES = {
    "headphones": "headphones product white background", "earbuds": "wireless earbuds product", "smartphone": "smartphone product white",
    "laptop": "laptop computer isolated", "smartwatch": "smartwatch product", "speaker": "bluetooth speaker product",
    "tablet": "tablet computer product", "camera": "digital camera isolated", "keyboard": "computer keyboard isolated",
    "tshirt": "t-shirt product white background", "jeans": "jeans denim product", "sneakers": "sneakers product white background",
    "jacket": "jacket product white background", "dress": "dress product mannequin", "handbag": "handbag product white background",
    "sunglasses": "sunglasses isolated white", "blender": "blender kitchen appliance", "coffee-maker": "coffee maker machine",
    "air-fryer": "air fryer", "cookware": "cookware pot pan set", "desk-lamp": "desk lamp isolated", "pillow": "pillow white bedding",
    "vacuum": "vacuum cleaner product", "book": "hardcover book isolated", "yoga-mat": "yoga mat rolled", "dumbbells": "dumbbells weights isolated",
    "football": "soccer ball isolated", "tennis-racket": "tennis racket isolated", "bike-helmet": "bicycle helmet product",
    "running-shoes": "running shoes product", "moisturizer": "skincare cream jar product", "perfume": "perfume bottle product",
    "lipstick": "lipstick product", "shampoo": "shampoo bottle product", "serum": "serum dropper bottle product",
    "puzzle": "jigsaw puzzle pieces", "building-blocks": "toy building bricks", "plush-toy": "teddy bear plush toy",
    "rc-car": "remote control car toy", "board-game": "board game box", "coffee-beans": "coffee beans bag", "tea": "green tea leaves cup",
    "olive-oil": "olive oil bottle", "honey": "honey jar", "chocolate": "dark chocolate bar",
}
MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}


def get(url: str) -> bytes:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(20 * (attempt + 1))
                continue
            if e.code in (500, 502, 503, 504) and attempt < 4:   # Openverse is often briefly overloaded
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def get_original(url: str) -> bytes:
    """One attempt, size-capped (some originals are 20+ MB)."""
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        data = r.read(25_000_000)
    return data


def shrink(data: bytes) -> bytes:
    """Re-encode as a JPEG of at most 1200 px (also strips metadata)."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((1200, 1200))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return out.getvalue()


def fetch(per_type: int, only: list[str] | None = None) -> None:
    for t, q in TYPES.items():
        if only and t not in only:
            continue
        d = CAND / t
        d.mkdir(parents=True, exist_ok=True)
        have = len(list(d.glob("[0-9]*.*")))
        if have >= per_type:
            continue
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        seen = {m.get("source") for m in meta.values()}
        phrase = q.replace(" product", "").replace(" white background", "").replace(" isolated", "").replace(" white", "")
        params = urllib.parse.urlencode({"q": phrase, "license_type": "commercial", "mature": "false", "page_size": 20,   # anonymous max
                                         "source": "stocksnap,rawpixel,wikimedia,flickr"})
        try:
            results = json.loads(get(f"{API}?{params}"))["results"]
        except Exception as e:  # noqa: BLE001
            print(f"  {t}: search failed {e}")
            continue
        n = have
        for r in results:
            if n >= per_type:
                break
            if r.get("foreign_landing_url") in seen or "wikimedia.org" in (r.get("url") or ""):  # Commons is unreachable here
                continue
            try:
                data = get_original(r["url"])     # from the photo's own host: doesn't use the Openverse API quota
            except Exception:  # noqa: BLE001
                continue
            ext = next((e for m, e in MAGIC.items() if data.startswith(m)), None)
            if not ext or len(data) < 4000:
                continue
            data, ext = shrink(data), ".jpg"
            n += 1
            (d / f"{n:02d}{ext}").write_bytes(data)
            meta[f"{n:02d}"] = {"title": r.get("title"), "creator": r.get("creator"), "license": r.get("license"),
                                "license_version": r.get("license_version"), "license_url": r.get("license_url"),
                                "source": r.get("foreign_landing_url")}
            time.sleep(0.3)
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        print(f"  {t:<16} {n} candidates")
        time.sleep(1.5)


COMMONS = "https://commons.wikimedia.org/w/api.php"


def fetch_commons(per_type: int, only: list[str] | None = None) -> None:
    """Wikimedia Commons search (every file there is under a free licence; we record licence + author)."""
    import html as htmllib
    for t, q in TYPES.items():
        if only and t not in only:
            continue
        d = CAND / t
        d.mkdir(parents=True, exist_ok=True)
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        have = len([p for p in d.glob("[0-9]*.*")])
        if have >= per_type:
            continue
        phrase = q.replace(" product", "").replace("isolated", "white background")
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": per_type * 3,
            "gsrsearch": f"{phrase} filetype:bitmap", "prop": "imageinfo", "iiprop": "url|extmetadata|mime", "iiurlwidth": 640})
        try:
            pages = json.loads(get(f"{COMMONS}?{params}")).get("query", {}).get("pages", {})
        except Exception as e:  # noqa: BLE001
            print(f"  {t}: search failed {e}")
            continue
        seen = {m.get("source") for m in meta.values()}
        n = have
        for pg in sorted(pages.values(), key=lambda x: x.get("index", 0)):
            if n >= per_type:
                break
            ii = (pg.get("imageinfo") or [{}])[0]
            if ii.get("mime") not in ("image/jpeg", "image/png") or ii.get("descriptionurl") in seen:
                continue
            em = ii.get("extmetadata", {})
            lic = em.get("LicenseShortName", {}).get("value", "")
            if not lic or "fair use" in lic.lower() or "non-free" in lic.lower():
                continue
            try:
                data = get(ii["thumburl"])
            except Exception:  # noqa: BLE001
                continue
            ext = next((e for m, e in MAGIC.items() if data.startswith(m)), None)
            if not ext or len(data) < 4000:
                continue
            n += 1
            (d / f"{n:02d}{ext}").write_bytes(data)
            artist = re.sub(r"<[^>]+>", "", htmllib.unescape(em.get("Artist", {}).get("value", ""))).strip()
            meta[f"{n:02d}"] = {"title": pg.get("title"), "creator": artist[:120], "license": lic,
                                "license_url": em.get("LicenseUrl", {}).get("value"), "source": ii.get("descriptionurl")}
            time.sleep(0.3)
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        print(f"  {t:<16} {n} candidates", flush=True)


def sheets(out_dir: Path) -> None:
    from PIL import Image, ImageDraw
    cells = [(t, p) for t in TYPES for p in sorted((CAND / t).glob("[0-9]*.*"))]
    T, COLS, PER = 140, 10, 60
    for s in range(0, len(cells), PER):
        chunk = cells[s:s + PER]
        rows = (len(chunk) + COLS - 1) // COLS
        sheet = Image.new("RGB", (COLS * T, rows * (T + 16)), "white")
        d = ImageDraw.Draw(sheet)
        for i, (t, p) in enumerate(chunk):
            try:
                im = Image.open(p).convert("RGB"); im.thumbnail((T, T))
            except Exception:  # noqa: BLE001
                continue
            x, y = (i % COLS) * T, (i // COLS) * (T + 16)
            sheet.paste(im, (x + (T - im.width) // 2, y + (T - im.height) // 2))
            d.text((x + 2, y + T + 2), f"{t}:{p.stem}"[:22], fill="black")
        f = out_dir / f"candidates_{s // PER + 1}.png"
        sheet.save(f)
        print(f)


def approve(picks: list[str]) -> None:
    from PIL import Image
    OUT.mkdir(parents=True, exist_ok=True)
    credits_f = OUT / "credits.json"
    credits = json.loads(credits_f.read_text()) if credits_f.exists() else {}
    for pick in picks:
        t, nums = pick.split(":")
        meta = json.loads((CAND / t / "meta.json").read_text())
        for k, num in enumerate(nums.split(","), start=1):
            num = f"{int(num):02d}"
            src = next((CAND / t).glob(f"{num}.*"))
            im = Image.open(src).convert("RGB")
            side = max(im.size)
            canvas = Image.new("RGB", (side, side), "white")       # square, padded on white
            canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
            base = f"{t}-{k}"
            for size in SIZES:
                canvas.resize((size, size), Image.LANCZOS).save(OUT / f"{base}-{size}.webp", "WEBP", quality=80, method=6)
            credits[base] = meta.get(num, {})
        print(f"  {t}: {len(nums.split(','))} published")
    credits_f.write_text(json.dumps(credits, indent=1))


def assign() -> None:
    from source_app.db import get_connection
    bases: dict[str, list[str]] = {}
    for f in OUT.glob("*-480.webp"):
        t, k = re.match(r"(.+)-(\d+)-480\.webp", f.name).groups()
        bases.setdefault(t, []).append(f"{t}-{k}")
    conn = get_connection()
    cur = conn.cursor()
    rows = cur.execute("SELECT product_id, photo_type FROM dbo.products").fetchall()
    n = 0
    for pid, ptype in rows:
        imgs = sorted(bases.get(ptype or "", []))
        if imgs:
            r = pid % len(imgs)
            gallery = imgs[r:] + imgs[:r]          # rotate so neighbouring products lead with different photos
            cur.execute("UPDATE dbo.products SET gallery = ?, image_url = ?, updated_at = SYSDATETIME() WHERE product_id = ? "
                        "AND (image_url IS NULL OR image_url NOT LIKE '/static/uploads/%')",
                        json.dumps(gallery), f"/static/img/products/{gallery[0]}-480.webp", pid)
            n += cur.rowcount
        else:  # no approved photo for this type: drop any old stock photo (it shows a different product); keep seller uploads
            cur.execute("UPDATE dbo.products SET gallery = NULL, image_url = NULL, updated_at = SYSDATETIME() WHERE product_id = ? "
                        "AND (gallery IS NOT NULL OR image_url LIKE '/static/img/products/%')", pid)
    conn.commit()
    print(f"assigned galleries to {n} products ({len(bases)} types have photos)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "sheets", "approve", "assign"])
    ap.add_argument("picks", nargs="*", help="approve: type:1,3 ...  fetch: optional list of types")
    ap.add_argument("--per-type", type=int, default=10)
    ap.add_argument("--source", choices=["commons", "openverse"], default="openverse")
    ap.add_argument("--out", default=str(ROOT / "data_lake" / "_state"))
    a = ap.parse_args()
    {"fetch": lambda: fetch_commons(a.per_type, a.picks) if a.source == "commons" else fetch(a.per_type, a.picks),
     "sheets": lambda: sheets(Path(a.out)),
     "approve": lambda: approve(a.picks), "assign": assign}[a.cmd]()


if __name__ == "__main__":
    main()
