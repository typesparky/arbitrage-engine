#!/usr/bin/env python3.12
"""Hotstuff perpetual DEX arbitrage scanner — with Telegram alerts.

Scans Hotstuff perps for arbitrage opportunities against external venues:
  - RWA stocks (AAPL, MSFT, NVDA, PLTR, etc.) vs Yahoo Finance spot
  - Commodities (GOLD, SILVER, OIL, NATGAS) vs Yahoo Finance futures
  - Crypto (BTC, ETH, SOL, etc.) vs Binance perps
  - Indices (USA500, USA100) vs Yahoo Finance

Also scans for funding rate opportunities (high funding -> collect by shorting).

Usage:
    python scripts/hotstuff_scan.py
    python scripts/hotstuff_scan.py --categories rwa_stock commodity
    python scripts/hotstuff_scan.py --min-divergence 10 --min-score 40
    python scripts/hotstuff_scan.py --funding-only
    python scripts/hotstuff_scan.py --dry-run
    python scripts/hotstuff_scan.py --json
    python scripts/hotstuff_scan.py --interval 300  # continuous every 5min
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.processors.funding_analyzer import FundingAnalyzer
from src.processors.hotstuff_arb import HotstuffArbScanner
from src.scrapers.hotstuff import HotstuffClient
from src.utils.alerts import TelegramAlerter


async def run_arb_scan(args):
    """Run cross-venue arbitrage scan with optional Telegram alerts."""
    scanner = HotstuffArbScanner(
        volume_14d=args.volume,
        maker_share_pct=args.maker_share / 100 if args.maker_share else 0,
        min_divergence_bps=args.min_divergence,
        min_score=args.min_score,
        categories=args.categories,
    )

    try:
        await scanner.initialize()
        opportunities = await scanner.scan()

        if args.json:
            output = []
            for opp in opportunities:
                output.append({
                    "symbol": opp.symbol,
                    "category": opp.category,
                    "hotstuff_mark": opp.hotstuff_mark,
                    "hotstuff_bid": opp.hotstuff_bid,
                    "hotstuff_ask": opp.hotstuff_ask,
                    "reference_price": opp.reference_price,
                    "reference_venue": opp.reference_venue,
                    "divergence_bps": opp.divergence_bps,
                    "direction": opp.direction,
                    "net_edge_bps": opp.fee_analysis.net_edge_bps if opp.fee_analysis else 0,
                    "is_profitable": opp.fee_analysis.is_profitable if opp.fee_analysis else False,
                    "funding_8h": opp.funding_8h,
                    "spread_bps": opp.spread_bps,
                    "score": opp.score,
                    "profit_per_1k": opp.net_profit_per_1k,
                })
            print(json.dumps(output, indent=2))
        else:
            print(scanner.format_opportunities(opportunities))

        # Send Telegram alerts for top profitable opportunities
        profitable = [o for o in opportunities if o.fee_analysis and o.fee_analysis.is_profitable]
        if profitable and not args.dry_run:
            alerter = TelegramAlerter()
            if alerter._enabled:
                await alerter.send_arb_summary(profitable[:5])
                for opp in profitable[:3]:
                    if opp.score >= args.alert_threshold:
                        await alerter.send_arb_opportunity(opp)
                logger.info(f"[Telegram] Sent alerts for {min(3, len(profitable))} opportunities")
            else:
                logger.info("[Telegram] Not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
        elif args.dry_run:
            logger.info(f"[DryRun] Would send {len(profitable)} alerts")

        return opportunities
    finally:
        await scanner.close()


async def run_funding_scan(args):
    """Run funding rate opportunity scan."""
    client = HotstuffClient()
    try:
        await client.initialize()
        tickers = await client.get_tickers("all")

        analyzer = FundingAnalyzer(client)
        analyses = await analyzer.analyze_all(tickers)

        # Filter
        analyses = [a for a in analyses if a.score >= args.min_score]

        if args.json:
            output = []
            for a in analyses[:20]:
                output.append({
                    "symbol": a.symbol,
                    "funding_8h": a.funding_8h,
                    "funding_hourly": a.funding_hourly,
                    "funding_annualized_pct": a.funding_annualized_pct,
                    "premium_pct": a.premium_pct,
                    "hourly_capture_per_10k": a.hourly_capture_per_10k,
                    "daily_capture_per_10k": a.daily_capture_per_10k,
                    "is_extreme": a.is_extreme,
                    "direction": a.direction,
                    "score": a.score,
                })
            print(json.dumps(output, indent=2))
        else:
            print(f"  Funding Rate Scanner — {len(analyses)} opportunities")
            print("-" * 70)
            for i, a in enumerate(analyses[:20], 1):
                print(f"{i:2d}. {a.summary()}")

        return analyses
    finally:
        await client.close()


async def run_full_report(args):
    """Run both scans and produce a combined report."""
    print("=" * 70)
    print(f"  HOTSTUFF PERP DEX ARBITRAGE SCANNER")
    print(f"   {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 70)

    # 1. Cross-venue scan
    print("\n\n  CROSS-VENUE ARBITRAGE")
    print("-" * 70)
    arb_opps = await run_arb_scan(args)

    # 2. Funding scan
    print("\n\n  FUNDING RATE OPPORTUNITIES")
    print("-" * 70)
    fund_opps = await run_funding_scan(args)

    # 3. Summary
    print("\n\n" + "=" * 70)
    profitable = [o for o in (arb_opps or []) if o.fee_analysis and o.fee_analysis.is_profitable]
    extreme_fund = [a for a in (fund_opps or []) if a.is_extreme]
    print(f"SUMMARY: {len(profitable)} profitable arb | {len(extreme_fund)} extreme funding")
    print("=" * 70)


async def run_continuous(args):
    """Run continuous scanning with alerts."""
    logger.info(f"[Continuous] Starting scan loop every {args.interval}s")
    alerter = TelegramAlerter()

    while True:
        try:
            start = time.time()
            await run_full_report(args)
            elapsed = time.time() - start

            sleep_time = max(10, args.interval - elapsed)
            logger.info(f"[Continuous] Sleeping {sleep_time:.0f}s until next scan...")
            await asyncio.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("[Continuous] Stopped by user")
            break
        except Exception as e:
            logger.error(f"[Continuous] Error: {e}")
            if alerter._enabled:
                await alerter.send_error(str(e), "Continuous scan error")
            await asyncio.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Hotstuff Perp DEX Arbitrage Scanner with Telegram")
    parser.add_argument("--categories", nargs="+", default=None,
                        help="Categories to scan: rwa_stock commodity crypto forex index")
    parser.add_argument("--min-divergence", type=float, default=5.0,
                        help="Minimum divergence in bps (default: 5)")
    parser.add_argument("--min-score", type=float, default=20.0,
                        help="Minimum opportunity score 0-100 (default: 20)")
    parser.add_argument("--volume", type=float, default=0,
                        help="Your 14d rolling volume in USD for fee tier")
    parser.add_argument("--maker-share", type=float, default=0,
                        help="Your maker volume share %% for rebate tier")
    parser.add_argument("--funding-only", action="store_true",
                        help="Only scan funding rates")
    parser.add_argument("--arb-only", action="store_true",
                        help="Only scan cross-venue arbitrage")
    parser.add_argument("--dry-run", action="store_true", help="Don't send alerts")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--interval", type=int, default=0,
                        help="Continuous scan interval in seconds (0 = one-shot)")
    parser.add_argument("--alert-threshold", type=float, default=50.0,
                        help="Minimum score to send individual Telegram alert (default: 50)")

    args = parser.parse_args()

    # Logging
    logger.remove()
    level = "DEBUG" if args.verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<level>{level: <8}</level> | {message}")

    if args.interval > 0:
        asyncio.run(run_continuous(args))
    elif args.funding_only:
        asyncio.run(run_funding_scan(args))
    elif args.arb_only:
        asyncio.run(run_arb_scan(args))
    else:
        asyncio.run(run_full_report(args))


if __name__ == "__main__":
    main()
