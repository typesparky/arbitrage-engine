#!/usr/bin/env python3.12
"""Lightweight scanner — scan Mercari JP, analyze candidates, push to Telegram.

Usage:
    python scripts/scan.py --query "anime figure rare" --max 20
    python scripts/scan.py --query "vintage camera nikon" --max 10 --min-score 60
    python scripts/scan.py --query "streetwear bape" --max 15 --dry-run
    python scripts/scan.py --preset anime --max 25

Presets:
    anime       → "anime figure rare limited"
    camera      → "vintage camera nikon canon leica"
    knife       → "japanese knife damascus"
    streetwear  → "streetwear bape supreme vintage"
    cards       → "pokemon card rare holo"
    electronics → "vintage audio sony pioneer"
    games       → "retro game japan nintendo sony"
    ceramics    → "japanese pottery ceramic noritake"
    vinyl       → "vinyl record japanese pressing obi"
    toys        → "vintage toy japan robot godzilla"

Options:
    --query     Search query (Japanese works best)
    --preset    Use a preset query
    --max       Max items to scrape (default 20)
    --min-score Minimum composite score to alert on (default 50)
    --dry-run   Print alerts instead of sending to Telegram
    --top       Number of top candidates to alert on (default 5)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.config.settings import settings
from src.processors.pricing import (
    PricingEngine,
    MarketPriceData,
)
from src.processors.product_id import ProductIdentifier, CATEGORY_TAXONOMY
from src.processors.platform_advisor import (
    get_platform_recommendation,
    get_shipping_estimate,
    get_tariff_estimate,
    TARGET_PLATFORMS,
)
from src.processors.market_data import (
    EbayMarketData,
    estimate_time_to_sale,
    validate_logistics,
)
from src.processors.image_restyler import AIRestyler
from src.scrapers.mercari_browser import MercariBrowserScraper
from src.utils.alerts import TelegramAlerter

PRESETS = {
    "anime": "anime figure rare limited 限定",
    "camera": "vintage camera nikon canon leica カメラ",
    "knife": "japanese knife damascus 包丁 刃物",
    "streetwear": "streetwear bape supreme vintage 古着",
    "cards": "pokemon card rare holo ポケモン カード",
    "electronics": "vintage audio sony pioneer オーディオ",
    "games": "retro game japan nintendo ゲーム レトロ",
    "ceramics": "japanese pottery ceramic noritake 陶器",
    "vinyl": "vinyl record japanese pressing obi レコード",
    "toys": "vintage toy japan robot godzilla 玩具 ロボット",
}


async def run_scan(
    query: str,
    max_items: int = 20,
    min_score: float = 50.0,
    top_n: int = 5,
    dry_run: bool = False,
) -> None:
    """Run a scan: scrape → identify → price → alert."""
    start = time.monotonic()

    scraper = MercariBrowserScraper()
    ebay = EbayFindingClient()
    pricing = PricingEngine()
    product_id = ProductIdentifier()
    alerter = TelegramAlerter()

    if dry_run:
        logger.info("🔍 DRY RUN — alerts will be printed, not sent")

    logger.info(f"🔍 Scanning Mercari JP: '{query}' (max={max_items})")

    # Step 1: Scrape
    result = await scraper.search(query, max_items=max_items)
    logger.info(f"📦 Scraped {len(result.items)} items in {result.duration_seconds:.1f}s")

    if not result.items:
        logger.warning("No items found")
        return

    # Step 2: Analyze each item
    candidates = []
    viable = 0
    rejected = 0

    for item in result.items:
        try:
            # Clean title — remove price prefix (e.g. "€4.59", "¥1,234")
            import re as _re
            clean_title = _re.sub(r'^[€$¥]\s*[\d,.]+\s*', '', item.title).strip()
            if not clean_title:
                clean_title = item.title

            # Identify
            product = product_id.identify(
                title=clean_title,
                description=item.description,
                category=item.category,
                price=item.price,
                condition=item.condition,
            )

            # Get real market data
            ebay_data = EbayMarketData()
            market_analysis = await ebay_data.search_sold(
                query=clean_title, max_results=30
            )

            # Use real data if available, otherwise fallback
            # Use real data if available, otherwise fallback multiplier
            if market_analysis.confidence in ("high", "medium") and market_analysis.median_price > 0:
                median_price = market_analysis.median_price
                data_confidence = market_analysis.confidence
                data_source = "ebay_scrape"
            else:
                # Fallback: 4x multiplier on source price
                median_price = item.price * pricing.jpy_to_usd * 4.0
                data_confidence = "low"
                data_source = "fallback_4x"

            market = MarketPriceData(
                source_price_jpy=item.price,
                source_price_usd=item.price * pricing.jpy_to_usd,
                target_median_price=median_price,
                target_avg_price=market_analysis.avg_price if market_analysis.avg_price > 0 else median_price * 0.95,
                target_min_price=market_analysis.min_price,
                target_max_price=market_analysis.max_price,
                target_25th_percentile=market_analysis.p25_price or median_price * 0.8,
                target_75th_percentile=market_analysis.p75_price or median_price * 1.2,
                num_sold_listings=market_analysis.total_sold,
            )

            # Profit analysis
            cat_info = CATEGORY_TAXONOMY.get(product.category, {})
            profit = pricing.analyze(
                source_price_jpy=item.price,
                market_data=market,
                category=product.category,
                item_weight_kg=cat_info.get("avg_weight_kg", 0.3),
                item_condition=product.condition_normalized,
                is_rare=product.rarity_score > 30,
            )

            # Time-to-sale estimate
            tts = estimate_time_to_sale(
                category=product.category,
                listing_price=profit.listing_price,
                market_data=market,
            )

            # Logistics feasibility check
            logistics = validate_logistics(
                source_price_jpy=item.price,
                listing_price_usd=profit.listing_price,
                category=product.category,
                weight_kg=cat_info.get("avg_weight_kg", 0.3),
                proxy_service="buyee",
                destination="us",
            )

            # Platform recommendation
            plat = get_platform_recommendation(product.category)
            plat_info = TARGET_PLATFORMS.get(plat["primary"], {})
            plat_name = plat_info.get("name", "eBay US")
            plat_url = plat_info.get("url", "https://www.ebay.com")

            # Shipping estimate
            cat_info = CATEGORY_TAXONOMY.get(product.category, {})
            weight = cat_info.get("avg_weight_kg", 0.3)
            shipping = get_shipping_estimate(weight, "us")
            ship_lines = []
            for carrier, est in shipping.items():
                if carrier in ("epacket", "ems", "dhl_express"):
                    ship_lines.append(f"  {est['name']}: ${est['cost_usd']:.0f} ({est['days']})")
            shipping_str = "\n".join(ship_lines) if ship_lines else f"  ~${weight * 25:.0f} est."

            # Tariff estimate
            tariff = get_tariff_estimate(product.category, profit.listing_price, "us")
            tariff_str = tariff["notes"]
            if tariff["total_fees"] > 0:
                tariff_str += f"\n  Est. fees: ${tariff['total_fees']:.2f} ({tariff['effective_rate']:.1%})"
            tariff_str += f"\n  HS Code: {tariff['harmonized_code']}"

            # Simple composite score
            profit_score = min(100, max(0, profit.net_profit * 3 + profit.net_margin_pct * 0.3))
            velocity_score = max(0, 100 - profit.expected_days_to_sell * 3)
            risk_score = max(0, 100 - profit.stockout_probability * 200)
            composite = profit_score * 0.4 + velocity_score * 0.3 + risk_score * 0.3

            if profit.is_viable and composite >= min_score:
                viable += 1
                priority = "high" if composite >= 70 else "medium" if composite >= 55 else "low"

                candidate = {
                    "title": clean_title,
                    "title_original": item.title,
                    "brand": product.brand,
                    "category": product.category,
                    "source_price_jpy": item.price,
                    "source_price_usd": profit.source_cost,
                    "listing_price": profit.listing_price,
                    "net_profit": logistics.net_profit,
                    "net_margin_pct": logistics.net_margin_pct,
                    "roi_pct": logistics.roi_pct,
                    "profit_per_day": logistics.profit_per_day,
                    "days_to_sell": tts.days_expected,
                    "composite_score": composite,
                    "priority": priority,
                    "source_url": item.url,
                    "ebay_median": market_analysis.median_price,
                    "ebay_sold_count": market_analysis.total_sold,
                    "data_confidence": data_confidence,
                    "condition": product.condition_normalized,
                    "seller_rating": item.seller_rating,
                    "warnings": (logistics.warnings if isinstance(logistics.warnings, list) else []) + profit.rejection_reasons,
                    "platform_name": plat_name,
                    "platform_url": plat_url,
                    "shipping_est": shipping_str,
                    "tariff_note": tariff_str,
                    "weight_kg": weight,
                    "logistics_feasible": logistics.is_feasible,
                    "total_cost": logistics.total_cost,
                    "cost_breakdown": dict(logistics.breakdown) if isinstance(logistics.breakdown, dict) else {},
                    "tts_min": tts.days_min,
                    "tts_max": tts.days_max,
                }
                candidates.append(candidate)

                # AI restyle the product images for listing
                if item.image_urls:
                    try:
                        restyler = AIRestyler(
                            style=settings.image_restyle_style,
                            backend=settings.image_restyle_backend,
                        )
                        restyle_result = await restyler.restyle_listing_images(
                            image_urls=item.image_urls,
                            listing_id=item.external_id,
                            max_images=4,
                        )
                        # Store restyled image paths in candidate
                        candidate["restyled_images"] = [
                            img.restyled_path
                            for img in restyle_result.images
                            if img.success and img.restyled_path
                        ]
                        candidate["original_images"] = [
                            img.local_path
                            for img in restyle_result.images
                            if img.local_path
                        ]
                        logger.info(
                            f"  🖼️ Restyled {restyle_result.successful}/{restyle_result.total} images"
                        )
                        await restyler.close()
                    except Exception as e:
                        logger.debug(f"  Image restyle failed: {e}")

                logger.info(
                    f"  ✅ {priority.upper()}: {clean_title[:50]}... "
                    f"${profit.net_profit:.0f} ({profit.net_margin_pct:.0f}%) "
                    f"Score:{composite:.0f}"
                )
            else:
                rejected += 1
                logger.debug(f"  ❌ {clean_title[:50]}: {profit.rejection_reasons[:100]}")

        except Exception as e:
            logger.debug(f"  Error: {item.title[:50]}: {e}")
            rejected += 1

    # Sort by composite score
    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    elapsed = time.monotonic() - start
    logger.info(
        f"\n📊 Results: {len(result.items)} scraped, {viable} viable, {rejected} rejected "
        f"({elapsed:.1f}s total)"
    )

    # Step 3: Send alerts for top candidates
    top = candidates[:top_n]

    if not top:
        logger.info("No viable candidates above threshold")
        if not dry_run:
            await alerter.send_scan_summary(
                query=query,
                total_scraped=len(result.items),
                total_viable=viable,
                total_rejected=rejected,
                top_candidates=[],
                duration_seconds=elapsed,
            )
        return

    logger.info(f"\n🚀 Sending {len(top)} candidate alerts...")

    for c in top:
        if dry_run:
            # Print the alert as it would appear on Telegram
            print(f"\n{'='*50}")
            print(f"📱 TELEGRAM ALERT ({c['priority'].upper()})")
            print(f"{'='*50}")
            prio_emoji = {"high": "🟢", "medium": "🟡", "low": "🟠"}.get(c["priority"], "⚪")
            print(f"\n{prio_emoji} {c['priority'].upper()} CANDIDATE")
            print(f"\n📦 {c['title'][:80]}")
            if c["brand"]:
                print(f"🏷 {c['brand']} • {c['category'].replace('_', ' ').title()}")
            print(f"\n── PRICING ──")
            print(f"💰 Source: ¥{c['source_price_jpy']:,.0f}  (${c['source_price_usd']:.0f})")
            print(f"🏷 List:   ${c['listing_price']:.2f}")
            if c["ebay_median"] > 0:
                print(f"📊 eBay median: ${c['ebay_median']:.0f}  ({c['ebay_sold_count']} sold)")
            print(f"\n── PROFIT ──")
            print(f"💵 Net:    ${c['net_profit']:.2f}  ({c['net_margin_pct']:.0f}% margin)")
            print(f"📈 ROI:    {c['roi_pct']:.0f}%")
            print(f"⚡ ${c['profit_per_day']:.2f}/day  •  ~{c['days_to_sell']:.0f} days to sell")
            print(f"\n🎯 Score: {c['composite_score']:.0f}/100")
            if c["warnings"]:
                print(f"\n── WARNINGS ──")
                for w in c["warnings"][:3]:
                    print(f"⚠️ {w}")
            print(f"\n🔗 {c['source_url']}")
            print(f"\n{'='*50}")
        else:
            await alerter.send_candidate(
                title=c["title"],
                title_original=c.get("title_original", c["title"]),
                brand=c["brand"],
                category=c["category"],
                source_price_jpy=c["source_price_jpy"],
                source_price_usd=c["source_price_usd"],
                listing_price=c["listing_price"],
                net_profit=c["net_profit"],
                net_margin_pct=c["net_margin_pct"],
                roi_pct=c["roi_pct"],
                profit_per_day=c["profit_per_day"],
                days_to_sell=c["days_to_sell"],
                composite_score=c["composite_score"],
                priority=c["priority"],
                source_url=c["source_url"],
                platform_name=c.get("platform_name", "eBay US"),
                platform_url=c.get("platform_url", "https://www.ebay.com"),
                shipping_est=c.get("shipping_est", ""),
                tariff_note=c.get("tariff_note", ""),
                weight_kg=c.get("weight_kg", 0.3),
                logistics_feasible=c.get("logistics_feasible", True),
                ebay_median=c.get("ebay_median", 0),
                ebay_sold_count=c.get("ebay_sold_count", 0),
                seller_rating=c["seller_rating"],
                warnings=c["warnings"],
                cost_breakdown=c.get("cost_breakdown", {}),
            )
            await asyncio.sleep(0.5)  # Rate limit Telegram messages

    # Step 4: Send summary
    if not dry_run:
        await alerter.send_scan_summary(
            query=query,
            total_scraped=len(result.items),
            total_viable=viable,
            total_rejected=rejected,
            top_candidates=top,
            duration_seconds=elapsed,
        )

    # Cleanup
    await scraper.close()
    await ebay.close()


def main():
    parser = argparse.ArgumentParser(description="Scan Mercari JP for arbitrage candidates")
    parser.add_argument("--query", "-q", help="Search query")
    parser.add_argument("--preset", "-p", choices=PRESETS.keys(), help="Use a preset query")
    parser.add_argument("--top", "-t", type=int, default=5, help="Top N candidates to alert")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Print instead of sending")
    parser.add_argument("--restyle", "-r", action="store_true", help="AI restyle product images")
    parser.add_argument(
        default="vintage_classy",
        help="Image restyle preset",
    )

    args = parser.parse_args()

    if args.preset:
        query = PRESETS[args.preset]
        logger.info(f"Using preset '{args.preset}': {query}")
    elif args.query:
        query = args.query
    else:
        parser.print_help()
        print(f"\nAvailable presets: {', '.join(PRESETS.keys())}")
        sys.exit(1)

    asyncio.run(
        run_scan(
            query=query,
            max_items=args.max,
            min_score=args.min_score,
            top_n=args.top,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
