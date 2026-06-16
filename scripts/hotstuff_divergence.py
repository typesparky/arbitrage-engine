#!/usr/bin/env python3
"""Hotstuff divergence scanner: compares against Hyperliquid, Binance, dYdX, Bybit, OKX.

NO TradFi spot references — only perp-to-perp comparisons.
Focuses on: price divergence, funding divergence, orderbook depth, spread.

Usage:
    python scripts/hotstuff_divergence.py
    python scripts/hotstuff_divergence.py --json
    python scripts/hotstuff_divergence.py --top 5
    python scripts/hotstuff_divergence.py --funding-only
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

from src.processors.divergence_engine import DivergenceEngine


# Instrument mapping: Hotstuff symbol -> comparisons against other perp venues
# Only instruments that exist on Hotstuff AND at least one other venue
PAIRS = [
    {
        "symbol": "BTC-PERP",
        "hs_id": "BTC-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "BTC"},
            {"venue": "binance", "symbol": "BTCUSDT"},
        ],
    },
    {
        "symbol": "ETH-PERP",
        "hs_id": "ETH-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "ETH"},
            {"venue": "binance", "symbol": "ETHUSDT"},
        ],
    },
    {
        "symbol": "SOL-PERP",
        "hs_id": "SOL-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "SOL"},
            {"venue": "binance", "symbol": "SOLUSDT"},
        ],
    },
    {
        "symbol": "HYPE-PERP",
        "hs_id": "HYPE-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "HYPE"},
            {"venue": "binance", "symbol": "HYPEUSDT"},
        ],
    },
    {
        "symbol": "XRP-PERP",
        "hs_id": "XRP-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "XRP"},
            {"venue": "binance", "symbol": "XRPUSDT"},
        ],
    },
    {
        "symbol": "BNB-PERP",
        "hs_id": "BNB-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "BNB"},
            {"venue": "binance", "symbol": "BNBUSDT"},
        ],
    },
    {
        "symbol": "ZEC-PERP",
        "hs_id": "ZEC-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "ZEC"},
        ],
    },
    {
        "symbol": "DOGE-PERP",
        "hs_id": "DOGE-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "DOGE"},
            {"venue": "binance", "symbol": "DOGEUSDT"},
        ],
    },
    {
        "symbol": "ADA-PERP",
        "hs_id": "ADA-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "ADA"},
        ],
    },
    {
        "symbol": "LINK-PERP",
        "hs_id": "LINK-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "LINK"},
        ],
    },
    {
        "symbol": "NEAR-PERP",
        "hs_id": "NEAR-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "NEAR"},
        ],
    },
    {
        "symbol": "AVAX-PERP",
        "hs_id": "AVAX-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "AVAX"},
        ],
    },
    {
        "symbol": "ARB-PERP",
        "hs_id": "ARB-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "ARB"},
        ],
    },
    {
        "symbol": "OP-PERP",
        "hs_id": "OP-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "OP"},
        ],
    },
    {
        "symbol": "LTC-PERP",
        "hs_id": "LTC-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "LTC"},
        ],
    },
    {
        "symbol": "ATOM-PERP",
        "hs_id": "ATOM-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "ATOM"},
        ],
    },
    {
        "symbol": "DOT-PERP",
        "hs_id": "DOT-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "DOT"},
        ],
    },
    {
        "symbol": "UNI-PERP",
        "hs_id": "UNI-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "UNI"},
        ],
    },
    {
        "symbol": "AAVE-PERP",
        "hs_id": "AAVE-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "AAVE"},
        ],
    },
    {
        "symbol": "MKR-PERP",
        "hs_id": "MKR-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "MKR"},
        ],
    },
    {
        "symbol": "X-PERP",
        "hs_id": "X-PERP",
        "comparisons": [
            {"venue": "hyperliquid", "symbol": "X"},
        ],
    },
]


async def main(args):
    print("=" * 80)
    print("  HOTSTUFF PERP DEX DIVERGENCE SCANNER")
    print("  Comparing against: Hyperliquid, Binance (perp-to-perp only)")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 80)

    engine = DivergenceEngine()
    try:
        divergences = await engine.scan_all(PAIRS)

        if args.json:
            output = []
            for d in divergences:
                output.append({
                    "symbol": d.symbol,
                    "other_venue": d.other_venue,
                    "hs_mark": d.hotstuff.mark_price,
                    "hs_index": d.hotstuff.index_price,
                    "hs_funding_8h": d.hotstuff.funding_8h,
                    "hs_spread_bps": d.hs_spread_bps,
                    "hs_depth_usd": d.hs_depth_usd,
                    "hs_oi": d.hotstuff.open_interest,
                    "other_mark": d.other.mark_price,
                    "other_index": d.other.index_price,
                    "other_funding_8h": d.other.funding_8h,
                    "other_spread_bps": d.other_spread_bps,
                    "other_depth_usd": d.other_depth_usd,
                    "other_oi": d.other.open_interest,
                    "mark_div_bps": d.mark_div_bps,
                    "index_div_bps": d.index_div_bps,
                    "funding_diff": d.funding_diff,
                    "premium_diff_bps": d.premium_diff_bps,
                    "direction": d.direction,
                    "buy_venue": d.buy_venue,
                    "sell_venue": d.sell_venue,
                    "gross_edge_bps": d.gross_edge_bps,
                    "net_edge_bps": d.net_edge_bps,
                    "is_profitable": d.is_profitable,
                    "score": d.score,
                })
            print(json.dumps(output, indent=2))
        else:
            if args.top:
                divergences = divergences[:args.top]

            if args.funding_only:
                funding_divs = sorted(
                    [d for d in divergences if abs(d.funding_diff) > 0.000005],
                    key=lambda d: abs(d.funding_diff),
                    reverse=True,
                )
                print(f"\n💰 Funding Rate Divergences ({len(funding_divs)} pairs)")
                print("─" * 80)
                for d in funding_divs[:args.top or 10]:
                    print(
                        f"  {d.symbol:12s} vs {d.other_venue:12s}: "
                        f"HS={d.hotstuff.funding_8h:+.6f} other={d.other.funding_8h:+.6f} | "
                        f"diff={d.funding_diff:+.6f} | "
                        f"trade: {d.direction}"
                    )
            else:
                print(engine.format_report(divergences))

        # Summary
        profitable = [d for d in divergences if d.is_profitable]
        print(f"\n{'=' * 80}")
        print(f"SUMMARY: {len(divergences)} pairs scanned | {len(profitable)} profitable")
        if profitable:
            best = max(profitable, key=lambda d: d.net_edge_bps)
            print(
                f"BEST: {best.symbol} vs {best.other_venue} | "
                f"div={best.mark_div_bps:+.1f}bps | "
                f"net={best.net_edge_bps:+.1f}bps | "
                f"score={best.score:.0f}"
            )
        print("=" * 80)

    finally:
        await engine.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hotstuff Perp DEX Divergence Scanner")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--top", type=int, default=0, help="Show top N opportunities")
    parser.add_argument("--funding-only", action="store_true", help="Only show funding divergences")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logger.remove()
    level = "DEBUG" if args.verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<level>{level: <8}</level> | {message}")

    asyncio.run(main(args))
