#!/usr/bin/env python3.12
"""Multi-venue comparison: Hotstuff vs Hyperliquid, dYdX, GMX, Binance, Bybit, OKX.

Compares prices across all venues for overlapping instruments and shows
the best arbitrage opportunities.

Usage:
    python scripts/hotstuff_multi_venue.py
    python scripts/hotstuff_multi_venue.py --historical 7
    python scripts/hotstuff_multi_venue.py --json
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

from src.processors.historical_div import HistoricalDivergenceAnalyzer
from src.processors.multi_venue import CrossVenueAnalyzer, MultiVenueClient, VenuePrice
from src.scrapers.hotstuff import HotstuffClient


# Instruments that exist on Hotstuff AND at least one other venue
CROSS_VENUE_SYMBOLS = [
    "BTC-PERP", "ETH-PERP", "SOL-PERP", "XRP-PERP", "BNB-PERP",
    "GOLD-PERP", "SILVER-PERP", "WTIOIL-PERP", "BRENTOIL-PERP", "NATGAS-PERP",
    "HYPE-PERP",
]


async def run_multi_venue_comparison(args):
    """Run multi-venue price comparison."""
    print("=" * 80)
    print("  MULTI-VENUE PERP COMPARISON: Hotstuff vs Hyperliquid/dYdX/GMX/Binance/Bybit/OKX")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 80)

    # 1. Fetch Hotstuff prices
    hs_client = HotstuffClient()
    await hs_client.initialize()
    hs_tickers = await hs_client.get_tickers("all")
    await hs_client.close()

    # Build Hotstuff price map
    hs_prices: dict[str, VenuePrice] = {}
    for t in hs_tickers:
        if t.symbol in CROSS_VENUE_SYMBOLS:
            hs_prices[t.symbol] = VenuePrice(
                venue="hotstuff",
                symbol=t.symbol,
                mark_price=t.mark_price,
                index_price=t.index_price,
                bid=t.best_bid_price,
                ask=t.best_ask_price,
                funding_8h=t.funding_rate,
                volume_24h=t.volume_24h,
                timestamp=time.time(),
            )

    # 2. Fetch all other venues in parallel
    mv_client = MultiVenueClient()
    venue_prices = await mv_client.fetch_all_venues(CROSS_VENUE_SYMBOLS)
    await mv_client.close()

    # 3. Analyze divergences
    analyzer = CrossVenueAnalyzer(mv_client)
    divergences = analyzer.analyze(hs_prices, venue_prices)

    # 4. Output
    if args.json:
        output = []
        for d in divergences:
            output.append({
                "symbol": d.symbol,
                "category": d.category,
                "hotstuff_mark": d.hotstuff.mark_price,
                "hotstuff_funding_8h": d.hotstuff.funding_8h,
                "other_venue": d.other_venue,
                "other_mark": d.other.mark_price,
                "other_funding_8h": d.other.funding_8h,
                "mark_divergence_bps": d.mark_divergence_bps,
                "index_divergence_bps": d.index_divergence_bps,
                "funding_diff": d.funding_diff,
                "direction": d.direction,
                "best_buy": d.best_venue_to_buy,
                "best_sell": d.best_venue_to_sell,
                "gross_edge_bps": d.gross_edge_bps,
                "net_edge_bps": d.estimated_net_edge_bps,
                "is_profitable": d.is_profitable,
            })
        print(json.dumps(output, indent=2))
    else:
        print(analyzer.format_report(divergences))

    return divergences


async def run_historical_analysis(args):
    """Run historical divergence analysis."""
    print("\n" + "=" * 80)
    print(f"  HISTORICAL DIVERGENCE ANALYSIS — {args.historical} days")
    print("=" * 80)

    hist = HistoricalDivergenceAnalyzer()

    # Define which instruments to analyze and their chart endpoints
    instruments = [
        {
            "symbol": "BTC-PERP",
            "hs_id": "1",  # Hotstuff instrument ID
            "venues": {
                "binance": "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&startTime={start_ms}&endTime={end_ms}",
                "bybit": "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&start={start_ms}&end={end_ms}",
            },
        },
        {
            "symbol": "ETH-PERP",
            "hs_id": "2",
            "venues": {
                "binance": "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=1h&startTime={start_ms}&endTime={end_ms}",
                "bybit": "https://api.bybit.com/v5/market/kline?category=linear&symbol=ETHUSDT&interval=60&start={start_ms}&end={end_ms}",
            },
        },
        {
            "symbol": "GOLD-PERP",
            "hs_id": "4",
            "venues": {
                # No direct gold perp on Binance/Bybit, skip
            },
        },
    ]


    for inst in instruments:
        sym = inst["symbol"]
        print(f"\n--- {sym} ---")

        # Build venue URLs with time range
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - args.historical * 86400 * 1000
        venues = {}
        for vname, url_tmpl in inst["venues"].items():
            venues[vname] = url_tmpl.format(start_ms=start_ms, end_ms=end_ms)

        if not venues:
            print(f"  No venue chart endpoints for {sym}")
            continue

        analysis = await hist.analyze(
            symbol=sym,
            hotstuff_id=inst["hs_id"],
            venues=venues,
            days=args.historical,
        )

        if analysis:
            print(f"  Recommendation: {analysis.recommendation}")
            for venue, stats in analysis.divergences.items():
                print(f"  {stats.summary()}")
            if analysis.funding_stats:
                fs = analysis.funding_stats
                print(f"  Funding: vol={fs.get('hourly_volatility', 0):.6f} "
                      f"avg_return={fs.get('avg_hourly_return', 0):.6f}")
        else:
            print(f"  No data available")

    await hist.close()


async def main(args):
    divergences = await run_multi_venue_comparison(args)

    if args.historical > 0:
        await run_historical_analysis(args)

    # Summary
    profitable = [d for d in divergences if d.is_profitable]
    print(f"\n{'=' * 80}")
    print(f"SUMMARY: {len(divergences)} cross-venue pairs | {len(profitable)} profitable")
    if profitable:
        best = max(profitable, key=lambda d: d.estimated_net_edge_bps)
        print(f"BEST: {best.symbol} — {best.other_venue} vs Hotstuff = {best.estimated_net_edge_bps:+.1f}bps net")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Venue Perp Comparison")
    parser.add_argument("--historical", type=int, default=0,
                        help="Also run historical divergence analysis (N days)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logger.remove()
    level = "DEBUG" if args.verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<level>{level: <8}</level> | {message}")

    asyncio.run(main(args))
