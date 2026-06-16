"""Multi-venue perpetual DEX/CEX comparison for Hotstuff arbitrage.

Compares Hotstuff perp prices against:
  - Hyperliquid (perp DEX, largest after Hotstuff)
  - dYdX v4 (Cosmos-based perp DEX)
  - GMX v2 (Arbitrum perp DEX)
  - Binance (largest CEX perp exchange)
  - Bybit (2nd largest CEX perp)
  - OKX (3rd largest CEX perp)
  - Yahoo Finance (spot reference for RWA stocks & commodities)

For each instrument, computes:
  - Price divergence (bps) vs each venue
  - Funding rate comparison
  - Best venue to trade against
  - Historical divergence statistics
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
from loguru import logger


@dataclass
class VenuePrice:
    """Price data from a single venue."""
    venue: str
    symbol: str
    mark_price: float
    index_price: float
    bid: float
    ask: float
    funding_8h: float
    volume_24h: float
    timestamp: float
    raw: dict = field(default_factory=dict)

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2
        if mid > 0 and self.ask > 0 and self.bid > 0:
            return (self.ask - self.bid) / mid * 100
        return 0.0

    @property
    def premium_pct(self) -> float:
        if self.index_price > 0:
            return (self.mark_price - self.index_price) / self.index_price * 100
        return 0.0


@dataclass
class CrossVenueDivergence:
    """Divergence between Hotstuff and another venue for the same instrument."""
    symbol: str
    category: str
    hotstuff: VenuePrice
    other: VenuePrice
    other_venue: str

    # Divergence metrics
    mark_divergence_bps: float      # (hs_mark - other_mark) / other_mark * 10000
    index_divergence_bps: float     # (hs_index - other_index) / other_index * 10000
    funding_diff: float             # hs_funding - other_funding

    # Best direction
    direction: str                  # "short_hotstuff" | "long_hotstuff"
    best_venue_to_buy: str          # Where to buy (cheaper)
    best_venue_to_sell: str         # Where to sell (more expensive)

    # Profitability
    gross_edge_bps: float
    estimated_net_edge_bps: float   # After estimated fees on both venues
    is_profitable: bool

    timestamp: float = field(default_factory=time.time)

    def summary(self) -> str:
        profit_emoji = "✅" if self.is_profitable else "❌"
        return (
            f"{profit_emoji} {self.symbol}: HS={self.hotstuff.mark_price:.2f} vs "
            f"{self.other_venue}={self.other.mark_price:.2f} | "
            f"div={self.mark_divergence_bps:+.1f}bps | "
            f"fund_diff={self.funding_diff:+.6f} | "
            f"buy@{self.best_venue_to_buy} sell@{self.best_venue_to_sell} | "
            f"net={self.estimated_net_edge_bps:+.1f}bps"
        )


class MultiVenueClient:
    """Fetches perp prices from multiple venues in parallel."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _c(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Hyperliquid ───────────────────────────────────────────────────

    async def get_hyperliquid(self, symbols: list[str]) -> dict[str, VenuePrice]:
        """Fetch Hyperliquid perp prices.

        HL uses different symbol names (just the coin name, no -PERP suffix).
        """
        client = await self._c()
        try:
            # Get meta + asset contexts
            r = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "metaAndAssetCtxs"},
            )
            r.raise_for_status()
            data = r.json()

            if not isinstance(data, list) or len(data) < 2:
                return {}

            meta = data[0]
            ctxs = data[1]
            universe = meta.get("universe", [])

            results = {}
            for i, u in enumerate(universe):
                name = u["name"]
                if name not in symbols:
                    continue
                if i >= len(ctxs):
                    continue
                ctx = ctxs[i]
                mark = float(ctx.get("markPx", 0))
                idx = float(ctx.get("oraclePx", 0))
                fund = float(ctx.get("funding", 0))
                # HL doesn't provide bid/ask in this endpoint; use mark ± small spread
                results[name] = VenuePrice(
                    venue="hyperliquid",
                    symbol=name,
                    mark_price=mark,
                    index_price=idx,
                    bid=mark * 0.9999,
                    ask=mark * 1.0001,
                    funding_8h=fund,
                    volume_24h=float(ctx.get("dayNtlVlm", 0)),
                    timestamp=time.time(),
                    raw=ctx,
                )
            return results
        except Exception as e:
            logger.warning(f"[MultiVenue] Hyperliquid error: {e}")
            return {}

    # ── dYdX v4 ───────────────────────────────────────────────────────

    async def get_dydx(self, symbols: list[str]) -> dict[str, VenuePrice]:
        """Fetch dYdX v4 perp prices. Symbols like 'BTC-USD'."""
        client = await self._c()
        try:
            r = await client.get("https://indexer.dydx.trade/v4/perpetualMarkets")
            r.raise_for_status()
            data = r.json()

            markets = data.get("markets", {})
            if not isinstance(markets, dict):
                return {}

            results = {}
            for key, m in markets.items():
                ticker = m.get("ticker", "")
                # Map dYdX ticker to our symbol
                mapped = None
                for s in symbols:
                    base = s.replace("-PERP", "")
                    if ticker == f"{base}-USD":
                        mapped = s
                        break
                if not mapped:
                    continue

                oracle = float(m.get("oraclePrice", 0))
                fund = float(m.get("nextFundingRate", 0))
                # dYdX funding is already hourly-like; convert to 8h equivalent
                fund_8h = fund * 8

                results[mapped] = VenuePrice(
                    venue="dydx",
                    symbol=ticker,
                    mark_price=oracle,  # dYdX uses oracle as reference
                    index_price=oracle,
                    bid=oracle * 0.9998,
                    ask=oracle * 1.0002,
                    funding_8h=fund_8h,
                    volume_24h=float(m.get("volume24H", 0)),
                    timestamp=time.time(),
                    raw=m,
                )
            return results
        except Exception as e:
            logger.warning(f"[MultiVenue] dYdX error: {e}")
            return {}

    # ── GMX v2 ────────────────────────────────────────────────────────

    async def get_gmx(self, symbols: list[str]) -> dict[str, VenuePrice]:
        """Fetch GMX v2 perp prices from Arbitrum.

        NOTE: GMX price API returns raw integers with inconsistent decimal
        formats per token. Skipping for now - GMX also lacks traditional
        funding rates so it's less relevant for funding arb.
        """
        return {}

    # ── Binance ───────────────────────────────────────────────────────

    async def get_binance(self, symbols: list[str]) -> dict[str, VenuePrice]:
        """Fetch Binance perp prices. Symbols like 'BTCUSDT'."""
        client = await self._c()
        results = {}

        # Map our symbols to Binance
        binance_map = {
            "BTC-PERP": "BTCUSDT",
            "ETH-PERP": "ETHUSDT",
            "SOL-PERP": "SOLUSDT",
            "XRP-PERP": "XRPUSDT",
            "BNB-PERP": "BNBUSDT",
            "HYPE-PERP": "HYPEUSDT",
        }

        binance_syms = []
        reverse_map = {}
        for s in symbols:
            bs = binance_map.get(s)
            if bs:
                binance_syms.append(bs)
                reverse_map[bs] = s

        if not binance_syms:
            return {}

        try:
            # Batch fetch 24hr ticker (includes last price, volume)
            tasks = [
                client.get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={bs}")
                for bs in binance_syms
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for bs, resp in zip(binance_syms, responses):
                if isinstance(resp, Exception):
                    continue
                if resp.status_code != 200:
                    continue
                d = resp.json()
                last = float(d.get("lastPrice", 0))
                vol = float(d.get("quoteVolume", 0))
                # Get funding rate
                fund_r = await client.get(
                    f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={bs}&limit=1"
                )
                fund_8h = 0
                if fund_r.status_code == 200 and fund_r.json():
                    # Binance funding is per 8h
                    fund_8h = float(fund_r.json()[0].get("fundingRate", 0))

                orig_sym = reverse_map[bs]
                results[orig_sym] = VenuePrice(
                    venue="binance",
                    symbol=bs,
                    mark_price=last,
                    index_price=last,  # Approximate
                    bid=float(d.get("bidPrice", last)),
                    ask=float(d.get("askPrice", last)),
                    funding_8h=fund_8h,
                    volume_24h=vol,
                    timestamp=time.time(),
                    raw=d,
                )
            return results
        except Exception as e:
            logger.warning(f"[MultiVenue] Binance error: {e}")
            return {}

    # ── Bybit ─────────────────────────────────────────────────────────

    async def get_bybit(self, symbols: list[str]) -> dict[str, VenuePrice]:
        """Fetch Bybit perp prices."""
        client = await self._c()
        bybit_map = {
            "BTC-PERP": "BTCUSDT",
            "ETH-PERP": "ETHUSDT",
            "SOL-PERP": "SOLUSDT",
            "XRP-PERP": "XRPUSDT",
            "BNB-PERP": "BNBUSDT",
        }

        bybit_syms = []
        reverse_map = {}
        for s in symbols:
            bs = bybit_map.get(s)
            if bs:
                bybit_syms.append(bs)
                reverse_map[bs] = s

        if not bybit_syms:
            return {}

        try:
            syms_str = ",".join(bybit_syms)
            r = await client.get(
                f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={syms_str}"
            )
            r.raise_for_status()
            d = r.json()
            items = d.get("result", {}).get("list", [])

            results = {}
            for item in items:
                sym = item.get("symbol", "")
                if sym not in reverse_map:
                    continue
                last = float(item.get("lastPrice", 0))
                fund = float(item.get("fundingRate", 0))
                vol = float(item.get("turnover24h", 0))
                orig = reverse_map[sym]
                results[orig] = VenuePrice(
                    venue="bybit",
                    symbol=sym,
                    mark_price=last,
                    index_price=last,
                    bid=float(item.get("bid1Price", last)),
                    ask=float(item.get("ask1Price", last)),
                    funding_8h=fund,
                    volume_24h=vol,
                    timestamp=time.time(),
                    raw=item,
                )
            return results
        except Exception as e:
            logger.warning(f"[MultiVenue] Bybit error: {e}")
            return {}

    # ── OKX ───────────────────────────────────────────────────────────

    async def get_okx(self, symbols: list[str]) -> dict[str, VenuePrice]:
        """Fetch OKX perp prices."""
        client = await self._c()
        okx_map = {
            "BTC-PERP": "BTC-USDT-SWAP",
            "ETH-PERP": "ETH-USDT-SWAP",
            "SOL-PERP": "SOL-USDT-SWAP",
            "XRP-PERP": "XRP-USDT-SWAP",
        }

        okx_syms = []
        reverse_map = {}
        for s in symbols:
            os_ = okx_map.get(s)
            if os_:
                okx_syms.append(os_)
                reverse_map[os_] = s

        results = {}
        for os_ in okx_syms:
            try:
                r = await client.get(
                    f"https://www.okx.com/api/v5/market/ticker?instId={os_}"
                )
                if r.status_code != 200:
                    continue
                d = r.json()
                if not d.get("data"):
                    continue
                t = d["data"][0]
                last = float(t.get("last", 0))
                results[reverse_map[os_]] = VenuePrice(
                    venue="okx",
                    symbol=os_,
                    mark_price=last,
                    index_price=last,
                    bid=float(t.get("bidPx", last)),
                    ask=float(t.get("askPx", last)),
                    funding_8h=0,  # Would need separate funding endpoint
                    volume_24h=float(t.get("vol24h", 0)),
                    timestamp=time.time(),
                    raw=t,
                )
            except Exception as e:
                logger.warning(f"[MultiVenue] OKX error for {os_}: {e}")

        return results

    # ── Fetch all venues in parallel ───────────────────────────────────

    async def fetch_all_venues(
        self, symbols: list[str]
    ) -> dict[str, dict[str, VenuePrice]]:
        """Fetch prices from all venues in parallel.

        Returns:
            {venue_name: {symbol: VenuePrice}}
        """
        results = {}

        # Map HL symbols (just base name)
        hl_syms = [s.replace("-PERP", "") for s in symbols]

        tasks = {
            "hyperliquid": self.get_hyperliquid(hl_syms),
            "dydx": self.get_dydx(symbols),
            "gmx": self.get_gmx(symbols),
            "binance": self.get_binance(symbols),
            "bybit": self.get_bybit(symbols),
            "okx": self.get_okx(symbols),
        }

        venue_results = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )

        for venue_name, result in zip(tasks.keys(), venue_results):
            if isinstance(result, Exception):
                logger.warning(f"[MultiVenue] {venue_name} failed: {result}")
                results[venue_name] = {}
            else:
                results[venue_name] = result
                logger.debug(f"[MultiVenue] {venue_name}: {len(result)} prices")

        return results


class CrossVenueAnalyzer:
    """Analyzes price divergences between Hotstuff and all other venues."""

    # Estimated taker fees for each venue (bps)
    VENUE_TAKER_FEES = {
        "hyperliquid": 0.035,   # 0.035% taker
        "dydx": 0.05,           # 0.05% taker
        "gmx": 0.05,            # ~0.05% swap fee
        "binance": 0.04,        # 0.04% taker (VIP 0)
        "bybit": 0.055,         # 0.055% taker
        "okx": 0.05,            # 0.05% taker
        "hotstuff": 0.025,      # 0.025% taker (Standard)
    }

    VENUE_MAKER_FEES = {
        "hyperliquid": -0.003,  # -0.003% maker rebate
        "dydx": -0.02,          # -0.02% maker rebate
        "gmx": 0.0,             # No maker concept (AMM)
        "binance": -0.02,       # -0.02% maker rebate
        "bybit": -0.01,         # -0.01% maker rebate
        "okx": -0.02,           # -0.02% maker rebate
        "hotstuff": -0.002,     # -0.002% maker rebate (Standard)
    }

    def __init__(self, multi_venue: MultiVenueClient):
        self.mv = multi_venue

    def analyze(
        self,
        hotstuff_prices: dict[str, VenuePrice],
        venue_prices: dict[str, dict[str, VenuePrice]],
    ) -> list[CrossVenueDivergence]:
        """Analyze all cross-venue divergences."""
        divergences = []

        for hs_sym, hs_price in hotstuff_prices.items():
            for venue_name, vprices in venue_prices.items():
                if hs_sym not in vprices:
                    continue
                vp = vprices[hs_sym]

                div = self._compute_divergence(hs_price, vp, venue_name)
                if div:
                    divergences.append(div)

        # Sort by absolute divergence (largest first)
        divergences.sort(key=lambda d: abs(d.mark_divergence_bps), reverse=True)
        return divergences

    def _compute_divergence(
        self,
        hs: VenuePrice,
        other: VenuePrice,
        other_venue: str,
    ) -> CrossVenueDivergence | None:
        """Compute divergence between Hotstuff and another venue."""
        if hs.mark_price <= 0 or other.mark_price <= 0:
            return None

        # Mark price divergence
        mark_div = (hs.mark_price - other.mark_price) / other.mark_price * 10_000

        # Index price divergence
        idx_div = 0
        if hs.index_price > 0 and other.index_price > 0:
            idx_div = (hs.index_price - other.index_price) / other.index_price * 10_000

        # Funding diff
        fund_diff = hs.funding_8h - other.funding_8h

        # Direction
        if mark_div > 0:
            direction = "short_hotstuff"
            buy_venue = other_venue
            sell_venue = "hotstuff"
        else:
            direction = "long_hotstuff"
            buy_venue = "hotstuff"
            sell_venue = other_venue

        # Estimate net edge after fees
        # Assume: maker on one venue, taker on the other
        hs_maker = abs(self.VENUE_MAKER_FEES.get("hotstuff", 0))
        hs_taker = self.VENUE_TAKER_FEES.get("hotstuff", 0)
        other_maker = abs(self.VENUE_MAKER_FEES.get(other_venue, 0))
        other_taker = self.VENUE_TAKER_FEES.get(other_venue, 0)

        # Best case: maker on both
        best_fees = hs_maker + other_maker
        # Worst case: taker on both
        worst_fees = hs_taker + other_taker
        # Realistic: maker on one, taker on other
        realistic_fees = min(hs_maker, hs_taker) + min(other_maker, other_taker)

        gross_edge = abs(mark_div)
        net_edge = gross_edge - realistic_fees

        return CrossVenueDivergence(
            symbol=hs.symbol,
            category=self._categorize(hs.symbol),
            hotstuff=hs,
            other=other,
            other_venue=other_venue,
            mark_divergence_bps=mark_div,
            index_divergence_bps=idx_div,
            funding_diff=fund_diff,
            direction=direction,
            best_venue_to_buy=buy_venue,
            best_venue_to_sell=sell_venue,
            gross_edge_bps=gross_edge,
            estimated_net_edge_bps=net_edge,
            is_profitable=net_edge > 0,
        )

    @staticmethod
    def _categorize(symbol: str) -> str:
        base = symbol.replace("-PERP", "")
        commodities = {"GOLD", "SILVER", "NATGAS", "WTIOIL", "BRENTOIL"}
        crypto = {"BTC", "ETH", "SOL", "XRP", "BNB", "HYPE", "ZEC", "DOGE", "ADA", "AVAX", "ARB", "OP", "MATIC", "LINK", "UNI", "AAVE", "DOT", "ATOM", "NEAR", "FTM", "LTC", "BCH", "FIL", "APT", "SUI", "SEI", "JUP", "WIF", "PEPE", "SHIB", "TON", "TRX", "XLM", "ALGO", "ICP", "HBAR", "VET", "MANA", "SAND", "AXS", "IMX", "RNDR", "INJ", "FET", "AGIX", "OCEAN", "GRT", "LDO", "CRV", "SNX", "MKR", "COMP", "YFI", "BAL", "SUSHI", "1INCH", "RUNE", "CAKE", "GMX", "DYDX", "PERP", "HFT", "WOO", "DYDX"}
        rwa = {"AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "PLTR", "META", "TSLA", "JPM", "GS", "BAC", "V", "MA", "JNJ", "INTC", "ORCL", "CSCO", "ABBV", "CRWD", "HOOD", "MSTR", "COIN", "CRM", "SPY", "QQQ", "EWJ", "EWY", "CVX", "LIN", "MRVL", "APP", "UNH", "AVGO", "NFLX", "AMD", "PFE", "MRK", "KO", "PEP", "WMT", "DIS", "HD", "PG", "XOM", "LLY", "BMY", "GILD", "AMGN", "MDT", "TMO", "ABT", "DHR", "ACN", "QCOM", "TXN", "CAT", "BA", "GE", "HON", "UPS", "DE", "MMM", "AXP", "LOW", "SBUX", "NKE", "MCD", "TGT", "COST", "ZTS", "SPGI", "BLK", "MS", "BK", "C", "USB", "SCHW", "VRTX", "BIIB", "REGN", "ILMN", "IDXX", "WAT", "DXCM", "BIO", "RGEN", "TECH", "PACB", "TWST", "DNA", "HOOD", "CRCL", "MSTR", "COIN", "SPACEX"}
        forex = {"EURUSD", "USDJPY"}
        indices = {"USA500", "USA100"}

        if base in commodities:
            return "commodity"
        elif base in rwa:
            return "rwa_stock"
        elif base in forex:
            return "forex"
        elif base in indices:
            return "index"
        else:
            return "crypto"

    def format_report(self, divergences: list[CrossVenueDivergence]) -> str:
        """Format a full cross-venue comparison report."""
        if not divergences:
            return "No cross-venue divergences found."

        profitable = [d for d in divergences if d.is_profitable]

        lines = [
            f"🔥 Cross-Venue Divergence Report — {len(divergences)} pairs, {len(profitable)} profitable",
            "─" * 70,
        ]

        # Group by symbol
        by_symbol: dict[str, list[CrossVenueDivergence]] = {}
        for d in divergences:
            by_symbol.setdefault(d.symbol, []).append(d)

        for sym, divs in sorted(by_symbol.items()):
            best = max(divs, key=lambda d: abs(d.mark_divergence_bps))
            profit_emoji = "✅" if best.is_profitable else "❌"
            lines.append(
                f"\n{profit_emoji} {sym} ({best.category}): "
                f"HS={best.hotstuff.mark_price:.2f} | "
                f"best venue: {best.other_venue} @ {best.other.mark_price:.2f} | "
                f"div={best.mark_divergence_bps:+.1f}bps | "
                f"net={best.estimated_net_edge_bps:+.1f}bps"
            )
            # Show all venues
            for d in sorted(divs, key=lambda x: abs(x.mark_divergence_bps), reverse=True):
                lines.append(
                    f"   {d.other_venue:15s}: {d.other.mark_price:>12.2f} | "
                    f"div={d.mark_divergence_bps:+8.1f}bps | "
                    f"fund_diff={d.funding_diff:+.6f} | "
                    f"net={d.estimated_net_edge_bps:+6.1f}bps"
                )

        return "\n".join(lines)
