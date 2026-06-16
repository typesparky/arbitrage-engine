"""Inventory sync — polls source listings and delists sold items on target platforms.

This is the critical component that prevents the #1 failure mode:
items selling on the source before we know about it.

Runs on a configurable interval (default 10 minutes).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.config.settings import settings
from src.models.database import (
    Base,
    ListingStatus,
    SourceListing,
    SyncLog,
    TargetListing,
)
from src.scrapers.mercari_jp import MercariJPScraper


class InventorySync:
    """Polls source listings and syncs availability to target platforms."""

    def __init__(
        self,
        scraper: MercariJPScraper | None = None,
        check_interval_minutes: int = 0,
    ):
        self.scraper = scraper or MercariJPScraper()
        self.interval = (check_interval_minutes or settings.scrape_interval_minutes) * 60
        self.engine = create_async_engine(settings.database_url)
        self.session_factory = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self._running = False

    async def start(self) -> None:
        """Start the sync loop. Runs indefinitely."""
        self._running = True
        logger.info(f"[Sync] Starting inventory sync (interval: {self.interval / 60:.0f} min)")

        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.error(f"[Sync] Error in sync loop: {e}")

            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self._running = False

    async def run_once(self) -> SyncLog:
        """Run a single sync cycle. Returns a SyncLog."""
        start_time = time.monotonic()
        log = SyncLog(sync_type="inventory", items_checked=0, items_delisted=0, items_updated=0, errors=0)

        async with self.session_factory() as session:
            # Get all active source listings that have active target listings
            stmt = select(SourceListing).where(
                SourceListing.status.in_([ListingStatus.LISTED, ListingStatus.CANDIDATE])
            )
            result = await session.execute(stmt)
            source_listings = result.scalars().all()

            logger.info(f"[Sync] Checking {len(source_listings)} source listings...")

            # Check each source listing
            tasks = []
            for source in source_listings:
                tasks.append(self._check_and_sync(session, source, log))

            # Run checks concurrently (with semaphore to limit concurrency)
            semaphore = asyncio.Semaphore(5)

            async def bounded_check(task_coro):
                async with semaphore:
                    return await task_coro

            await asyncio.gather(*[bounded_check(t) for t in tasks])

            log.duration_seconds = time.monotonic() - start_time
            session.add(log)
            await session.commit()

        logger.info(
            f"[Sync] Complete: {log.items_checked} checked, "
            f"{log.items_delisted} delisted, {log.items_updated} updated, "
            f"{log.errors} errors in {log.duration_seconds:.1f}s"
        )
        return log

    async def _check_and_sync(
        self,
        session: AsyncSession,
        source: SourceListing,
        log: SyncLog,
    ) -> None:
        """Check a single source listing and sync its target listings."""
        log.items_checked += 1

        try:
            is_available = await self.scraper.check_availability(source.external_id)

            if not is_available and source.is_available:
                # Item sold on source! Delist from all target platforms.
                logger.warning(
                    f"[Sync] Source item {source.external_id} is GONE. Delisting targets..."
                )
                await self._delist_all_targets(session, source, log)
                source.is_available = False
                source.status = ListingStatus.SOURCE_GONE
                source.updated_at = datetime.now(timezone.utc)

            elif is_available and not source.is_available:
                # Item is back (maybe a different listing with same ID)
                logger.info(f"[Sync] Source item {source.external_id} is back!")
                source.is_available = True
                source.status = ListingStatus.LISTED
                source.updated_at = datetime.now(timezone.utc)

            source.last_seen_at = datetime.now(timezone.utc)

        except Exception as e:
            log.errors += 1
            logger.error(f"[Sync] Error checking {source.external_id}: {e}")

    async def _delist_all_targets(
        self,
        session: AsyncSession,
        source: SourceListing,
        log: SyncLog,
    ) -> None:
        """End all target listings for a source item."""
        for target in source.target_listings:
            if target.status == ListingStatus.LISTED:
                # End the listing on the target platform
                # TODO: call the appropriate lister's end_listing method
                logger.info(
                    f"[Sync] Delisting target {target.external_id} "
                    f"({target.target_platform.value})"
                )
                target.status = ListingStatus.SOURCE_GONE
                target.delisted_at = datetime.now(timezone.utc)
                target.failure_reason = "Source item no longer available"
                log.items_delisted += 1

                # TODO: Send alert via Telegram
                # await self._send_alert(
                #     f"⚠️ Auto-delisted: {source.title_original[:50]}..."
                # )

    async def close(self) -> None:
        await self.scraper.close()
        await self.engine.dispose()
