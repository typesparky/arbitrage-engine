"""Product identification and matching system.

Identifies products across platforms using:
1. Brand + Model matching (fuzzy)
2. MPN/UPC/EAN/ISBN extraction
3. Title normalization and keyword extraction
4. Image similarity (for visual matching)

This lets us:
- Match a Mercari JP listing to eBay sold listings for the same product
- Avoid listing counterfeits (brand mismatch detection)
- Identify the exact product variant (color, size, edition)
- Track the same product across platforms over time
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# --- Known brand database (expandable) ---
# In production, this would be a proper database. For now, curated lists.

ANIME_FIGURE_BRANDS = {
    "good smile", "good smile company", "gsc", "max factory", "bandai",
    "bandai spirits", "sentinel", "alpha x omega", "aniplex", "alter",
    "kotobukiya", "kotobukiya", "freeing", "wing", "phat company",
    "mega house", "banpresto", "square enix", "furyu", "f:nex",
    "sideshow", "hot toys", "medicom toy", "medicom", "revoltech",
    "threezero", "storm collectibles", "neca", "tamashii nations",
    "shfiguarts", "figma", "nendoroid", "scale figure", "prize figure",
}

CAMERA_BRANDS = {
    "canon", "nikon", "sony", "fujifilm", "fuji", "olympus", "panasonic",
    "leica", "zeiss", "hasselblad", "pentax", "ricoh", "sigma", "tamron",
    "voigtlander", "cosina", "minolta", "kodak", "polaroid", "lomography",
    "mamiya", "bronica", "contax", "kiev", "zenit", "nikkor", "canon ef",
    "canon rf", "sony fe", "sony e-mount",
}

JAPANESE_KNIFE_BRANDS = {
    "global", "shun", "miyabi", "yoshihiro", "masamoto", "tojiro",
    "sakai takayuki", "misono", "mac", "dalstrong", "yoshikin",
    "kasumi", "fujiwara", "suisin", "sakai", "takeda", "aoki",
    "ishikawa", "nippontsusho", "hitachi", "blue steel", "white steel",
    "vg-10", "sg2", "zdp-189", "aus-8", "d2 steel",
}

STREETWEAR_BRANDS = {
    "bape", "a bathing ape", "supreme", "kith", "palace", "stussy",
    "visvim", "comme des garçons", "cdg", "yohji yamamoto", "y-3",
    "undercover", "neighborhood", "wtaps", "mastermind japan", "mmj",
    "number nine", "nike", "adidas", "new balance", "asics", "converse",
    "vans", "jordan", "off-white", "travis scott", "sacai", "cdg play",
    "kenzo", "louis vuitton", "gucci", "prada", "balenciaga",
}

TRADING_CARD_BRANDS = {
    "pokemon", "pokémon", "yu-gi-oh", "yugioh", "magic the gathering",
    "mtg", "digimon", "one piece", "dragon ball", "saint seiya",
    "cardfight vanguard", "weiss schwarz", "flesh and blood",
}

ELECTRONICS_BRANDS = {
    "sony", "panasonic", "pioneer", "onkyo", "denon", "marantz",
    "technics", "jvc", "sharp", "toshiba", "hitachi", "mitsubishi",
    "kenwood", "yamaha", "bose", "sennheiser", "audio-technica",
    "akg", "beyerdynamic", "focal", "hifiman", "campfire audio",
}

ALL_BRANDS = (
    ANIME_FIGURE_BRANDS | CAMERA_BRANDS | JAPANESE_KNIFE_BRANDS
    | STREETWEAR_BRANDS | TRADING_CARD_BRANDS | ELECTRONICS_BRANDS
)

# --- Category taxonomy ---
# Maps source platform categories to normalized internal categories

CATEGORY_TAXONOMY = {
    # Anime Figures
    "anime_figure": {
        "keywords": ["figure", "nendoroid", "figma", "scale figure", "prize figure",
                      "anime", "mcharacter", "girl", "boy", "statue", "bust"],
        "mercari_categories": ["hobbies", "figures", "anime"],
        "ebay_category_id": "156874",  # Action Figures & Accessories
        "avg_weight_kg": 0.3,
        "avg_shipping_usd": 20,
        "demand_score": 8,
        "competition_score": 6,
        "typical_margin_pct": 150,
        "price_range_usd": (20, 500),
    },
    # Vintage Cameras
    "vintage_camera": {
        "keywords": ["camera", "lens", "nikon", "canon", "leica", "film camera",
                      "vintage camera", "rangefinder", "slr", "dslr", "mirrorless",
                      "vintage lens", "manual focus"],
        "mercari_categories": ["cameras", "electronics", "hobbies"],
        "ebay_category_id": "15200",  # Cameras & Photo
        "avg_weight_kg": 0.8,
        "avg_shipping_usd": 30,
        "demand_score": 7,
        "competition_score": 5,
        "typical_margin_pct": 120,
        "price_range_usd": (30, 2000),
    },
    # Japanese Knives
    "japanese_knife": {
        "keywords": ["knife", "chef knife", "japanese knife", "gyuto", "santoku",
                      "nakiri", "bunka", "deba", "yanagiba", "kiritsuke",
                      "damascus", "carbon steel", "stainless"],
        "mercari_categories": ["kitchen", "knives", "home"],
        "ebay_category_id": "177009",  # Kitchen Knives
        "avg_weight_kg": 0.3,
        "avg_shipping_usd": 15,
        "demand_score": 7,
        "competition_score": 4,
        "typical_margin_pct": 150,
        "price_range_usd": (30, 500),
    },
    # Streetwear / Fashion
    "streetwear": {
        "keywords": ["hoodie", "t-shirt", "jacket", "sneakers", "shoes",
                      "streetwear", "vintage clothing", "designer", "limited",
                      "collab", "collaboration", "exclusive", "rare"],
        "mercari_categories": ["fashion", "clothing", "shoes", "accessories"],
        "ebay_category_id": "11450",  # Clothing, Shoes & Accessories
        "avg_weight_kg": 0.5,
        "avg_shipping_usd": 20,
        "demand_score": 9,
        "competition_score": 8,
        "typical_margin_pct": 100,
        "price_range_usd": (20, 1000),
    },
    # Trading Cards
    "trading_cards": {
        "keywords": ["card", "pokemon", "yu-gi-oh", "magic", "mtg", "booster",
                      "pack", "box", "single", "holo", "secret rare", "1st edition",
                      "shadowless", "charizard", "blue eyes"],
        "mercari_categories": ["hobbies", "trading cards", "games"],
        "ebay_category_id": "2536",  # Trading Cards
        "avg_weight_kg": 0.05,
        "avg_shipping_usd": 8,
        "demand_score": 9,
        "competition_score": 7,
        "typical_margin_pct": 200,
        "price_range_usd": (5, 5000),
    },
    # Vintage Electronics
    "vintage_electronics": {
        "keywords": ["vintage", "retro", "classic", "old", "antique",
                      "turntable", "amplifier", "receiver", "speaker",
                      "headphone", "walkman", "discman", "tape deck",
                      "reel to reel", "vintage audio"],
        "mercari_categories": ["electronics", "audio", "hobbies"],
        "ebay_category_id": "14969",  # Vintage Electronics
        "avg_weight_kg": 2.0,
        "avg_shipping_usd": 35,
        "demand_score": 6,
        "competition_score": 4,
        "typical_margin_pct": 130,
        "price_range_usd": (20, 2000),
    },
    # Japanese Ceramics
    "japanese_ceramics": {
        "keywords": ["ceramic", "pottery", "porcelain", "noritake", "kutani",
                      "arita", "imari", "seto", "mino", "bizen", "raku",
                      "tea cup", "sake cup", "plate", "bowl", "vase"],
        "mercari_categories": ["home", "kitchen", "decor", "antiques"],
        "ebay_category_id": "10024",  # Asian Antiques
        "avg_weight_kg": 0.5,
        "avg_shipping_usd": 25,
        "demand_score": 5,
        "competition_score": 3,
        "typical_margin_pct": 180,
        "price_range_usd": (15, 500),
    },
    # Retro Games
    "retro_games": {
        "keywords": ["game", "nes", "snes", "n64", "gameboy", "gba",
                      "sega", "genesis", "dreamcast", "ps1", "ps2",
                      "atari", "arcade", "cartridge", "disc", "japan",
                      "japanese", "import game", "retro game"],
        "mercari_categories": ["hobbies", "games", "retro"],
        "ebay_category_id": "139973",  # Video Games & Consoles
        "avg_weight_kg": 0.2,
        "avg_shipping_usd": 12,
        "demand_score": 8,
        "competition_score": 6,
        "typical_margin_pct": 150,
        "price_range_usd": (10, 1000),
    },
    # Vinyl Records
    "vinyl_records": {
        "keywords": ["vinyl", "lp", "record", "album", "single", "ep",
                      "12 inch", "7 inch", "japanese pressing", "obi",
                      "first pressing", "limited edition", "colored vinyl"],
        "mercari_categories": ["music", "records", "hobbies"],
        "ebay_category_id": "176985",  # Records
        "avg_weight_kg": 0.3,
        "avg_shipping_usd": 15,
        "demand_score": 7,
        "competition_score": 5,
        "typical_margin_pct": 140,
        "price_range_usd": (10, 500),
    },
    # Vintage Toys
    "vintage_toys": {
        "keywords": ["toy", "action figure", "transformers", "gundam",
                      "model kit", "die cast", "tin toy", "robot",
                      "vintage toy", "retro toy", "80s", "90s"],
        "mercari_categories": ["hobbies", "toys", "vintage"],
        "ebay_category_id": "2613",  # Toys & Hobbies
        "avg_weight_kg": 0.4,
        "avg_shipping_usd": 20,
        "demand_score": 7,
        "competition_score": 5,
        "typical_margin_pct": 160,
        "price_range_usd": (10, 800),
    },
}


@dataclass
class ProductIdentity:
    """Identified product with normalized attributes."""

    # Identification
    brand: str = ""
    model: str = ""
    mpn: str = ""  # Manufacturer Part Number
    upc: str = ""
    ean: str = ""
    isbn: str = ""

    # Classification
    category: str = ""  # Normalized category key
    subcategory: str = ""
    tags: list[str] = field(default_factory=list)

    # Attributes
    color: str = ""
    size: str = ""
    edition: str = ""  # e.g., "1st Edition", "Limited", "Exclusive"
    variant: str = ""  # e.g., "Blue", "Large", "Complete in Box"
    country_of_origin: str = "Japan"

    # Condition
    condition_original: str = ""
    condition_normalized: str = ""  # "new", "like_new", "good", "fair", "poor"

    # Rarity signals
    is_limited: bool = False
    is_vintage: bool = False
    is_import: bool = True  # Most items from Japan are imports
    rarity_score: float = 0.0  # 0-100

    # Matching confidence
    identification_confidence: float = 0.0  # 0-1


class ProductIdentifier:
    """Identifies and normalizes product information from raw listing data."""

    def identify(
        self,
        title: str,
        description: str = "",
        category: str = "",
        price: float = 0.0,
        **kwargs: Any,
    ) -> ProductIdentity:
        """Identify a product from its listing data."""
        identity = ProductIdentity()

        # Extract brand
        identity.brand = self._extract_brand(title, description)

        # Extract model/MPN
        identity.model = self._extract_model(title, description)
        identity.mpn = self._extract_mpn(title, description)
        identity.upc = self._extract_upc(title, description)

        # Classify category
        identity.category = self._classify_category(title, description, category)

        # Extract attributes
        identity.color = self._extract_color(title, description)
        identity.edition = self._extract_edition(title, description)
        identity.condition_normalized = self._normalize_condition(
            kwargs.get("condition", "")
        )

        # Rarity signals
        identity.is_limited = self._detect_limited(title, description)
        identity.is_vintage = self._detect_vintage(title, description)
        identity.rarity_score = self._calculate_rarity(identity, price)

        # Tags
        identity.tags = self._extract_tags(title, description)

        # Confidence
        identity.identification_confidence = self._calculate_confidence(identity)

        return identity

    def _extract_brand(self, title: str, description: str) -> str:
        """Extract brand from title/description using known brand database."""
        text = f"{title} {description}".lower()

        # Check multi-word brands first (longer matches first)
        sorted_brands = sorted(ALL_BRANDS, key=len, reverse=True)
        for brand in sorted_brands:
            if brand in text:
                return brand.title()

        return ""

    def _extract_model(self, title: str, description: str) -> str:
        """Extract model number/name from title."""
        text = f"{title} {description}"

        # Common model patterns
        patterns = [
            # Alphanumeric model numbers: "DMC-GH5", "EOS R5", "α7 IV"
            r'\b([A-Z]{2,4}[-\s]?[A-Z]?\d{1,4}[A-Z]?(?:\s*(?:II|III|IV|V|VI|VII))?)\b',
            # Japanese model numbers: "OM-1", "X-T5", "FX3"
            r'\b([A-Z][-]?\d{1,4}[A-Z]?)\b',
            # Parenthetical models: "(Model XYZ-123)"
            r'\(([A-Z0-9][-A-Z0-9]{2,20})\)',
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()

        return ""

    def _extract_mpn(self, title: str, description: str) -> str:
        """Extract Manufacturer Part Number."""
        text = f"{title} {description}"
        patterns = [
            r'(?:mpn|model\s*(?:no|number|#))\s*[:：]?\s*([A-Z0-9][-A-Z0-9]{2,30})',
            r'(?:品型番|型番)\s*[:：]?\s*([A-Z0-9][-A-Z0-9]{2,30})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    def _extract_upc(self, title: str, description: str) -> str:
        """Extract UPC/EAN barcode."""
        text = f"{title} {description}"
        # UPC-A: 12 digits, EAN-13: 13 digits
        match = re.search(r'\b(\d{12,13})\b', text)
        if match:
            return match.group(1)
        return ""

    def _classify_category(
        self, title: str, description: str, source_category: str
    ) -> str:
        """Classify product into normalized category."""
        text = f"{title} {description} {source_category}".lower()

        best_category = ""
        best_score = 0

        for cat_key, cat_info in CATEGORY_TAXONOMY.items():
            score = 0
            for keyword in cat_info["keywords"]:
                if keyword in text:
                    score += 1
            # Bonus for source category match
            for mercari_cat in cat_info.get("mercari_categories", []):
                if mercari_cat in source_category.lower():
                    score += 3

            if score > best_score:
                best_score = score
                best_category = cat_key

        return best_category

    def _extract_color(self, title: str, description: str) -> str:
        """Extract color from title/description."""
        text = f"{title} {description}".lower()
        colors = [
            "black", "white", "red", "blue", "green", "yellow", "orange",
            "purple", "pink", "brown", "gray", "grey", "silver", "gold",
            "clear", "transparent", "limited color", "exclusive color",
            "midnight", "starlight", "space gray", "rose gold",
        ]
        for color in colors:
            if color in text:
                return color
        return ""

    def _extract_edition(self, title: str, description: str) -> str:
        """Extract edition information."""
        text = f"{title} {description}".lower()
        editions = [
            "1st edition", "first edition", "limited edition", "special edition",
            "anniversary edition", "collector's edition", "exclusive",
            "limited", "rare", "vintage", "retro", "original", "first press",
            "初回限定", "限定版", "特別版",
        ]
        for edition in editions:
            if edition in text:
                return edition
        return ""

    def _normalize_condition(self, condition: str) -> str:
        """Normalize condition to standard values."""
        text = condition.lower()
        if any(w in text for w in ["new", "新品", "未使用", "mint", "mint condition"]):
            return "new"
        if any(w in text for w in ["like new", "未開封", "near mint", "nm"]):
            return "like_new"
        if any(w in text for w in ["good", "very good", "vg", "美品", "良い"]):
            return "good"
        if any(w in text for w in ["fair", "acceptable", "可", "そこそこ"]):
            return "fair"
        if any(w in text for w in ["poor", "bad", "damaged", "傷"]):
            return "poor"
        return "good"  # default

    def _detect_limited(self, title: str, description: str) -> bool:
        """Detect if item is limited edition."""
        text = f"{title} {description}".lower()
        signals = [
            "limited", "exclusive", "限定", "初回", "rare", "hard to find",
            "discontinued", "production ended", "out of print", "oop",
            "event exclusive", "con exclusive", "web exclusive",
        ]
        return any(s in text for s in signals)

    def _detect_vintage(self, title: str, description: str) -> bool:
        """Detect if item is vintage."""
        text = f"{title} {description}".lower()
        signals = [
            "vintage", "retro", "classic", "antique", "old", "年代",
            "1970", "1980", "1990", "2000", "昭和", "平成",
            "used", "中古", "pre-owned",
        ]
        return any(s in text for s in signals)

    def _calculate_rarity(self, identity: ProductIdentity, price: float) -> float:
        """Calculate rarity score 0-100 based on signals."""
        score = 0.0

        if identity.is_limited:
            score += 30
        if identity.is_vintage:
            score += 20
        if identity.edition:
            score += 15
        if identity.brand and identity.brand.lower() in {
            "bape", "supreme", "comme des garçons", "visvim", "leica",
            "hasselblad", "hot toys", "sideshow",
        }:
            score += 20
        # Higher price often correlates with rarity
        if price > 10000:  # >¥10,000
            score += 10
        if price > 50000:  # >¥50,000
            score += 15

        return min(100, score)

    def _extract_tags(self, title: str, description: str) -> list[str]:
        """Extract searchable tags from listing."""
        text = f"{title} {description}".lower()
        tags = []

        tag_patterns = {
            "japanese": ["japan", "日本", "japanese", "import"],
            "rare": ["rare", "限定", "limited", "hard to find"],
            "new": ["new", "新品", "未使用", "mint"],
            "vintage": ["vintage", "retro", "classic", "年代"],
            "complete": ["complete", "full set", "complete set", "box", "boxed"],
            "autographed": ["signed", "autographed", "signature"],
            "damaged": ["damaged", "broken", "parts", "repair", "as is"],
        }

        for tag, patterns in tag_patterns.items():
            if any(p in text for p in patterns):
                tags.append(tag)

        return tags

    def _calculate_confidence(self, identity: ProductIdentity) -> float:
        """Calculate how confident we are in the identification."""
        score = 0.0
        if identity.brand:
            score += 0.3
        if identity.model:
            score += 0.2
        if identity.category:
            score += 0.3
        if identity.mpn or identity.upc:
            score += 0.2
        return min(1.0, score)
