"""Platform advisor — where to sell, how to ship, and tariff awareness.

For each product category, provides:
1. Best target platforms (eBay, Amazon, Etsy, Mercari US, Poshmark, etc.)
2. Platform-specific fee structures
3. Shipping cost estimates by carrier and destination
4. Tariff/duty estimates by country and product type
5. Recommended listing strategy per platform
"""

from __future__ import annotations


# ===== TARGET PLATFORMS =====

# Each platform: (name, fees, best_for, shipping_notes)
TARGET_PLATFORMS = {
    "ebay_us": {
        "name": "eBay US",
        "url": "https://www.ebay.com",
        "fee_pct": 0.13,  # Final value fee
        "payment_pct": 0.029,
        "payment_fixed": 0.30,
        "insertion_fee": 0.0,  # First 250 free/month
        "best_for": [
            "anime_figure", "trading_cards", "vintage_camera",
            "vintage_electronics", "vintage_toys", "japanese_ceramics",
        ],
        "audience": "US/international collectors, highest prices for rare items",
        "pros": [
            "Largest global marketplace for collectibles",
            "Auction format for rare items drives up prices",
            "Global Shipping Program handles international logistics",
            "Strong buyer protection = higher buyer trust",
        ],
        "cons": [
            "13%+ fees eat into margins",
            "eBayManaged Payments hold funds for new sellers",
            "Competition from established sellers",
        ],
        "shipping_notes": "Use eBay Global Shipping Program (GSP) — buyer pays international shipping + duties. You ship domestic to eBay's hub.",
        "tariff_handling": "eBay GSP collects import duties from buyer at checkout — you don't pay US tariffs.",
        "listing_tips": [
            "Use auction format for rare/unique items (7-day, start at $0.99)",
            "Buy It Now for commodity items with clear market price",
            "Include 'Japan Import' and 'Rare' in title",
            "Ship via GSP for international sales — zero tariff risk to you",
        ],
    },
    "amazon_us": {
        "name": "Amazon US",
        "url": "https://www.amazon.com",
        "fee_pct": 0.08,  # Referral fee (varies by category)
        "fulfillment_fee_per_lb": 0.50,  # FBA
        "monthly_fee": 39.99,  # Professional seller
        "storage_per_cuft_monthly": 0.87,  # FBA storage
        "best_for": [
            "trading_cards", "japanese_knife", "vintage_electronics",
        ],
        "audience": "US mainstream buyers, high volume",
        "pros": [
            "Highest traffic volume",
            "FBA handles storage, shipping, customer service",
        ],
        "cons": [
            "Stricter product listing requirements",
            "FBA fees eat margins on low-priced items",
            "Require UPC/EAN for most categories",
            "Longer payment cycles",
        ],
        "shipping_notes": "Use FBA — ship bulk to Amazon warehouse. Amazon handles fulfillment. Or FBM (merchant-fulfilled) for large/heavy items.",
        "tariff_handling": "You pay import tariffs when shipping to Amazon FBA warehouse. Factor ~3% for most goods under $800 de minimis.",
        "listing_tips": [
            "FBA only viable for items >$30 profit after all fees",
            "Need UPC/EAN barcodes (buy from GS1)",
            "Stiff competition — price competitively",
        ],
    },
    "etsy": {
        "name": "Etsy",
        "url": "https://www.etsy.com",
        "fee_pct": 0.065,  # Transaction fee
        "listing_fee": 0.20,  # Per listing
        "payment_pct": 0.03,
        "payment_fixed": 0.25,
        "best_for": [
            "japanese_ceramics", "vintage_toys", "vinyl_records",
        ],
        "audience": "US/UK buyers seeking vintage, handmade, unique items",
        "pros": [
            "Lower transaction fees than eBay",
            "Less competition for niche Japanese goods",
            "Buyers expect to pay premium for unique items",
        ],
        "cons": [
            "Only vintage (20+ years), handmade, or craft supplies",
            "Reselling mass-produced items = policy violation",
            "Smaller buyer pool",
            "Strict about 'handmade' claims",
        ],
        "shipping_notes": "Ship yourself. Offer free shipping (build into price) — Etsy algorithm favors free shipping.",
        "tariff_handling": "You're the seller of record for international sales. Ship via USPS First Class International or Priority Mail.",
        "listing_tips": [
            "Only list items 20+ years old as 'vintage'",
            "Tag with 'japanese', 'import', 'rare', 'vintage'",
            "Quality photos critical — Etsy is visual",
        ],
    },
    "mercari_us": {
        "name": "Mercari US",
        "url": "https://www.mercari.com/us",
        "fee_pct": 0.10,  # Selling fee
        "payment_pct": 0.0,
        "payment_fixed": 0.0,
        "best_for": [
            "anime_figure", "streetwear", "japanese_knife",
        ],
        "audience": "US buyers familiar with Mercari Japan",
        "pros": [
            "Simple interface, low fees",
            "Built-in shipping labels",
            "No listing fees",
        ],
        "cons": [
            "Smaller marketplace than eBay",
            "Lower price expectations",
            "Dropshipping from other platforms = prohibited",
        ],
        "shipping_notes": "Use Mercari's prepaid shipping labels (USPS). Flat-rate boxes available.",
        "tariff_handling": "Domestic US sales only — no tariffs. You handle shipping to buyer.",
        "listing_tips": [
            "Price 10-15% below eBay (buyers expect deals)",
            "Bundle items to increase average order value",
            "Quick shipping = better ratings",
        ],
    },
    "poshmark": {
        "name": "Poshmark",
        "url": "https://www.poshmark.com",
        "fee_pct": 0.20,  # Flat 20% fee on sales > $15
        "flat_fee_under_15": 2.95,  # For sales ≤ $15
        "best_for": [
            "streetwear", "japanese_knife",
        ],
        "audience": "US fashion-focused buyers",
        "pros": [
            "Massive US audience for fashion/streetwear",
            "Social selling model — sharing listings helps",
            "Prepaid shipping labels included",
        ],
        "cons": [
            "20% fee is high",
            "Fashion-only platform",
            "Slow payment (released after delivery)",
        ],
        "shipping_notes": "USPS Priority Mail label provided by Poshmark. Flat rate up to 5 lbs.",
        "tariff_handling": "Domestic US only — no tariffs.",
        "listing_tips": [
            "List designer streetwear at 30-50% below retail",
            "Share your listings daily for visibility",
            "Bundle items for discounts",
        ],
    },
    "grailed": {
        "name": "Grailed",
        "url": "https://www.grailed.com",
        "fee_pct": 0.06,  # Payment processing
        "listing_fee": 0.0,
        "best_for": [
            "streetwear",
        ],
        "audience": "US/UK streetwear enthusiasts, willing to pay premium",
        "pros": [
            "Lower fees than Poshmark",
            "Design-savvy buyers pay top dollar for rare pieces",
            "Good for Japanese streetwear (BAPE, Visvim, CDG)",
        ],
        "cons": [
            "Fashion-only, smaller audience",
            "Designed for individual sellers, not businesses",
        ],
        "shipping_notes": "You ship. Buyer pays shipping cost. Use tracked shipping.",
        "tariff_handling": "You're responsible for international shipping. Use USPS or DHL.",
        "listing_tips": [
            "Japanese streetwear commands 2-3x premiums vs domestic",
            "Provide detailed measurements and condition notes",
            "Keyword: 'Japan import', 'vintage', 'rare', 'deadstock'",
        ],
    },
    "yahoo_auctions_us": {
        "name": "Yahoo Auctions via Buyee",
        "url": "https://auctions.yahoo.co.jp",
        "fee_pct": 0.0,  # You're buying, not selling
        "best_for": [],
        "audience": "This is a SOURCE platform, not a target",
        "pros": [],
        "cons": [],
        "shipping_notes": "N/A — use Buyee as proxy to purchase from Yahoo Auctions JP",
        "tariff_handling": "N/A",
        "listing_tips": [],
    },
}

# ===== SHIPPING COSTS: Japan → US/Destinations =====

SHIPPING_ROUTES = {
    "jp_to_us": {
        "epacket": {
            "name": "ePacket (Japan Post)",
            "days": "6-15 business days",
            "tracking": "Yes (basic)",
            "insurance": "Up to ¥20,000 included",
            "cost_per_100g": 130,  # ¥ per 100g
            "min_cost": 900,  # ¥ minimum
            "max_weight_kg": 2,
            "best_for": "Light items under 2kg, low value",
        },
        "ems": {
            "name": "EMS (Japan Post Express)",
            "days": "3-7 business days",
            "tracking": "Yes (full)",
            "insurance": "Up to ¥2,000,000",
            "cost_per_100g": 220,  # ¥ per 100g for first 500g, then less
            "min_cost": 1500,  # ¥ minimum
            "max_weight_kg": 30,
            "best_for": "Medium items, faster than ePacket",
        },
        "dhl_express": {
            "name": "DHL Express",
            "days": "2-4 business days",
            "tracking": "Yes (full, real-time)",
            "insurance": "Full value, buy separately",
            "cost_per_kg": 3500,  # ¥ per kg (approximate)
            "min_cost": 4500,  # ¥ minimum
            "max_weight_kg": 50,
            "best_for": "High-value items, fastest delivery",
        },
        "fedex": {
            "name": "FedEx International Priority",
            "days": "2-5 business days",
            "tracking": "Yes (full, real-time)",
            "insurance": "Full value, buy separately",
            "cost_per_kg": 3200,  # ¥ per kg (approximate)
            "min_cost": 4200,  # ¥ minimum
            "max_weight_kg": 50,
            "best_for": "High-value items, reliable tracking",
        },
        "surface_mail": {
            "name": "Surface Mail (Seamail)",
            "days": "60-90 business days",
            "tracking": "Limited",
            "insurance": "Limited",
            "cost_per_kg": 900,  # ¥ per kg
            "min_cost": 1200,
            "max_weight_kg": 30,
            "best_for": "Heavy/cheap items where speed doesn't matter",
        },
    },
    "jp_to_eu": {
        "epacket": {
            "name": "ePacket",
            "days": "10-20 business days",
            "tracking": "Yes (basic)",
            "cost_per_100g": 160,
            "min_cost": 1100,
            "max_weight_kg": 2,
        },
        "ems": {
            "name": "EMS",
            "days": "5-10 business days",
            "tracking": "Yes (full)",
            "cost_per_100g": 280,
            "min_cost": 2000,
            "max_weight_kg": 30,
        },
        "dhl": {
            "name": "DHL Express",
            "days": "3-5 business days",
            "tracking": "Yes (full)",
            "cost_per_kg": 4000,
            "min_cost": 5000,
            "max_weight_kg": 50,
        },
    },
}

# ===== TARIFFS / DUTIES =====

# US de minimis threshold: $800 (duty-free for packages under $800)
# EU de minimis: €150 (duty-free for packages under €150)
# UK de minimis: £135

TARIFF_RATES = {
    "us": {
        "de_minimis_threshold_usd": 800,
        "tariff_rates": {
            "anime_figure": 0.00,       # Toys/figures: generally duty-free
            "trading_cards": 0.00,      # Trading cards: duty-free
            "vintage_camera": 0.035,    # Cameras: ~3.5%
            "japanese_knife": 0.065,    # Knives/cutlery: ~6.5%
            "streetwear": 0.12,         # Clothing: ~12%
            "vintage_electronics": 0.025,  # Electronics: ~2.5%
            "japanese_ceramics": 0.05,  # Ceramics: ~5%
            "retro_games": 0.00,        # Games: duty-free
            "vinyl_records": 0.00,      # Vinyl: duty-free
        },
        "notes": "Under $800 = no duty. Over $800 = duty on excess amount only. Use eBay GSP to collect duties from buyer.",
        "harmonized_codes": {
            "anime_figure": "9503.00",
            "trading_cards": "9504.90",
            "vintage_camera": "9006.59",
            "japanese_knife": "8211.91",
            "streetwear": "61-62 series",
            "vintage_electronics": "8525-8528",
            "japanese_ceramics": "6911-6913",
            "retro_games": "9504.30",
            "vinyl_records": "8523.80",
        },
    },
    "eu": {
        "de_minimis_threshold_usd": 150,  # €150
        "vat_rate": 0.20,  # ~20% average EU VAT
        "tariff_rates": {
            "anime_figure": 0.045,
            "trading_cards": 0.045,
            "vintage_camera": 0.00,
            "japanese_knife": 0.025,
            "streetwear": 0.12,
            "vintage_electronics": 0.00,
            "japanese_ceramics": 0.035,
            "retro_games": 0.00,
            "vinyl_records": 0.00,
        },
        "notes": "Under €150 = VAT-free. Over €150 = duty + VAT on full value. IOSS simplifies VAT collection.",
    },
    "uk": {
        "de_minimis_threshold_usd": 135,  # £135
        "vat_rate": 0.20,  # 20% UK VAT
        "tariff_rates": {
            "anime_figure": 0.00,
            "trading_cards": 0.00,
            "vintage_camera": 0.00,
            "japanese_knife": 0.00,
            "streetwear": 0.12,
            "vintage_electronics": 0.00,
            "japanese_ceramics": 0.00,
            "retro_games": 0.00,
            "vinyl_records": 0.00,
        },
        "notes": "Under £135 = VAT-free. Over £135 = duty + 20% VAT. UK VAT is collected from buyer.",
    },
    "canada": {
        "de_minimis_threshold_usd": 20,  # CAD $20 — very low!
        "vat_rate": 0.13,  # ~13% HST/GST average
        "tariff_rates": {
            "anime_figure": 0.065,
            "trading_cards": 0.065,
            "vintage_camera": 0.00,
            "japanese_knife": 0.07,
            "streetwear": 0.18,
            "vintage_electronics": 0.00,
            "japanese_ceramics": 0.05,
            "retro_games": 0.00,
            "vinyl_records": 0.00,
        },
        "notes": "Very low de minimis (CAD $20). Expect duties on almost all shipments. USPS → Canada Post handoff.",
    },
}

# ===== PLATFORM RECOMMENDATION BY CATEGORY =====

CATEGORY_PLATFORM_RECOMMENDATION = {
    "anime_figure": {
        "primary": "ebay_us",
        "secondary": ["amazon_us", "mercari_us"],
        "why": "eBay has the largest collector base for anime figures. Rare/limited items command 2-5x premiums. Global Shipping Program eliminates tariff risk.",
        "price_range_usd": "30-500",
        "sweet_spot": "50-200",
    },
    "trading_cards": {
        "primary": "ebay_us",
        "secondary": ["amazon_us"],
        "why": "eBay dominates trading card sales. TCGPlayer for US domestic. Japanese cards command premiums on eBay.",
        "price_range_usd": "5-5000",
        "sweet_spot": "20-300",
    },
    "vintage_camera": {
        "primary": "ebay_us",
        "secondary": ["etsy"],
        "why": "eBay is the go-to for vintage camera collectors. Japanese cameras (Leica, Nikon, Canon) have huge global demand.",
        "price_range_usd": "50-2000",
        "sweet_spot": "100-500",
    },
    "japanese_knife": {
        "primary": "ebay_us",
        "secondary": ["amazon_us", "etsy"],
        "why": "Japanese knives have cult following on eBay. Etsy good for artisan/blacksmith pieces.",
        "price_range_usd": "50-500",
        "sweet_spot": "80-300",
    },
    "streetwear": {
        "primary": "ebay_us",
        "secondary": ["grailed", "poshmark"],
        "why": "eBay for reach, Grailed for streetwear-savvy buyers who pay premiums for Japanese brands (BAPE, Visvim, CDG).",
        "price_range_usd": "30-500",
        "sweet_spot": "50-300",
    },
    "vintage_electronics": {
        "primary": "ebay_us",
        "secondary": ["etsy"],
        "why": "Japanese vintage audio/electronics have dedicated collector base on eBay. High margins on rare pieces.",
        "price_range_usd": "30-1000",
        "sweet_spot": "50-400",
    },
    "japanese_ceramics": {
        "primary": "ebay_us",
        "secondary": ["etsy"],
        "why": "Etsy's vintage policy fits ceramics well. eBay for higher-end pieces (Noritake, Kutani).",
        "price_range_usd": "20-300",
        "sweet_spot": "30-150",
    },
    "retro_games": {
        "primary": "ebay_us",
        "secondary": ["amazon_us"],
        "why": "Japanese retro games command huge premiums. eBay auction format maximizes price for rare titles.",
        "price_range_usd": "10-500",
        "sweet_spot": "20-200",
    },
    "vinyl_records": {
        "primary": "ebay_us",
        "secondary": ["etsy", "discogs"],
        "why": "Japanese pressings are highly collectible. Discogs is specialized but smaller audience.",
        "price_range_usd": "10-300",
        "sweet_spot": "20-150",
    },
    "vintage_toys": {
        "primary": "ebay_us",
        "secondary": ["etsy"],
        "why": "Japanese vintage toys (Bandai, vintage robots) have massive collector market on eBay.",
        "price_range_usd": "20-500",
        "sweet_spot": "30-200",
    },
}


def get_platform_recommendation(category: str) -> dict:
    """Get the best platforms for a product category."""
    return CATEGORY_PLATFORM_RECOMMENDATION.get(category, {
        "primary": "ebay_us",
        "secondary": [],
        "why": "eBay is the default — largest audience, best for rare/collectible items",
        "price_range_usd": "unknown",
        "sweet_spot": "unknown",
    })


def get_shipping_estimate(weight_kg: float, destination: str = "us") -> dict:
    """Get shipping cost estimates for a given weight and destination."""
    route = SHIPPING_ROUTES.get(f"jp_to_{destination}", SHIPPING_ROUTES["jp_to_us"])
    estimates = {}
    for carrier, info in route.items():
        if weight_kg > info.get("max_weight_kg", 999):
            continue
        if "cost_per_100g" in info:
            cost_jpy = max(info["min_cost"], weight_kg * 1000 / 100 * info["cost_per_100g"])
        elif "cost_per_kg" in info:
            cost_jpy = max(info.get("min_cost", 0), weight_kg * info["cost_per_kg"])
        else:
            cost_jpy = info.get("min_cost", 0)
        estimates[carrier] = {
            "name": info["name"],
            "cost_jpy": cost_jpy,
            "cost_usd": cost_jpy * 0.0067,
            "days": info["days"],
            "tracking": info.get("tracking", "Unknown"),
        }
    return estimates


def get_tariff_estimate(category: str, item_value_usd: float, destination: str = "us") -> dict:
    """Estimate tariffs/duties for shipping to a destination."""
    tariff = TARIFF_RATES.get(destination, TARIFF_RATES["us"])
    rate = tariff["tariff_rates"].get(category, 0.05)  # default 5%
    threshold = tariff["de_minimis_threshold_usd"]

    if item_value_usd <= threshold:
        duty = 0.0
        notes = f"Under {threshold} USD de minimis — no duty"
    else:
        excess = item_value_usd - threshold
        duty = excess * rate
        notes = f"Over {threshold} USD threshold — duty on ${excess:.0f} excess at {rate:.1%}"

    vat = 0.0
    if destination in ("eu", "uk", "canada") and item_value_usd > threshold:
        vat_rate = tariff.get("vat_rate", 0)
        vat = item_value_usd * vat_rate
        notes += f" + {vat_rate:.0%} VAT (${vat:.2f})"

    return {
        "duty": duty,
        "vat": vat,
        "total_fees": duty + vat,
        "effective_rate": (duty + vat) / item_value_usd if item_value_usd > 0 else 0,
        "notes": notes,
        "harmonized_code": tariff.get("harmonized_codes", {}).get(category, "N/A"),
    }
