"""Turn the generated catalog into a realistic one (one-time, safe to re-run with the same --seed).

  - Product names: invented brand + specific product that matches its type and photo
    (e.g. "Vireo AirPulse 4.1L Digital Air Fryer"). Brands are fictional on purpose: using real brands
    on invented products and reviews would misrepresent those companies.
  - Prices by product type, ending in .99/.49; ~1/3 on sale; a few nearly sold out.
  - Ratings 3.6-4.9, review counts 12-4,800, plus sample written reviews (reviews table).
  - A description and a spec sheet per product, and a brand column (for filters).
  - One market: stores, customers, riders and shipping addresses all in one country (--country).

Usage:  python tools/realistic_catalog.py [--country "United States"] [--seed 11]
"""
import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from source_app.db import get_connection  # noqa: E402

CITIES = {
    "United States": ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "Philadelphia", "San Diego", "Dallas",
                      "Seattle", "Austin", "Boston", "Miami", "Atlanta", "Denver", "San Francisco"],
    "Pakistan": ["Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar", "Quetta", "Sialkot", "Hyderabad"],
    "United Kingdom": ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Bristol", "Liverpool", "Edinburgh"],
}
SCHEMA = [
    "IF COL_LENGTH('dbo.products','brand') IS NULL ALTER TABLE dbo.products ADD brand NVARCHAR(60) NULL",
    "IF COL_LENGTH('dbo.products','photo_type') IS NULL ALTER TABLE dbo.products ADD photo_type VARCHAR(30) NULL",
    "IF COL_LENGTH('dbo.products','gallery') IS NULL ALTER TABLE dbo.products ADD gallery NVARCHAR(1000) NULL",
    "IF COL_LENGTH('dbo.products','specs') IS NULL ALTER TABLE dbo.products ADD specs NVARCHAR(2000) NULL",
    """IF OBJECT_ID('dbo.reviews','U') IS NULL CREATE TABLE dbo.reviews (
         review_id INT IDENTITY(1,1) PRIMARY KEY, product_id INT NOT NULL REFERENCES dbo.products(product_id),
         customer_id INT NULL REFERENCES dbo.customers(customer_id), rating TINYINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
         title NVARCHAR(120) NOT NULL, body NVARCHAR(1000) NOT NULL, verified BIT NOT NULL DEFAULT 1,
         helpful INT NOT NULL DEFAULT 0, created_at DATETIME2(0) NOT NULL, updated_at DATETIME2(0) NOT NULL DEFAULT SYSDATETIME())""",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_reviews_product') CREATE INDEX IX_reviews_product ON dbo.reviews(product_id)",
]

BRANDS = {
    "Electronics": ["Vireo", "Kova", "Novix", "Auralis", "Zentro", "Lumio"],
    "Fashion": ["Marlowe & Co", "Urbane Thread", "Solene", "Ridgeway", "North & Pine", "Atelier Noa"],
    "Home & Kitchen": ["Hearthly", "Casa Nova", "Brava Home", "Lumen & Oak", "Kitchora"],
    "Books": ["Harbor Lane Press", "Northlight Books", "Quill & Ivy", "Meridian House"],
    "Sports": ["Strive", "Peakform", "Velo Sport", "Kinetic Labs", "Apex Trail"],
    "Beauty": ["Dewlune", "Aurae", "Petal & Pine", "Lumière Skin", "Nativa"],
    "Toys": ["Brickly", "Wonderpal", "Tumbletoy", "Little Lab", "Playforge"],
    "Grocery": ["Golden Field", "Highland Estate", "Wildflower Apiary", "Olea Grove", "Cacao Coast"],
}
# old noun in the generated names -> (photo type, category)
NOUN_TYPE = {
    "Headphones": "headphones", "Smartphone": "smartphone", "Laptop": "laptop", "Smartwatch": "smartwatch", "Speaker": "speaker",
    "Tablet": "tablet", "Camera": "camera", "Keyboard": "keyboard", "T-Shirt": "tshirt", "Jeans": "jeans", "Sneakers": "sneakers",
    "Jacket": "jacket", "Dress": "dress", "Handbag": "handbag", "Sunglasses": "sunglasses", "Blender": "blender",
    "Coffee Maker": "coffee-maker", "Cookware Set": "cookware", "Desk Lamp": "desk-lamp", "Pillow": "pillow", "Vacuum": "vacuum",
    "Novel": "book", "Cookbook": "book", "Biography": "book", "Textbook": "book", "Comic": "book", "Yoga Mat": "yoga-mat",
    "Dumbbells": "dumbbells", "Football": "football", "Tennis Racket": "tennis-racket", "Bike Helmet": "bike-helmet",
    "Running Shoes": "running-shoes", "Moisturizer": "moisturizer", "Perfume": "perfume", "Lipstick": "lipstick",
    "Shampoo": "shampoo", "Face Serum": "serum", "Puzzle": "puzzle", "Building Blocks": "building-blocks", "Doll": "plush-toy",
    "RC Car": "rc-car", "Board Game": "board-game", "Coffee Beans": "coffee-beans", "Green Tea": "tea", "Olive Oil": "olive-oil",
    "Honey": "honey", "Dark Chocolate": "chocolate",
}
SPLIT = {"headphones": ("earbuds", .45), "coffee-maker": ("air-fryer", .5)}  # some get a sibling type for variety

# type: (price range USD, name templates, spec builder)
M = lambda *xs: random.choice(xs)  # noqa: E731
TYPE = {
    "headphones": ((49, 349), ["{b} {m} Wireless Noise-Cancelling Headphones", "{b} {m} Over-Ear Bluetooth Headphones"],
                   lambda: {"Battery": f"{M(30, 40, 50, 60)} hours", "Connectivity": "Bluetooth 5.3", "Noise cancelling": M("Active (ANC)", "Hybrid ANC"), "Weight": f"{M(250, 265, 280, 310)} g"}),
    "earbuds": ((29, 199), ["{b} {m} True Wireless Earbuds", "{b} {m} Sport Wireless Earbuds"],
                lambda: {"Battery": f"{M(6, 7, 8)} h (+{M(24, 28, 32)} h case)", "Water resistance": M("IPX4", "IPX5", "IP55"), "Connectivity": "Bluetooth 5.3"}),
    "smartphone": ((199, 1099), ["{b} {m} {g}GB Smartphone, {c}"], lambda: {"Display": f"{M('6.1', '6.4', '6.7')}\" OLED, 120 Hz", "Camera": f"{M(48, 50, 108, 200)} MP main", "Battery": f"{M(4500, 5000, 5500)} mAh", "Storage": "see title"}),
    "laptop": ((549, 1899), ["{b} {m} 14\" Laptop, {r}GB RAM, {s} SSD", "{b} {m} 16\" Laptop, {r}GB RAM, {s} SSD"],
               lambda: {"Processor": M("8-core", "10-core", "12-core"), "Display": M("2.8K OLED", "FHD+ IPS", "QHD+ 120 Hz"), "Battery": f"up to {M(12, 15, 18, 20)} h", "Weight": f"{M('1.2', '1.4', '1.6', '1.9')} kg"}),
    "smartwatch": ((99, 449), ["{b} {m} Smartwatch, {mm}mm GPS"], lambda: {"Display": "AMOLED, always-on", "Battery": f"up to {M(5, 7, 10, 14)} days", "Water resistance": "5 ATM", "Sensors": "Heart rate, SpO2, GPS"}),
    "speaker": ((29, 249), ["{b} {m} Portable Bluetooth Speaker", "{b} {m} Waterproof Bluetooth Speaker"], lambda: {"Battery": f"{M(12, 16, 20, 24)} hours", "Water resistance": M("IP67", "IPX7"), "Output": f"{M(10, 20, 30, 40)} W"}),
    "tablet": ((179, 899), ["{b} {m} 11\" Tablet, {g}GB, Wi-Fi"], lambda: {"Display": f"{M('10.9', '11', '12.4')}\" {M('LCD', 'OLED')}", "Battery": f"{M(8000, 9000, 10000)} mAh", "Storage": "see title"}),
    "camera": ((399, 1499), ["{b} {m} Mirrorless Camera with 18-55mm Lens"], lambda: {"Sensor": f"{M(24, 26, 33)} MP APS-C", "Video": M("4K 30p", "4K 60p"), "Stabilisation": "5-axis in-body"}),
    "keyboard": ((29, 159), ["{b} {m} Wireless Mechanical Keyboard", "{b} {m} Low-Profile Keyboard"], lambda: {"Switches": M("Red linear", "Brown tactile", "Low-profile"), "Connectivity": "Bluetooth + USB-C", "Battery": f"{M(1, 2, 3)} months"}),
    "tshirt": ((12, 39), ["{b} Essential Cotton Crew T-Shirt", "{b} Relaxed-Fit Organic T-Shirt"], lambda: {"Material": M("100% organic cotton", "Cotton blend"), "Fit": M("Regular", "Relaxed", "Slim"), "Care": "Machine wash cold"}),
    "jeans": ((29, 89), ["{b} Slim Straight Stretch Jeans", "{b} Relaxed Tapered Jeans"], lambda: {"Material": "98% cotton, 2% elastane", "Fit": M("Slim", "Straight", "Tapered"), "Rise": M("Mid", "High")}),
    "sneakers": ((49, 159), ["{b} {m} Everyday Sneakers", "{b} {m} Low-Top Leather Sneakers"], lambda: {"Upper": M("Leather", "Knit mesh", "Canvas"), "Sole": "Rubber", "Fit": "True to size"}),
    "jacket": ((59, 249), ["{b} Lightweight Puffer Jacket", "{b} Water-Resistant Rain Jacket", "{b} Classic Denim Jacket"], lambda: {"Material": M("Recycled nylon", "Cotton denim", "Polyester"), "Fit": "Regular", "Care": "Machine wash"}),
    "dress": ((29, 129), ["{b} Linen Midi Dress", "{b} Wrap Maxi Dress"], lambda: {"Material": M("100% linen", "Viscose", "Cotton poplin"), "Length": M("Midi", "Maxi"), "Care": "Hand wash"}),
    "handbag": ((39, 299), ["{b} Leather Tote Bag", "{b} Crossbody Bag"], lambda: {"Material": M("Full-grain leather", "Vegan leather"), "Closure": M("Zip", "Magnetic snap"), "Size": M("Medium", "Large")}),
    "sunglasses": ((19, 149), ["{b} Polarized Aviator Sunglasses", "{b} Classic Square Sunglasses"], lambda: {"Lens": "Polarized, UV400", "Frame": M("Metal", "Acetate"), "Includes": "Case + cloth"}),
    "blender": ((39, 199), ["{b} {m} 1200W Countertop Blender", "{b} {m} Personal Smoothie Blender"], lambda: {"Power": f"{M(600, 900, 1200, 1500)} W", "Jar": f"{M('0.6', '1.5', '2.0')} L", "Speeds": f"{M(3, 5, 10)}"}),
    "coffee-maker": ((49, 299), ["{b} {m} Programmable Drip Coffee Maker", "{b} {m} Espresso Machine"], lambda: {"Capacity": f"{M(10, 12)} cups", "Pressure": M("15 bar", "20 bar", "-"), "Features": "Auto-off, keep warm"}),
    "air-fryer": ((59, 199), ["{b} {m} {l}L Digital Air Fryer"], lambda: {"Capacity": "see title", "Power": f"{M(1400, 1500, 1700)} W", "Presets": f"{M(6, 8, 10)}", "Dishwasher-safe basket": "Yes"}),
    "cookware": ((49, 249), ["{b} 10-Piece Non-Stick Cookware Set", "{b} Stainless Steel Pots & Pans Set"], lambda: {"Pieces": f"{M(8, 10, 12)}", "Material": M("Hard-anodised aluminium", "Tri-ply stainless steel"), "Induction": "Yes"}),
    "desk-lamp": ((19, 89), ["{b} LED Desk Lamp with Wireless Charger", "{b} Adjustable Architect Desk Lamp"], lambda: {"Brightness": f"{M(5, 7)} levels", "Colour temperature": "3000-6500 K", "Power": "USB-C"}),
    "pillow": ((15, 69), ["{b} Memory Foam Pillow, Queen", "{b} Down-Alternative Pillow, Set of 2"], lambda: {"Fill": M("Memory foam", "Down alternative"), "Cover": "Cotton, removable", "Size": M("Standard", "Queen")}),
    "vacuum": ((89, 499), ["{b} {m} Cordless Stick Vacuum", "{b} {m} Robot Vacuum with Mapping"], lambda: {"Runtime": f"up to {M(40, 60, 90)} min", "Suction": f"{M(150, 180, 220)} AW", "Dust bin": f"{M('0.5', '0.6')} L"}),
    "book": ((9, 34), ["{t}", "{t}"], lambda: {"Format": M("Paperback", "Hardcover"), "Pages": f"{M(224, 288, 336, 412)}", "Language": "English"}),
    "yoga-mat": ((19, 79), ["{b} 6mm Non-Slip Yoga Mat", "{b} Eco Cork Yoga Mat"], lambda: {"Thickness": M("4 mm", "6 mm"), "Material": M("TPE", "Natural rubber + cork"), "Size": "183 x 61 cm"}),
    "dumbbells": ((29, 149), ["{b} Adjustable Dumbbell, 5-25 lb", "{b} Neoprene Dumbbell Pair, {w} lb"], lambda: {"Weight": "see title", "Grip": "Knurled, non-slip", "Material": M("Cast iron", "Neoprene-coated")}),
    "football": ((15, 49), ["{b} Match Football, Size 5", "{b} Training Football, Size 5"], lambda: {"Size": "5", "Construction": "Thermo-bonded", "Use": M("Match", "Training")}),
    "tennis-racket": ((39, 229), ["{b} {m} 100 Tennis Racket", "{b} {m} Graphite Tennis Racket"], lambda: {"Head size": "100 sq in", "Weight": f"{M(285, 300, 305)} g", "Material": "Graphite"}),
    "bike-helmet": ((29, 129), ["{b} Lightweight Road Bike Helmet", "{b} Urban Commuter Helmet with Light"], lambda: {"Certification": "CPSC", "Vents": f"{M(14, 18, 22)}", "Weight": f"{M(250, 280, 320)} g"}),
    "running-shoes": ((59, 179), ["{b} {m} Road Running Shoes", "{b} {m} Trail Running Shoes"], lambda: {"Drop": f"{M(6, 8, 10)} mm", "Cushioning": M("Medium", "High"), "Weight": f"{M(240, 260, 280)} g"}),
    "moisturizer": ((9, 49), ["{b} Hydrating Gel Moisturizer, 50 ml", "{b} Daily Face Cream SPF 30, 50 ml"], lambda: {"Skin type": M("All", "Dry", "Combination"), "Key ingredient": M("Hyaluronic acid", "Ceramides", "Niacinamide"), "Size": "50 ml"}),
    "perfume": ((29, 149), ["{b} {n} Eau de Parfum, 50 ml"], lambda: {"Notes": M("Bergamot, jasmine, amber", "Fig, cedar, musk", "Rose, oud, vanilla"), "Concentration": "Eau de Parfum", "Size": "50 ml"}),
    "lipstick": ((7, 29), ["{b} Satin Lipstick, {sh}"], lambda: {"Finish": "Satin", "Shade": "see title", "Weight": "3.5 g"}),
    "shampoo": ((6, 24), ["{b} Sulfate-Free Shampoo, 400 ml", "{b} Repair Shampoo, 400 ml"], lambda: {"Hair type": M("All", "Dry / damaged", "Curly"), "Free from": "Sulfates, parabens", "Size": "400 ml"}),
    "serum": ((12, 59), ["{b} Vitamin C Brightening Serum, 30 ml", "{b} Niacinamide 10% Serum, 30 ml"], lambda: {"Key ingredient": "see title", "Skin type": "All", "Size": "30 ml"}),
    "puzzle": ((9, 29), ["{b} 1000-Piece Jigsaw Puzzle, {theme}"], lambda: {"Pieces": "1000", "Finished size": "70 x 50 cm", "Age": "12+"}),
    "building-blocks": ((19, 129), ["{b} Creative Building Bricks, {pc} Pieces"], lambda: {"Pieces": "see title", "Age": "6+", "Compatible": "Major brick brands"}),
    "plush-toy": ((9, 39), ["{b} Cuddly Teddy Bear, 30 cm", "{b} Soft Plush Bunny, 25 cm"], lambda: {"Size": "see title", "Material": "Recycled polyester", "Age": "0+"}),
    "rc-car": ((24, 149), ["{b} 1:16 Off-Road RC Truck", "{b} High-Speed Remote Control Car"], lambda: {"Scale": M("1:16", "1:18"), "Top speed": f"{M(20, 30, 40)} km/h", "Battery": "Rechargeable, 2 included"}),
    "board-game": ((14, 59), ["{b} {g2}: Strategy Board Game", "{b} {g2}: Family Board Game"], lambda: {"Players": f"{M(2, 2, 3)}-{M(4, 5, 6)}", "Play time": f"{M(30, 45, 60, 90)} min", "Age": f"{M(8, 10, 12)}+"}),
    "coffee-beans": ((9, 29), ["{b} {o} Whole Bean Coffee, 1 kg", "{b} Medium Roast Ground Coffee, 500 g"], lambda: {"Roast": M("Medium", "Medium-dark", "Light"), "Origin": "see title", "Weight": "see title"}),
    "tea": ((5, 19), ["{b} Organic Green Tea, 50 Bags", "{b} Loose-Leaf Jasmine Green Tea, 100 g"], lambda: {"Type": "Green tea", "Organic": "Yes", "Caffeine": "Low"}),
    "olive-oil": ((8, 29), ["{b} Extra Virgin Olive Oil, 1 L", "{b} Cold-Pressed Olive Oil, 500 ml"], lambda: {"Grade": "Extra virgin", "Origin": M("Greece", "Spain", "Italy"), "Size": "see title"}),
    "honey": ((7, 24), ["{b} Raw Wildflower Honey, 500 g", "{b} Manuka Blend Honey, 250 g"], lambda: {"Type": "Raw, unfiltered", "Origin": M("Local", "New Zealand blend"), "Weight": "see title"}),
    "chocolate": ((3, 9), ["{b} 70% Dark Chocolate Bar, 100 g", "{b} 85% Dark Chocolate with Sea Salt, 100 g"], lambda: {"Cocoa": "see title", "Weight": "100 g", "Vegan": "Yes"}),
}
MODELS = ["Air", "Pro", "Max", "One", "Flex", "Neo", "Edge", "Pulse", "Arc", "Nova", "Prime", "Lite", "Core", "Wave"]
BOOKS = ["The Salt Orchard", "A Map of Quiet Things", "The Last Lighthouse Keeper", "Sourdough & Sunday Mornings", "Forty Days of Rain",
         "The Glass Architect", "How to Build Anything", "Small Kitchens, Big Flavours", "The Long Way Home", "Atlas of Forgotten Islands",
         "The Mindful Founder", "Under the Copper Moon", "Data for Humans", "The Orchard Keeper's Daughter", "Everyday Physics"]
REVIEWS = {
    5: [("Exactly what I wanted", "Great quality for the price. Arrived quickly and well packed - would buy again."),
        ("Love it", "Looks even better than the photos. Using it every day and it's holding up perfectly."),
        ("Five stars", "Does everything it says. Setup took two minutes. Highly recommend this seller.")],
    4: [("Very good", "Really happy overall. Only small thing is the packaging could be better, but the product itself is great."),
        ("Solid buy", "Good value. Delivery was a day later than estimated but the rider kept me updated."),
        ("Nice quality", "Feels premium. Took off one star because the colour is slightly different from the photo.")],
    3: [("It's okay", "Does the job but nothing special. Fine for the price."),
        ("Average", "Works as described, though I expected a bit better build quality.")],
    2: [("Not great", "Stopped working properly after a few weeks. Seller replied quickly about a return though.")],
}


def price_end(x: float) -> Decimal:
    """Retail pricing: .99 (or .49 for some lower prices)."""
    whole = max(1, int(x))
    return Decimal(f"{whole}.{random.choice(['99', '99', '49']) if x < 60 else '99'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--country", default="United States", choices=sorted(CITIES))
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    random.seed(a.seed)
    conn = get_connection()
    cur = conn.cursor()
    for stmt in SCHEMA:
        cur.execute(stmt)
    conn.commit()
    now = datetime.now().replace(microsecond=0)

    # ---- products
    rows = cur.execute("SELECT p.product_id, p.product_name, c.category_name, COALESCE(s.sold, 0) FROM dbo.products p "
                       "JOIN dbo.categories c ON c.category_id = p.category_id "
                       "LEFT JOIN (SELECT product_id, SUM(quantity) sold FROM dbo.order_items GROUP BY product_id) s "
                       "ON s.product_id = p.product_id ORDER BY p.product_id").fetchall()
    used, renamed = set(), 0
    for pid, name, cat, sold in rows:
        noun = max((n for n in NOUN_TYPE if n in name), key=len, default=None)
        ptype = NOUN_TYPE.get(noun)
        if ptype is None:  # already converted on an earlier run: keep its type
            ptype = cur.execute("SELECT photo_type FROM dbo.products WHERE product_id = ?", pid).fetchval() or "book"
        if ptype in SPLIT and random.random() < SPLIT[ptype][1]:
            ptype = SPLIT[ptype][0]
        (lo, hi), templates, spec = TYPE[ptype]
        brand = random.choice(BRANDS[cat])
        for _ in range(20):
            nm = random.choice(templates).format(
                b=brand, m=random.choice(MODELS) + (f" {random.randint(2, 9)}" if random.random() < .5 else ""),
                g=random.choice([128, 256, 512]), c=random.choice(["Midnight", "Graphite", "Silver", "Ocean Blue"]),
                r=random.choice([8, 16, 32]), s=random.choice(["512GB", "1TB"]), mm=random.choice([41, 44, 46]),
                l=random.choice(["3.5", "4.1", "5.5", "6.2"]), t=random.choice(BOOKS), w=random.choice([5, 10, 15, 20]),
                n=random.choice(["Ember", "Solstice", "Velvet Noir", "Azure"]), sh=random.choice(["Rosewood", "Classic Red", "Nude Petal", "Berry"]),
                theme=random.choice(["Mountain Lake", "City at Night", "Wildflowers"]), pc=random.choice([300, 500, 1000]),
                g2=random.choice(["Harbour Lights", "Trade Winds", "Skyward", "Kingdomfall"]), o=random.choice(["Colombia", "Ethiopia", "Guatemala"]))
            if nm not in used:
                break
        used.add(nm)
        brand_out = "" if ptype == "book" else brand
        if ptype == "book":
            nm = f"{nm} ({random.choice(['Paperback', 'Hardcover'])})"
            brand_out = brand  # publisher
        u = random.random()
        price = price_end(math.exp(math.log(lo) + u * (math.log(hi) - math.log(lo))))
        cost = (price * Decimal(str(round(random.uniform(.45, .7), 2)))).quantize(Decimal("0.01"))
        on_sale = random.random() < .34
        list_price = price_end(float(price) / (1 - random.choice([.1, .15, .2, .25, .3, .4]))) if on_sale else None
        rating = round(min(4.9, max(3.6, random.triangular(3.6, 4.9, 4.45))), 1)
        reviews = int(min(4800, max(12, (sold or 1) * random.uniform(.4, 1.6) + random.uniform(0, 60))))
        specs = {"Brand": brand_out or brand, **spec()}
        desc = (f"{nm.split(',')[0]} from {brand_out or brand}. Built for everyday use, backed by a 1-year seller warranty "
                f"and Raunaqi's 7-day easy returns.")
        cur.execute("UPDATE dbo.products SET product_name = ?, brand = ?, photo_type = ?, unit_price = ?, unit_cost = ?, list_price = ?, "
                    "rating = ?, review_count = ?, specs = ?, description = ?, updated_at = ? WHERE product_id = ?",
                    nm, brand_out or brand, ptype, price, cost, list_price, Decimal(str(rating)), reviews, json.dumps(specs), desc, now, pid)
        renamed += 1
    # a few nearly sold out so "Only N left" is real
    cur.execute("UPDATE i SET quantity_on_hand = 1 + ABS(CHECKSUM(NEWID())) % 4, updated_at = ? FROM dbo.inventory i "
                "WHERE i.product_id % 13 = 0", now)
    conn.commit()

    # ---- sample written reviews (rating mix follows each product's average)
    if not cur.execute("SELECT COUNT(*) FROM dbo.reviews").fetchval():
        cust = [r[0] for r in cur.execute("SELECT TOP 1500 customer_id FROM dbo.customers ORDER BY NEWID()").fetchall()]
        batch = []
        for pid, rating in cur.execute("SELECT product_id, rating FROM dbo.products").fetchall():
            for _ in range(random.randint(4, 9)):
                stars = max(2, min(5, round(random.gauss(float(rating), .8))))
                title, body = random.choice(REVIEWS[stars])
                batch.append((pid, random.choice(cust), stars, title, body, 1 if random.random() < .9 else 0,
                              random.randint(0, 40), now - timedelta(days=random.randint(1, 300), minutes=random.randint(0, 1440)), now))
        cur.fast_executemany = True
        cur.executemany("INSERT INTO dbo.reviews (product_id, customer_id, rating, title, body, verified, helpful, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
        print(f"reviews: {len(batch):,} written reviews")

    # ---- one market
    cities = CITIES[a.country]
    for table, key, cols in [("customers", "customer_id", ("city", "country")), ("vendors", "vendor_id", ("city", "country")),
                             ("riders", "rider_id", ("city", None)), ("orders", "order_id", ("shipping_city", "shipping_country"))]:
        city_col, country_col = cols
        sets = f"{city_col} = CHOOSE(1 + ABS(CHECKSUM({key})) % {len(cities)}, {', '.join(repr(c) for c in cities)})"
        if country_col:
            sets += f", {country_col} = ?"
        where = f"{country_col} IS NULL OR {country_col} <> ?" if country_col else "1 = 1"
        params = [a.country, a.country] if country_col else []
        n = cur.execute(f"UPDATE dbo.{table} SET {sets}, updated_at = ? WHERE {where}", *params[:1], now, *params[1:]).rowcount
        print(f"{table}: {n:,} moved to {a.country}")
    conn.commit()
    conn.close()
    print(f"products: {renamed} renamed with brands, prices, ratings, specs")


if __name__ == "__main__":
    main()
