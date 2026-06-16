#!/usr/bin/env python3.12
"""CLI entry point for the arbitrage engine.

Usage:
    python scripts/run.py discover "anime figure" --max-items 20
    python scripts/run.py list --top 5
    python scripts/run.py sync --once
    python scripts/run.py sync --daemon
    python scripts/run.py status
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.config.settings import settings
from src.models.database import init_db
from src.pipeline import ArbitragePipeline


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
        level=settings.log_level,
    )
    logger.add(
        "data/arbitrage.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
    )


async def cmd_discover(args: argparse.Namespace) -> None:
    """Run discovery for a search query."""
    pipeline = ArbitragePipeline()
    await pipeline.initialize()

    try:
        candidates = await pipeline.run_discovery(
            query=args.query,
            category=args.category or "",
            max_items=args.max_items,
        )

        viable = [c for c in candidates if c.is_viable]
        print(f"\n{'='*60}")
        print(f"Discovery Results for: '{args.query}'")
        print(f"{'='*60}")
        print(f"Total candidates: {len(candidates)}")
        print(f"Viable: {len(viable)}")
        print()

        for i, c in enumerate(viable[:20], 1):
            print(f"  {i}. {c.source_item.title[:60]}")
            print(f"     Source: ¥{c.source_item.price:,.0f} (${c.source_price_usd:.0f})")
            print(f"     eBay median: ${c.ebay_analysis.median_price:.2f}")
            print(f"     Est. profit: ${c.estimated_profit:.2f} ({c.estimated_margin_percent:.0f}%)")
            print(f"     Score: {c.overall_score:.0f}/100")
            print(f"     URL: {c.source_item.url}")
            print()

    finally:
        await pipeline.close()


async def cmd_list(args: argparse.Namespace) -> None:
    """List top candidates on eBay."""
    pipeline = ArbitragePipeline()
    await pipeline.initialize()

    try:
        results = await pipeline.list_top_candidates(count=args.top)
        for r in results:
            if r.success:
                print(f"✅ Listed: {r.listing_url}")
            else:
                print(f"❌ Failed: {r.errors}")
    finally:
        await pipeline.close()


async def cmd_sync(args: argparse.Namespace) -> None:
    """Run inventory sync."""
    pipeline = ArbitragePipeline()
    await pipeline.initialize()

    try:
        if args.once:
            await pipeline.run_sync_once()
        else:
            await pipeline.start_sync()
    finally:
        await pipeline.close()


async def cmd_status(args: argparse.Namespace) -> None:
    """Show system status."""
    from sqlalchemy import select, func
    from src.models.database import SourceListing, TargetListing, ListingStatus

    pipeline = ArbitragePipeline()
    await pipeline.initialize()

    try:
        async with pipeline.session_factory() as session:
            # Source stats
            source_total = await session.execute(select(func.count(SourceListing.id)))
            source_active = await session.execute(
                select(func.count(SourceListing.id)).where(
                    SourceListing.status == ListingStatus.LISTED
                )
            )

            # Target stats
            target_total = await session.execute(select(func.count(TargetListing.id)))
            target_active = await session.execute(
                select(func.count(TargetListing.id)).where(
                    TargetListing.status == ListingStatus.LISTED
                )
            )
            target_sold = await session.execute(
                select(func.count(TargetListing.id)).where(
                    TargetListing.status == ListingStatus.SOLD
                )
            )

            print(f"\n{'='*40}")
            print("Arbitrage Engine Status")
            print(f"{'='*40}")
            print(f"Source listings: {source_total.scalar()} total, {source_active.scalar()} active")
            print(f"Target listings: {target_total.scalar()} total, {target_active.scalar()} active, {target_sold.scalar()} sold")
            print()

    finally:
        await pipeline.close()


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="Arbitrage Engine CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Discover
    discover_parser = subparsers.add_parser("discover", help="Search for arbitrage candidates")
    discover_parser.add_argument("query", help="Search query")
    discover_parser.add_argument("--category", "-c", default="", help="Category filter")
    discover_parser.add_argument("--max-items", "-n", type=int, default=50, help="Max items to scrape")

    # List
    list_parser = subparsers.add_parser("list", help="List top candidates on eBay")
    list_parser.add_argument("--top", "-n", type=int, default=5, help="Number of candidates to list")

    # Sync
    sync_parser = subparsers.add_parser("sync", help="Run inventory sync")
    sync_parser.add_argument("--once", action="store_true", help="Run once and exit")
    sync_parser.add_argument("--daemon", action="store_true", help="Run continuously")

    # Status
    subparsers.add_parser("status", help="Show system status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        "discover": cmd_discover,
        "list": cmd_list,
        "sync": cmd_sync,
        "status": cmd_status,
    }

    asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
