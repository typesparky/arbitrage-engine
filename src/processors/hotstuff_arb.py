"""Hotstuff perpetual DEX arbitrage scanner.

Main module that:
1. Fetches Hotstuff perp prices (mark, index, funding, spread)
2. Fetches reference prices from Yahoo Finance, Binance, etc.
3. Computes divergences adjusted for fees and funding
4. Identifies profitable arbitrage opportunities

Arbitrage strategies:
  A. CROSS-VENUE: Hotstuff perp vs. CEX perp/spot
     - Profit from price divergences
     - Must overcome taker fees on both legs + funding
  B. FUNDING CAPTURE: Go short perp when funding is very positive
     - Collect funding payments from longs
     - Hedge with spot/perp on another venue
  C. PREMIUM ARB: Hotstuff perp trades at premium/discount to oracle
     - Buy/sell on Hotstuff, hedge on reference venue
     - Converges at settlement

The maker rebate structure makes Hotstuff particularly interesting:
  - Standard makers GET PAID -0.002%
  - VIP 6 makers GET PAID -0.014%
  - High-volume MMs get additional rebates up to -0.013%
  - This means even 5-10 bps divergences can be profitable
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from loguru import logger

from src.processors.hotstuff_fees import (
    FeeTier,
    TradeFee,
    analyze_arb,
    get_fee_tier,
    trade_fee,
)
from src.processors.reference_prices import (
    BinanceClient,
    PriceDivergence,
    ReferencePrice,
    YahooFinanceClient,
    COMMODITY_SYMBOL_MAP,
    CRYPTO_SYMBOL_MAP,
    INDEX_SYMBOL_MAP,
    RWA_SYMBOL_MAP,
    compute_divergence,
)
from src.scrapers.hotstuff import HotstuffClient, HotstuffTicker


@dataclass
class ArbOpportunity:
    """A concrete arbitrage opportunity."""
    # Instrument
    symbol: str
    category: str  # "rwa_stock" | "commodity" | "crypto" | "forex" | "index"

    # Prices
    hotstuff_mark: float
    hotstuff_bid: float
    hotstuff_ask: float
    hotstuff_index: float
    reference_price: float
    reference_venue: str

    # Divergence
    divergence_bps: float
    divergence_usd: float
    direction: str  # "long_hotstuff" | "short_hotstuff"

    # Fees
    fee_analysis: object  # ArbFeeAnalysis

    # Hotstuff market quality
    spread_bps: float
    funding_8h: float
    funding_hourly: float
    open_interest: float
    volume_24h: float
    hotstuff_depth_usd: float

    # Score (0-100): higher = better opportunity
    score: float = 0.0

    # Metadata
    timestamp: float = field(default_factory=time.time)
    notes: list[str] = field(default_factory=list)

    @property
    def net_profit_per_1k(self) -> float:
        """Estimated net profit per $1000 traded."""
        if self.fee_analysis:
            bps = self.fee_analysis.net_edge_bps
            return 1000 * bps / 10_000
        return 0.0

    def summary(self) -> str:
        dir_emoji = "🔴" if self.direction == "short_hotstuff" else "🟢"
        profit_emoji = "✅" if self.fee_analysis and self.fee_analysis.is_profitable else "❌"
        note_str = f" | {'; '.join(self.notes)}" if self.notes else ""
        return (
            f"{dir_emoji} {self.symbol} ({self.category}) Score={self.score:.0f} {profit_emoji}\n"
            f"   Hotstuff: bid={self.hotstuff_bid:.2f} ask={self.hotstuff_ask:.2f} "
            f"mark={self.hotstuff_mark:.2f} index={self.hotstuff_index:.2f}\n"
            f"   Reference: {self.reference_venue}={self.reference_price:.2f}\n"
            f"   Divergence: {self.divergence_bps:+.1f}bps ({self.divergence_usd:+.2f})\n"
            f"   Funding: {self.funding_8h:+.6f} (8h) | Spread: {self.spread_bps:.4f}%\n"
            f"   Net edge: {self.fee_analysis.net_edge_bps if self.fee_analysis else 0:+.1f}bps | "
            f"Profit/$1k: ${self.net_profit_per_1k:.2f}{note_str}"
        )


class HotstuffArbScanner:
    """Scans Hotstuff perps for cross-venue arbitrage opportunities."""

    def __init__(
        self,
        volume_14d: float = 0,
        maker_share_pct: float = 0.0,
        min_divergence_bps: float = 5.0,
        min_score: float = 30.0,
        categories: list[str] | None = None,
    ):
        """
        Args:
            volume_14d: Your 14-day rolling volume for fee tier
            maker_share_pct: Your maker volume share for rebate tier
            min_divergence_bps: Minimum divergence to consider (in bps)
            min_score: Minimum score to report (0-100)
            categories: Which categories to scan (None = all)
        """
        self.volume_14d = volume_14d
        self.maker_share_pct = maker_share_pct
        self.min_divergence_bps = min_divergence_bps
        self.min_score = min_score
        self.categories = categories  # None = all

        # Clients
        self.hotstuff = HotstuffClient()
        self.yahoo = YahooFinanceClient()
        self.binance = BinanceClient()

        # Fee tier
        self._fee_tier = get_fee_tier(volume_14d, maker_share_pct)

        # State
        self._last_opportunities: list[ArbOpportunity] = []
        self._last_scan_time: float = 0

    async def initialize(self) -> "HotstuffArbScanner":
        """Initialize all clients."""
        await self.hotstuff.initialize()
        logger.info(
            f"[ArbScanner] Initialized. Fee tier: {self._fee_tier.tier_name} "
            f"(taker={self._fee_tier.taker_fee_bps:.4f}%, maker={self._fee_tier.effective_maker_bps:.4f}%)"
        )
        return self

    async def close(self):
        """Clean up all clients."""
        await self.hotstuff.close()
        await self.yahoo.close()
        await self.binance.close()

    async def scan(self) -> list[ArbOpportunity]:
        """Run a full scan: fetch Hotstuff prices + reference prices, find arbs."""
        start = time.time()
        logger.info("[ArbScanner] Starting scan...")

        # 1. Fetch Hotstuff tickers
        tickers = await self.hotstuff.get_tickers("all")
        logger.info(f"[ArbScanner] Fetched {len(tickers)} Hotstuff tickers")

        # 2. Categorize and filter
        rwa_tickers = []
        crypto_tickers = []
        commodity_tickers = []
        index_tickers = []

        for t in tickers:
            cat = t.category
            if self.categories and cat not in self.categories:
                continue
            if not t.is_liquid:
                continue
            if cat == "rwa_stock":
                rwa_tickers.append(t)
            elif cat == "crypto":
                crypto_tickers.append(t)
            elif cat == "commodity":
                commodity_tickers.append(t)
            elif cat == "index":
                index_tickers.append(t)

        # 3. Fetch reference prices in parallel
        opportunities: list[ArbOpportunity] = []

        # RWA stocks → Yahoo Finance
        if rwa_tickers:
            rwa_opps = await self._scan_rwa(rwa_tickers)
            opportunities.extend(rwa_opps)

        # Crypto → Binance
        if crypto_tickers:
            crypto_opps = await self._scan_crypto(crypto_tickers)
            opportunities.extend(crypto_opps)

        # Commodities → Yahoo Finance futures
        if commodity_tickers:
            comm_opps = await self._scan_commodities(commodity_tickers)
            opportunities.extend(comm_opps)

        # Indices → Yahoo Finance
        if index_tickers:
            idx_opps = await self._scan_indices(index_tickers)
            opportunities.extend(idx_opps)

        # 4. Score and sort
        for opp in opportunities:
            opp.score = self._score_opportunity(opp)

        opportunities.sort(key=lambda o: o.score, reverse=True)

        # 5. Filter by min score
        opportunities = [o for o in opportunities if o.score >= self.min_score]

        elapsed = time.time() - start
        self._last_opportunities = opportunities
        self._last_scan_time = time.time()

        logger.info(
            f"[ArbScanner] Scan complete in {elapsed:.1f}s. "
            f"Found {len(opportunities)} opportunities (score >= {self.min_score})"
        )

        return opportunities

    async def _scan_rwa(self, tickers: list[HotstuffTicker]) -> list[ArbOpportunity]:
        """Scan RWA stock perps against Yahoo Finance spot prices."""
        opportunities = []

        # Build symbol list for Yahoo
        yahoo_symbols = []
        ticker_map: dict[str, HotstuffTicker] = {}
        for t in tickers:
            yahoo_sym = RWA_SYMBOL_MAP.get(t.symbol)
            if yahoo_sym:
                yahoo_symbols.append(yahoo_sym)
                ticker_map[yahoo_sym] = t

        if not yahoo_symbols:
            return []

        # Fetch Yahoo prices
        yahoo_prices = await self.yahoo.get_quotes(yahoo_symbols)

        for yahoo_sym, ref in yahoo_prices.items():
            hs_ticker = ticker_map[yahoo_sym]
            opp = self._evaluate_divergence(
                hs_ticker=hs_ticker,
                ref_price=ref,
                ref_venue="yahoo_finance",
                ref_symbol=yahoo_sym,
            )
            if opp:
                # Add RWA-specific notes
                import datetime
                now = datetime.datetime.utcnow()
                # US market hours: 9:30-16:00 ET = 14:30-21:00 UTC
                market_open = now.hour >= 14 and (now.hour < 21 or (now.hour == 14 and now.minute >= 30))
                if not market_open:
                    opp.notes.append("AFTER-HOURS: stock spot may be stale")
                    opp.score *= 0.7  # Discount after-hours opportunities
                opportunities.append(opp)

        return opportunities

    async def _scan_crypto(self, tickers: list[HotstuffTicker]) -> list[ArbOpportunity]:
        """Scan crypto perps against Binance perp prices."""
        opportunities = []

        binance_symbols = []
        ticker_map: dict[str, HotstuffTicker] = {}
        for t in tickers:
            bin_sym = CRYPTO_SYMBOL_MAP.get(t.symbol)
            if bin_sym:
                binance_symbols.append(bin_sym)
                ticker_map[bin_sym] = t

        if not binance_symbols:
            return []

        # Fetch Binance perp prices in parallel
        tasks = [self.binance.get_perp_price(s) for s in binance_symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for bin_sym, result in zip(binance_symbols, results):
            if isinstance(result, Exception) or result is None:
                continue
            hs_ticker = ticker_map[bin_sym]
            opp = self._evaluate_divergence(
                hs_ticker=hs_ticker,
                ref_price=result,
                ref_venue="binance_perp",
                ref_symbol=bin_sym,
            )
            if opp:
                opportunities.append(opp)

        return opportunities

    async def _scan_commodities(self, tickers: list[HotstuffTicker]) -> list[ArbOpportunity]:
        """Scan commodity perps against Yahoo Finance futures prices."""
        opportunities = []

        yahoo_symbols = []
        ticker_map: dict[str, HotstuffTicker] = {}
        for t in tickers:
            yahoo_sym = COMMODITY_SYMBOL_MAP.get(t.symbol)
            if yahoo_sym:
                yahoo_symbols.append(yahoo_sym)
                ticker_map[yahoo_sym] = t

        if not yahoo_symbols:
            return []

        yahoo_prices = await self.yahoo.get_quotes(yahoo_symbols)

        for yahoo_sym, ref in yahoo_prices.items():
            hs_ticker = ticker_map[yahoo_sym]
            opp = self._evaluate_divergence(
                hs_ticker=hs_ticker,
                ref_price=ref,
                ref_venue="yahoo_finance",
                ref_symbol=yahoo_sym,
            )
            if opp:
                opportunities.append(opp)

        return opportunities

    async def _scan_indices(self, tickers: list[HotstuffTicker]) -> list[ArbOpportunity]:
        """Scan index perps against Yahoo Finance."""
        opportunities = []

        yahoo_symbols = []
        ticker_map: dict[str, HotstuffTicker] = {}
        for t in tickers:
            yahoo_sym = INDEX_SYMBOL_MAP.get(t.symbol)
            if yahoo_sym:
                yahoo_symbols.append(yahoo_sym)
                ticker_map[yahoo_sym] = t

        if not yahoo_symbols:
            return []

        yahoo_prices = await self.yahoo.get_quotes(yahoo_symbols)

        for yahoo_sym, ref in yahoo_prices.items():
            hs_ticker = ticker_map[yahoo_sym]
            opp = self._evaluate_divergence(
                hs_ticker=hs_ticker,
                ref_price=ref,
                ref_venue="yahoo_finance",
                ref_symbol=yahoo_sym,
            )
            if opp:
                opportunities.append(opp)

        return opportunities

    def _evaluate_divergence(
        self,
        hs_ticker: HotstuffTicker,
        ref_price: ReferencePrice,
        ref_venue: str,
        ref_symbol: str,
    ) -> ArbFeeAnalysis | None:
        """Evaluate if a divergence is worth trading."""
        # Use mark price for divergence (what you'd actually get)
        div = compute_divergence(
            hotstuff_price=hs_ticker.mark_price,
            reference_price=ref_price.price,
            hotstuff_symbol=hs_ticker.symbol,
            reference_venue=ref_venue,
            reference_symbol=ref_symbol,
            funding_8h=hs_ticker.funding_rate,
        )

        # Skip if divergence too small
        if abs(div.divergence_bps) < self.min_divergence_bps:
            return None

        # Analyze fees
        fee_analysis = analyze_arb(
            spread_bps=abs(div.divergence_bps),
            notional=1000,  # per $1000 for normalization
            funding_8h=hs_ticker.funding_rate,
            volume_14d=self.volume_14d,
            maker_share_pct=self.maker_share_pct,
            leg1_is_maker=True,   # We'll try to be maker on Hotstuff
            leg2_is_maker=False,  # Taker on other venue
            holding_hours=1.0,
        )

        # Build opportunity
        depth = min(hs_ticker.bid_depth_usd, hs_ticker.ask_depth_usd)
        opp = ArbOpportunity(
            symbol=hs_ticker.symbol,
            category=hs_ticker.category,
            hotstuff_mark=hs_ticker.mark_price,
            hotstuff_bid=hs_ticker.best_bid_price,
            hotstuff_ask=hs_ticker.best_ask_price,
            hotstuff_index=hs_ticker.index_price,
            reference_price=ref_price.price,
            reference_venue=ref_venue,
            divergence_bps=div.divergence_bps,
            divergence_usd=div.divergence_usd,
            direction=div.direction,
            fee_analysis=fee_analysis,
            spread_bps=hs_ticker.spread_pct,
            funding_8h=hs_ticker.funding_rate,
            funding_hourly=hs_ticker.hourly_funding,
            open_interest=hs_ticker.open_interest,
            volume_24h=hs_ticker.volume_24h,
            hotstuff_depth_usd=depth,
        )

        return opp

    def _score_opportunity(self, opp: ArbOpportunity) -> float:
        """Score an opportunity from 0-100. Higher = better."""
        score = 0.0

        # 1. Net edge (most important) — up to 40 points
        if opp.fee_analysis:
            net_bps = opp.fee_analysis.net_edge_bps
            score += min(40, max(0, net_bps * 4))  # 10 bps net = 40 pts

        # 2. Divergence size — up to 20 points
        div_bps = abs(opp.divergence_bps)
        score += min(20, div_bps / 2)  # 40 bps div = 20 pts

        # 3. Liquidity — up to 20 points
        if opp.volume_24h > 1_000_000:
            score += 10
        elif opp.volume_24h > 100_000:
            score += 5

        if opp.hotstuff_depth_usd > 1000:
            score += 10
        elif opp.hotstuff_depth_usd > 100:
            score += 5

        # 4. Spread tightness — up to 10 points
        if opp.spread_bps < 0.05:
            score += 10
        elif opp.spread_bps < 0.1:
            score += 5

        # 5. Funding bonus — up to 10 points
        # If funding aligns with our direction, bonus
        if opp.direction == "short_hotstuff" and opp.funding_8h > 0:
            score += min(10, opp.funding_8h * 5000)  # Positive funding → short collects
        elif opp.direction == "long_hotstuff" and opp.funding_8h < 0:
            score += min(10, abs(opp.funding_8h) * 5000)

        return min(100, score)

    def get_top_opportunities(self, n: int = 10) -> list[ArbOpportunity]:
        """Get top N opportunities from last scan."""
        return self._last_opportunities[:n]

    def format_opportunities(self, opportunities: list[ArbOpportunity] | None = None) -> str:
        """Format opportunities for display/alerting."""
        if opportunities is None:
            opportunities = self._last_opportunities

        if not opportunities:
            return "No arbitrage opportunities found."

        lines = [
            f"🔥 Hotstuff Arb Scanner — {len(opportunities)} opportunities",
            f"Fee tier: {self._fee_tier.tier_name} | "
            f"Taker: {self._fee_tier.taker_fee_bps:.4f}% | "
            f"Maker: {self._fee_tier.effective_maker_bps:.4f}%",
            "─" * 60,
        ]

        for i, opp in enumerate(opportunities[:10], 1):
            lines.append(f"\n{i}. {opp.summary()}")

        return "\n".join(lines)
