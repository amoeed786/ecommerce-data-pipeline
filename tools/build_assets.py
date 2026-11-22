"""Download the storefront's design assets once and keep them local (fast, works offline, no third-party
requests from shoppers' browsers):

  - Inter (variable font, latin)       -> shop_app/static/fonts/inter-var.woff2
  - Lucide icons, as one SVG sprite    -> shop_app/static/icons.svg   (use with <svg><use href="/static/icons.svg#i-NAME"/></svg>)

Icons are rebuilt from their shape elements only (path, circle, line, ...), so nothing but drawing data
from the downloaded files ends up in the sprite.

Usage:  python tools/build_assets.py
"""
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "shop_app" / "static"
LUCIDE = "https://cdn.jsdelivr.net/npm/lucide-static@0.460.0/icons/{}.svg"
INTER = "https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.1.0/files/inter-latin-wght-normal.woff2"
ICONS = [
    "search", "shopping-cart", "shopping-bag", "heart", "user", "star", "truck", "banknote", "rotate-ccw",
    "shield-check", "store", "map-pin", "chevron-right", "chevron-left", "chevron-down", "x", "plus", "minus",
    "check", "house", "layout-grid", "menu", "sliders-horizontal", "list", "package", "clock", "zap", "flame", "tag",
    "trash-2", "credit-card", "bike", "phone", "log-out", "sparkles", "badge-percent", "arrow-right", "info",
    "circle-alert", "package-check", "circle-check", "eye", "history", "gift", "headphones", "smartphone", "shirt",
    "book-open", "dumbbell", "toy-brick", "apple", "sofa",
    "thumbs-up", "badge-check", "lock", "share-2", "wallet", "map", "navigation", "chevron-up", "image", "timer",
    "circle-user", "receipt", "arrow-left", "copy",
    # admin console
    "layout-dashboard", "users", "settings", "bell", "command", "activity", "database", "chart-column", "chart-line",
    "file-code", "sun", "moon", "monitor", "panel-left", "external-link", "refresh-cw", "play", "square", "arrow-up-right",
    "arrow-down-right", "arrow-up", "arrow-down", "chevrons-up-down", "filter", "ellipsis", "route", "landmark", "percent",
    "git-branch", "bot", "list-checks", "triangle-alert", "inbox", "kanban", "user-check", "user-x", "rows-3", "rows-4",
    "pencil", "save", "send", "workflow", "warehouse", "boxes", "power",
]
SHAPES = {"path", "circle", "line", "polyline", "polygon", "rect", "ellipse"}
SAFE_ATTR = re.compile(r"^(d|cx|cy|r|rx|ry|x|y|x1|x2|y1|y2|width|height|points)$")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kartora-asset-builder"}), timeout=30) as r:
        return r.read()


def main() -> None:
    (STATIC / "fonts").mkdir(parents=True, exist_ok=True)
    font = fetch(INTER)
    if not font.startswith(b"wOF2"):
        sys.exit("Inter download is not a WOFF2 font - aborting")
    (STATIC / "fonts" / "inter-var.woff2").write_bytes(font)
    print(f"Inter: {len(font) // 1024} KB")

    symbols, missing = [], []
    for name in ICONS:
        try:
            svg = ET.fromstring(fetch(LUCIDE.format(name)))
        except Exception:  # noqa: BLE001 - not every icon exists in every version
            missing.append(name)
            continue
        parts = []
        for el in svg.iter():
            tag = el.tag.split("}")[-1]
            if tag in SHAPES:
                attrs = " ".join(f'{k}="{v}"' for k, v in el.attrib.items() if SAFE_ATTR.match(k) and re.fullmatch(r"[0-9A-Za-z .,\-]+", v))
                parts.append(f"<{tag} {attrs}/>")
        symbols.append(f'<symbol id="i-{name}" viewBox="0 0 24 24">{"".join(parts)}</symbol>')
    sprite = ('<svg xmlns="http://www.w3.org/2000/svg" style="display:none">'
              '<!-- Lucide icons (ISC licence, lucide.dev) -->' + "".join(symbols) + "</svg>")
    (STATIC / "icons.svg").write_text(sprite, encoding="utf-8")
    print(f"Icons: {len(symbols)} in sprite" + (f" (not found: {', '.join(missing)})" if missing else ""))


if __name__ == "__main__":
    main()
