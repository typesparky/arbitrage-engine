"""Main orchestrator — ties scrapers, processors, listers, and sync together.

Usage:
    from src.pipeline import ArbitragePipeline
    pipeline = ArbitragePipeline()
    await pipeline.initialize()
    await pipeline.run_discovery("anime figure")  # find + score candidates
    await pipeline.list_top_candidates(5)  # list top 5 on eBay
    await pipeline.start_sync()  # start inventory monitoring
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.config.settings import settings
from src.listers.ebay_publisher import EbayPublisher, PublishedListing
from src.models.database import (
    Base,
    EbaySoldListing,
    ListingStatus,
    SourceListing,
    TargetListing,
)
from src.processors.enricher import EnrichedListing, LLMEnricher
from src.processors.images import ImageProcessor
from src.processors.scorer import CandidateScorer, CandidateScore
from src.processors.pricing import PricingEngine, MarketPriceData, ProfitAnalysis
from src.processors.product_id import ProductIdentifier
from src.processors.filter_rank import CompositeFilterRank, RankedListing
from src.processors.capital_optimizer import CapitalOptimizer
from src.processors.market_tracker import MarketPriceTracker
from src.scrapers.base import ScrapedItem
from src.scrapers.ebay_finding import EbayFindingClient, EbayPriceAnalysis
from src.scrapers.mercari_jp import MercariJPScraper
from src.sync.inventory_sync import InventorySync
from src.utils.alerts import TelegramAlerter

class ArbitragePipeline:
    """End-to-end arbitrage pipeline."""

    def __init__(self):
        # Scrapers
        self.mercari = MercariJPScraper()
        self.ebay_finding = EbayFindingClient()

        # Processors
        self.enricher = LLMEnricher()
        self.scorer = CandidateScorer()
        self.image_processor = ImageProcessor()
        self.pricing = PricingEngine()
        self.product_id = ProductIdentifier()
        self.filter_rank = CompositeFilterRank()
        self.capital_optimizer = CapitalOptimizer()
        self.market_tracker = None  # initialized after DB setup
        # Listers
        self.ebay_publisher = EbayPublisher()

        # Sync
        self.inventory_sync = InventorySync(scraper=self.mercari)

        # Alerts
        self.alerter = TelegramAlerter()

        # Database
        self.engine = create_async_engine(settings.database_url, echo=settings.debug)
        self.session_factory = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        # State
        self._last_candidates: list[RankedListing] = []
        self._portfolio: dict[str, int] = {}
    async def initialize(self) -> None:
        """Set up database and verify connections."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("[Pipeline] Initialized. Database ready.")

    async def run_discovery(
        self,
        query: str,
        category: str = "",
        max_items: int = 50,
        min_score: float = 40.0,
    ) -> list[RankedListing]:
        """Full intelligence pipeline: scrape → identify → price → filter → rank.

        Stages:
        1. Scrape source (multi-strategy: API → Crawl4AI → CF Bypass → Scrapfly)
        2. Identify products (brand, model, category, rarity)
        3. Get market prices (eBay sold listings)
        4. Analyze profit (fees, shipping, ROI, capital efficiency)
        5. Filter and rank (composite scoring, portfolio constraints)
        6. Optimize capital allocation across categories
        """
        logger.info(f"[Pipeline] Discovery: '{query}' (max={max_items})")

        # Step 1: Scrape source
        scrape_result = await self.mercari.search(query, category=category, max_items=max_items)
        logger.info(f"[Pipeline] Scraped {len(scrape_result.items)} items")

        if not scrape_result.items:
            return []

        # Step 2-4: Identify → Price → Analyze
        candidates = []
        for item in scrape_result.items:
            try:
                product = self.product_id.identify(
                    title=item.title,
                    description=item.description,
                    category=item.category,
                    price=item.price,
                    condition=item.condition,
                )

                ebay_analysis = await self.ebay_finding.search_sold(
                    query=item.title, max_results=30,
                )

                from src.processors.pricing import CATEGORY_TAXONOMY

                market_data = MarketPriceData(
                    source_price_jpy=item.price,
                    source_price_usd=item.price * self.pricing.jpy_to_usd,
                    target_median_price=ebay_analysis.median_price,
                    target_avg_price=ebay_analysis.avg_price,
                    target_min_price=ebay_analysis.min_price,
                    target_max_price=ebay_analysis.max_price,
                    target_25th_percentile=ebay_analysis.median_price * 0.8,
                    target_75th_percentile=ebay_analysis.max_price * 0.75,
                    num_sold_listings=ebay_analysis.total_sold,
                )

                cat_info = CATEGORY_TAXONOMY.get(product.category, {})
                profit = self.pricing.analyze(
                    source_price_jpy=item.price,
                    market_data=market_data,
                    category=product.category,
                    item_weight_kg=cat_info.get("avg_weight_kg", 0.3),
                    item_condition=product.condition_normalized,
                    is_rare=product.rarity_score > 30,
                )

                candidates.append((product, profit, market_data))

                if profit.is_viable:
                    logger.info(
                        f"[Pipeline] ✅ {profit.recommendation.upper()}: {item.title[:45]}... "
                        f"${profit.net_profit:.0f} ({profit.net_margin_pct:.0f}%) "
                        f"ROI:{profit.roi_pct:.0f}% "
                        f"${profit.profit_per_day:.1f}/day"
                    )
                else:
                    logger.debug(
                        f"[Pipeline] ❌ {item.title[:45]}: {profit.rejection_reasons}"
                    )
            except Exception as e:
                logger.error(f"[Pipeline] Error: {item.title[:50]}: {e}")

        # Step 5: Filter and rank
        ranked = self.filter_rank.rank(candidates, self._portfolio)
        self._last_candidates = [r for r in ranked if r.composite_score >= min_score]

        # Step 6: Capital optimization
        plan = self.capital_optimizer.optimize(current_portfolio=self._portfolio)

        viable = sum(1 for r in self._last_candidates if r.should_list)
        logger.info(
            f"[Pipeline] Done: {len(ranked)} analyzed, {viable} viable, "
            f"{len(self._last_candidates)} above threshold"
        )
        logger.info(
            f"[Pipeline] Capital plan: ${plan.total_expected_monthly_profit:.0f}/mo, "
            f"{plan.total_listings} listings, {len(plan.allocations)} categories"
        )
        for rec in plan.recommendations[:3]:
            logger.info(f"[Pipeline] 📊 {rec}")

        return self._last_candidates
    async def list_top_candidates(self, count: int = 5) -> list[PublishedListing]:
        """List the top N ranked candidates on eBay."""
        # Filter to only viable, high-priority candidates
        viable = [r for r in self._last_candidates if r.should_list][:count]
        if not viable:
            logger.warning("[Pipeline] No viable candidates. Run discovery first.")
            return []

        results: list[PublishedListing] = []

        for ranked in viable:
            try:
                product = ranked.product
                profit = ranked.profit
                if not product or not profit:
                    continue

                # Reconstruct source item from ranked listing data
                source = ScrapedItem(
                    external_id=product.mpn or ranked.source_url.split("/")[-1],
                    url=ranked.source_url,
                    title=ranked.source_title,
                    price=ranked.source_price_jpy,
                    currency="JPY",
                    condition=product.condition_normalized,
                    category=product.category,
                    image_urls=[],
                    is_available=True,
                )

                # Save to DB, enrich, publish (same flow as before)
                async with self.session_factory() as session:
                    db_source = SourceListing(
                        source_platform="mercari_jp",
                        external_id=source.external_id,
                        url=source.url,
                        title_original=source.title,
                        description_original="",
                        price_original=source.price,
                        currency=source.currency,
                        price_usd=profit.source_cost,
                        condition=source.condition,
                        category=product.category,
                        seller_rating=0,
                        seller_reviews=0,
                        image_urls="",
                        is_available=True,
                        status=ListingStatus.CANDIDATE,
                    )
                    session.add(db_source)
                    await session.flush()
                    source_db_id = db_source.id

                enriched = await self.enricher.enrich(
                    title=source.title,
                    price_jpy=source.price,
                    category=product.category,
                    condition=product.condition_normalized,
                )

                listing_price = profit.listing_price

                listing_id = str(uuid.uuid4())[:12]
                sku = f"arb-{source.external_id}-{listing_id}"

                published = await self.ebay_publisher.create_listing(
                    sku=sku,
                    enriched=enriched,
                    price=listing_price,
                    shipping_cost=profit.fees.shipping_cost or settings.default_shipping_cost,
                    quantity=1,
                    image_urls=source.image_urls,
                    condition=self._map_condition(product.condition_normalized),
                    category_id=enriched.suggested_category_id,
                )

                async with self.session_factory() as session:
                    if published.success:
                        db_target = TargetListing(
                            source_listing_id=source_db_id,
                            target_platform="ebay",
                            external_id=published.offer_id,
                            url=published.listing_url,
                            title=enriched.title,
                            description=enriched.description,
                            price=listing_price,
                            shipping_cost=profit.fees.shipping_cost,
                            status=ListingStatus.LISTED,
                            estimated_profit=profit.net_profit,
                        )
                        session.add(db_target)
                        db_src = await session.get(SourceListing, source_db_id)
                        if db_src:
                            db_src.status = ListingStatus.LISTED

                        # Update portfolio tracking
                        self._portfolio[product.category] = (
                            self._portfolio.get(product.category, 0) + 1
                        )

                if published.success:
                    await self.alerter.send_listing_alert(
                        title=enriched.title, price=listing_price, url=published.listing_url
                    )

                results.append(published)
                logger.info(
                    f"[Pipeline] Listed: {enriched.title[:60]} at ${listing_price:.2f} "
                    f"(score: {ranked.composite_score:.0f}, priority: {ranked.priority})"
                )

            except Exception as e:
                logger.error(f"[Pipeline] Failed to list: {e}")
                results.append(PublishedListing(success=False, errors=[str(e)]))

        return results
    async def start_sync(self) -> None:
        """Start the inventory sync loop (runs indefinitely)."""
        logger.info("[Pipeline] Starting inventory sync...")
        await self.inventory_sync.start()

    async def run_sync_once(self) -> None:
        """Run a single sync cycle."""
        await self.inventory_sync.run_once()

    async def close(self) -> None:
        """Clean up all resources."""
        await self.mercari.close()
        await self.ebay_finding.close()
        await self.image_processor.close()
        await self.ebay_publisher.close()
        await self.inventory_sync.close()
        await self.engine.dispose()
        logger.info("[Pipeline] Shut down cleanly.")

    @staticmethod
    def _map_condition(condition: str) -> str:
        """Map source condition to eBay condition enum."""
        mapping = {
            "New": "NEW",
            "Like New": "LIKE_NEW",
            "Good": "USED_GOOD",
            "Fair": "USED_ACCEPTABLE",
            "Poor": "USED_POOR",
        }
        return mapping.get(condition, "USED_GOOD")
