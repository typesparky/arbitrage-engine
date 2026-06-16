"""Core divergence engine: compares Hotstuff perps against other perp venues.

Design principles:
  - Only compare against OTHER PERP VEXs (Hyperliquid, dYdX, Binance, Bybit, OKX)
  - NO TradFi spot references (stale, delayed, wrong market)
  - RWA stocks on Hotstuff ARE the price discovery — they trade 24/7 while
    the underlying equity doesn't. The perp IS the reference.
  - Focus on: funding rate divergences, premium divergences, basis trades

Key metrics:
  - Price divergence (mark vs mark) in bps
  - Funding rate divergence (8h rate diff)
  - Premium divergence (mark vs index/oracle)
  - Orderbook depth comparison (bid/ask sizes)
  - Spread comparison
  - Open interest ratio (which venue has more conviction)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
from loguru import logger


@dataclass
class OrderbookLevel:
    """Single orderbook level."""
    price: float
    size: float
    orders: int = 0


@dataclass
class Orderbook:
    """Full orderbook for a venue."""
    venue: str
    symbol: str
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]
    timestamp: float

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0

    @property
    def mid(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2
        return 0

    @property
    def spread_bps(self) -> float:
        if self.mid > 0:
            return (self.best_ask - self.best_bid) / self.mid * 10_000
        return 0

    @property
    def bid_depth_usd(self) -> float:
        return sum(l.price * l.size for l in self.bids)

    @property
    def ask_depth_usd(self) -> float:
        return sum(l.price * l.size for l in self.asks)

    def depth_at_bps(self, bps: float) -> tuple[float, float]:
        """Get total depth within X bps of mid."""
        if self.mid <= 0:
            return (0, 0)
        threshold = self.mid * bps / 10_000
        bid_depth = sum(l.price * l.size for l in self.bids if l.price >= self.mid - threshold)
        ask_depth = sum(l.price * l.size for l in self.asks if l.price <= self.mid + threshold)
        return (bid_depth, ask_depth)


@dataclass
class VenueSnapshot:
    """Complete market snapshot from one venue."""
    venue: str
    symbol: str
    mark_price: float
    index_price: float
    funding_8h: float
    open_interest: float
    volume_24h: float
    orderbook: Orderbook | None
    timestamp: float

    @property
    def premium_bps(self) -> float:
        if self.index_price > 0:
            return (self.mark_price - self.index_price) / self.index_price * 10_000
        return 0

    @property
    def funding_annualized_pct(self) -> float:
        return self.funding_8h * 3 * 365 * 100


@dataclass
class DivergencePair:
    """Divergence between Hotstuff and one other venue."""
    symbol: str
    hotstuff: VenueSnapshot
    other: VenueSnapshot
    other_venue: str

    # Price divergence
    mark_div_bps: float       # (HS_mark - other_mark) / other_mark * 10000
    index_div_bps: float      # (HS_index - other_index) / other_index * 10000

    # Funding divergence
    funding_diff: float       # HS_funding - other_funding
    funding_diff_bps: float   # In basis points of 8h rate

    # Premium divergence
    premium_diff_bps: float   # HS_premium - other_premium

    # Spread comparison
    hs_spread_bps: float
    other_spread_bps: float
    spread_advantage: float   # Positive = Hotstuff has tighter spread

    # Depth comparison
    hs_depth_usd: float
    other_depth_usd: float
    depth_ratio: float        # HS depth / other depth

    # Trade direction
    direction: str            # "short_hotstuff" | "long_hotstuff"
    buy_venue: str
    sell_venue: str

    # Edge
    gross_edge_bps: float
    net_edge_bps: float       # After estimated fees
    is_profitable: bool

    # Score
    score: float = 0.0

    timestamp: float = field(default_factory=time.time)

    def summary(self) -> str:
        profit_emoji = "✅" if self.is_profitable else "❌"
        depth_str = f"depth HS={self.hs_depth_usd:,.0f} {self.other_venue}={self.other_depth_usd:,.0f}"
        return (
            f"{profit_emoji} {self.symbol}: HS={self.hotstuff.mark_price:.2f} vs "
            f"{self.other_venue}={self.other.mark_price:.2f} | "
            f"div={self.mark_div_bps:+.1f}bps | "
            f"fund_diff={self.funding_diff:+.6f} | "
            f"spread HS={self.hs_spread_bps:.3f}% {self.other_venue}={self.other_spread_bps:.3f}% | "
            f"net={self.net_edge_bps:+.1f}bps | "
            f"score={self.score:.0f}"
        )


class DivergenceEngine:
    """Fetches and compares orderbooks + prices across perp venues."""

    # Estimated fees (taker, maker) in bps
    FEES = {
        "hotstuff":    (0.025, -0.002),
        "hyperliquid": (0.035, -0.003),
        "dydx":        (0.050, -0.020),
        "binance":     (0.040, -0.020),
        "bybit":       (0.055, -0.010),
        "okx":         (0.050, -0.020),
    }

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

    # ── Fetch orderbooks ──────────────────────────────────────────────

    async def get_hotstuff_orderbook(self, symbol: str) -> Orderbook | None:
        """Fetch Hotstuff orderbook."""
        client = await self._c()
        try:
            r = await client.post("https://api.hotstuff.trade/info", json={
                "method": "orderbook",
                "params": {"symbol": symbol},
            })
            if r.status_code != 200:
                return None
            data = r.json()

            # Parse orderbook format
            bids = []
            asks = []
            if isinstance(data, dict):
                raw_bids = data.get("bids", [])
                raw_asks = data.get("asks", [])
                for level in raw_bids:
                    if isinstance(level, (list, tuple)) and len(level) >= 2:
                        bids.append(OrderbookLevel(price=float(level[0]), size=float(level[1])))
                    elif isinstance(level, dict):
                        bids.append(OrderbookLevel(
                            price=float(level.get("price", level.get("px", 0))),
                            size=float(level.get("size", level.get("sz", 0))),
                            orders=int(level.get("n", level.get("orders", 0))),
                        ))
                for level in raw_asks:
                    if isinstance(level, (list, tuple)) and len(level) >= 2:
                        asks.append(OrderbookLevel(price=float(level[0]), size=float(level[1])))
                    elif isinstance(level, dict):
                        asks.append(OrderbookLevel(
                            price=float(level.get("price", level.get("px", 0))),
                            size=float(level.get("size", level.get("sz", 0))),
                            orders=int(level.get("n", level.get("orders", 0))),
                        ))

            return Orderbook(
                venue="hotstuff",
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.warning(f"[DivergenceEngine] Hotstuff OB error for {symbol}: {e}")
            return None

    async def get_hyperliquid_orderbook(self, coin: str) -> Orderbook | None:
        """Fetch Hyperliquid L2 orderbook (20 levels)."""
        client = await self._c()
        try:
            r = await client.post("https://api.hyperliquid.xyz/info", json={
                "type": "l2Book",
                "coin": coin,
            })
            if r.status_code != 200:
                return None
            data = r.json()

            bids = []
            asks = []
            if isinstance(data, dict):
                levels = data.get("levels", [[], []])
                for level in levels[0]:  # bids
                    bids.append(OrderbookLevel(
                        price=float(level["px"]),
                        size=float(level["sz"]),
                        orders=int(level.get("n", 0)),
                    ))
                for level in levels[1]:  # asks
                    asks.append(OrderbookLevel(
                        price=float(level["px"]),
                        size=float(level["sz"]),
                        orders=int(level.get("n", 0)),
                    ))

            return Orderbook(
                venue="hyperliquid",
                symbol=coin,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.warning(f"[DivergenceEngine] HL OB error for {coin}: {e}")
            return None

    async def get_binance_orderbook(self, symbol: str, limit: int = 20) -> Orderbook | None:
        """Fetch Binance perp orderbook."""
        client = await self._c()
        try:
            r = await client.get(
                f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit={limit}"
            )
            if r.status_code != 200:
                return None
            data = r.json()

            bids = [OrderbookLevel(price=float(p), size=float(s)) for p, s in data.get("bids", [])]
            asks = [OrderbookLevel(price=float(p), size=float(s)) for p, s in data.get("asks", [])]

            return Orderbook(
                venue="binance",
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.warning(f"[DivergenceEngine] Binance OB error for {symbol}: {e}")
            return None

    # ── Fetch full snapshots ──────────────────────────────────────────

    async def get_hotstuff_snapshot(self, symbol: str, instrument_id: int = 0) -> VenueSnapshot | None:
        """Fetch Hotstuff full snapshot (ticker + orderbook)."""
        client = await self._c()
        try:
            # Get ticker
            r = await client.post("https://api.hotstuff.trade/info", json={
                "method": "ticker",
                "params": {"symbol": symbol},
            })
            if r.status_code != 200:
                return None
            tickers = r.json()
            if not tickers or not isinstance(tickers, list):
                return None
            t = tickers[0]

            # Get orderbook
            ob = await self.get_hotstuff_orderbook(symbol)

            return VenueSnapshot(
                venue="hotstuff",
                symbol=symbol,
                mark_price=float(t.get("mark_price", 0)),
                index_price=float(t.get("index_price", 0)),
                funding_8h=float(t.get("funding_rate", 0)),
                open_interest=float(t.get("open_interest", 0)),
                volume_24h=float(t.get("volume_24h", 0)),
                orderbook=ob,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.warning(f"[DivergenceEngine] HS snapshot error for {symbol}: {e}")
            return None

    async def get_hyperliquid_snapshot(self, coin: str) -> VenueSnapshot | None:
        """Fetch Hyperliquid full snapshot."""
        client = await self._c()
        try:
            # Get meta + context
            r = await client.post("https://api.hyperliquid.xyz/info", json={
                "type": "metaAndAssetCtxs",
            })
            if r.status_code != 200:
                return None
            meta, ctxs = r.json()
            universe = meta.get("universe", [])

            # Find the coin
            idx = None
            for i, u in enumerate(universe):
                if u["name"] == coin:
                    idx = i
                    break
            if idx is None or idx >= len(ctxs):
                return None

            ctx = ctxs[idx]

            # Get orderbook
            ob = await self.get_hyperliquid_orderbook(coin)

            return VenueSnapshot(
                venue="hyperliquid",
                symbol=coin,
                mark_price=float(ctx.get("markPx", 0)),
                index_price=float(ctx.get("oraclePx", 0)),
                funding_8h=float(ctx.get("funding", 0)),
                open_interest=float(ctx.get("openInterest", 0)),
                volume_24h=float(ctx.get("dayNtlVlm", 0)),
                orderbook=ob,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.warning(f"[DivergenceEngine] HL snapshot error for {coin}: {e}")
            return None

    async def get_binance_snapshot(self, symbol: str) -> VenueSnapshot | None:
        """Fetch Binance perp full snapshot."""
        client = await self._c()
        try:
            # Get ticker
            r = await client.get(
                f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
            )
            if r.status_code != 200:
                return None
            t = r.json()

            # Get funding
            r2 = await client.get(
                f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
            )
            fund_8h = 0
            if r2.status_code == 200 and r2.json():
                fund_8h = float(r2.json()[0].get("fundingRate", 0))

            # Get orderbook
            ob = await self.get_binance_orderbook(symbol)

            last = float(t.get("lastPrice", 0))
            return VenueSnapshot(
                venue="binance",
                symbol=symbol,
                mark_price=last,
                index_price=last,  # Approximate
                funding_8h=fund_8h,
                open_interest=0,  # Would need separate endpoint
                volume_24h=float(t.get("quoteVolume", 0)),
                orderbook=ob,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.warning(f"[DivergenceEngine] Binance snapshot error for {symbol}: {e}")
            return None

    # ── Compare divergences ──────────────────────────────────────────

    async def compare_pair(
        self,
        symbol: str,
        hs_id: str,
        other_venue: str,
        other_symbol: str,
    ) -> DivergencePair | None:
        """Compare Hotstuff vs one other venue for a single instrument."""
        # Fetch both snapshots in parallel
        hs_task = self.get_hotstuff_snapshot(hs_id, 0)

        if other_venue == "hyperliquid":
            other_task = self.get_hyperliquid_snapshot(other_symbol)
        elif other_venue == "binance":
            other_task = self.get_binance_snapshot(other_symbol)
        else:
            return None

        hs, other = await asyncio.gather(hs_task, other_task, return_exceptions=True)

        if isinstance(hs, Exception) or hs is None:
            return None
        if isinstance(other, Exception) or other is None:
            return None
        if hs.mark_price <= 0 or other.mark_price <= 0:
            return None

        # Compute divergences
        mark_div = (hs.mark_price - other.mark_price) / other.mark_price * 10_000

        idx_div = 0
        if hs.index_price > 0 and other.index_price > 0:
            idx_div = (hs.index_price - other.index_price) / other.index_price * 10_000

        fund_diff = hs.funding_8h - other.funding_8h

        fund_diff_bps = 0
        if abs(other.funding_8h) > 1e-10:
            fund_diff_bps = fund_diff / abs(other.funding_8h) * 10_000

        prem_diff = hs.premium_bps - other.premium_bps

        # Spread comparison
        hs_spread = hs.orderbook.spread_bps if hs.orderbook else 0
        other_spread = other.orderbook.spread_bps if other.orderbook else 0
        spread_adv = other_spread - hs_spread  # Positive = HS tighter

        # Depth comparison
        hs_depth = 0
        if hs.orderbook:
            hs_depth = min(hs.orderbook.bid_depth_usd, hs.orderbook.ask_depth_usd)
        other_depth = 0
        if other.orderbook:
            other_depth = min(other.orderbook.bid_depth_usd, other.orderbook.ask_depth_usd)
        depth_ratio = hs_depth / other_depth if other_depth > 0 else 0

        # Direction
        if mark_div > 0:
            direction = "short_hotstuff"
            buy_venue = other_venue
            sell_venue = "hotstuff"
        else:
            direction = "long_hotstuff"
            buy_venue = "hotstuff"
            sell_venue = other_venue

        # Fee-adjusted edge
        hs_taker, hs_maker = self.FEES.get("hotstuff", (0.025, -0.002))
        other_taker, other_maker = self.FEES.get(other_venue, (0.04, -0.02))

        # Realistic: maker on one, taker on other
        realistic_fees = min(hs_maker, hs_taker) + min(other_maker, other_taker)
        gross = abs(mark_div)
        net = gross - abs(realistic_fees)

        # Score
        score = self._score(
            net_edge=net,
            gross_edge=gross,
            fund_diff=fund_diff,
            direction=direction,
            hs_spread=hs_spread,
            other_spread=other_spread,
            hs_depth=hs_depth,
            other_depth=other_depth,
            hs_oi=hs.open_interest,
            other_oi=other.open_interest,
        )

        return DivergencePair(
            symbol=symbol,
            hotstuff=hs,
            other=other,
            other_venue=other_venue,
            mark_div_bps=mark_div,
            index_div_bps=idx_div,
            funding_diff=fund_diff,
            funding_diff_bps=fund_diff_bps,
            premium_diff_bps=prem_diff,
            hs_spread_bps=hs_spread,
            other_spread_bps=other_spread,
            spread_advantage=spread_adv,
            hs_depth_usd=hs_depth,
            other_depth_usd=other_depth,
            depth_ratio=depth_ratio,
            direction=direction,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            gross_edge_bps=gross,
            net_edge_bps=net,
            is_profitable=net > 0,
            score=score,
        )

    def _score(
        self,
        net_edge: float,
        gross_edge: float,
        fund_diff: float,
        direction: str,
        hs_spread: float,
        other_spread: float,
        hs_depth: float,
        other_depth: float,
        hs_oi: float,
        other_oi: float,
    ) -> float:
        """Score 0-100."""
        score = 0.0

        # Net edge (up to 35 pts)
        score += min(35, max(0, net_edge * 3.5))

        # Funding alignment (up to 20 pts)
        if direction == "short_hotstuff" and fund_diff > 0:
            score += min(20, fund_diff * 10000)
        elif direction == "long_hotstuff" and fund_diff < 0:
            score += min(20, abs(fund_diff) * 10000)

        # Spread advantage (up to 15 pts)
        spread_adv = other_spread - hs_spread
        if spread_adv > 0:
            score += min(15, spread_adv * 100)

        # Depth (up to 15 pts)
        depth = min(hs_depth, other_depth)
        if depth > 10000:
            score += 15
        elif depth > 1000:
            score += 10
        elif depth > 100:
            score += 5

        # OI confidence (up to 15 pts)
        total_oi = hs_oi + other_oi
        if total_oi > 1000:
            score += 15
        elif total_oi > 100:
            score += 10
        elif total_oi > 10:
            score += 5

        return min(100, score)

    # ── Full scan ─────────────────────────────────────────────────────

    async def scan_all(
        self,
        pairs: list[dict],
    ) -> list[DivergencePair]:
        """Scan all instrument pairs across venues.

        Args:
            pairs: List of {
                "symbol": "BTC-PERP",
                "hs_id": "BTC-PERP",
                "comparisons": [
                    {"venue": "hyperliquid", "symbol": "BTC"},
                    {"venue": "binance", "symbol": "BTCUSDT"},
                ]
            }
        """
        tasks = []
        for pair in pairs:
            hs_id = pair.get("hs_id", pair["symbol"])
            for comp in pair.get("comparisons", []):
                tasks.append(self.compare_pair(
                    symbol=pair["symbol"],
                    hs_id=hs_id,
                    other_venue=comp["venue"],
                    other_symbol=comp["symbol"],
                ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        divergences = []
        for r in results:
            if isinstance(r, DivergencePair):
                divergences.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"[DivergenceEngine] Task error: {r}")

        divergences.sort(key=lambda d: d.score, reverse=True)
        return divergences

    def format_heatmap(self, divergences: list[DivergencePair]) -> str:
        """Format a divergence heatmap: symbols x venues."""
        if not divergences:
            return "No divergence data."

        # Build matrix: symbol -> venue -> divergence
        matrix: dict[str, dict[str, float]] = {}
        venues_set = set()
        for d in divergences:
            sym = d.symbol
            ven = d.other_venue
            venues_set.add(ven)
            matrix.setdefault(sym, {})[ven] = d.mark_div_bps

        venues = sorted(venues_set)
        symbols = sorted(matrix.keys())

        # Header
        lines = ["📊 Divergence Heatmap (bps)"]
        header = f"{'Symbol':16s}" + "".join(f"{v:>14s}" for v in venues) + f"{'Best':>14s}"
        lines.append(header)
        lines.append("─" * len(header))

        for sym in symbols:
            row = f"{sym:16s}"
            best_venue = ""
            best_div = 0
            for ven in venues:
                div = matrix[sym].get(ven, None)
                if div is not None:
                    row += f"{div:>+14.1f}"
                    if abs(div) > abs(best_div):
                        best_div = div
                        best_venue = ven
                else:
                    row += f"{'N/A':>14s}"
            row += f"{best_venue:>14s}"
            lines.append(row)

        return "\n".join(lines)

    def format_report(self, divergences: list[DivergencePair]) -> str:
        """Format full divergence report."""
        if not divergences:
            return "No divergences found."

        profitable = [d for d in divergences if d.is_profitable]

        lines = [
            f"🔥 Perp DEX Divergence Report — {len(divergences)} pairs, {len(profitable)} profitable",
            "─" * 80,
        ]

        # Heatmap
        lines.append("")
        lines.append(self.format_heatmap(divergences))
        lines.append("")

        # Top opportunities
        lines.append("🏆 Top Opportunities:")
        lines.append("─" * 80)
        for i, d in enumerate(divergences[:10], 1):
            lines.append(f"{i:2d}. {d.summary()}")

        # Funding divergences
        funding_opps = sorted(
            [d for d in divergences if abs(d.funding_diff) > 0.00001],
            key=lambda d: abs(d.funding_diff),
            reverse=True,
        )
        if funding_opps:
            lines.append("")
            lines.append("💰 Funding Rate Divergences:")
            lines.append("─" * 80)
            for d in funding_opps[:5]:
                lines.append(
                    f"  {d.symbol}: HS={d.hotstuff.funding_8h:+.6f} vs "
                    f"{d.other_venue}={d.other.funding_8h:+.6f} | "
                    f"diff={d.funding_diff:+.6f} | "
                    f"direction: {d.direction}"
                )

        return "\n".join(lines)
