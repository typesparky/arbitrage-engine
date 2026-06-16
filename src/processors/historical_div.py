from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

import httpx
from loguru import logger


@dataclass
class DivergenceStats:
    """Statistical summary of historical divergence."""
    symbol: str
    venue: str
    start_ts: float
    end_ts: float
    num_observations: int

    # Divergence statistics (in bps)
    mean_div: float
    median_div: float
    std_div: float
    min_div: float
    max_div: float
    current_div: float

    # Percentiles
    p5_div: float
    p25_div: float
    p75_div: float
    p95_div: float

    # Revertal analysis
    pct_time_profitable: float   # % of time divergence was > fee threshold
    avg_revert_hours: float      # Average hours for divergence to halve

    # Risk
    max_drawdown_bps: float      # Largest adverse divergence movement

    def summary(self) -> str:
        return (
            f"{self.symbol} vs {self.venue}: "
            f"mean={self.mean_div:+.1f}bps median={self.median_div:+.1f}bps "
            f"std={self.std_div:.1f}bps range=[{self.min_div:+.1f}, {self.max_div:+.1f}] | "
            f"current={self.current_div:+.1f}bps | "
            f"profitable={self.pct_time_profitable:.0%}"
        )


@dataclass
class HistoricalAnalysis:
    """Full historical analysis for one instrument."""
    symbol: str
    category: str
    divergences: dict[str, DivergenceStats]  # venue -> stats
    funding_stats: dict  # Funding rate statistics
    recommendation: str  # "arb_favorable", "neutral", "unfavorable"


from dataclasses import dataclass


class HistoricalDivergenceAnalyzer:
    """Fetches and analyzes historical price divergences."""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _c(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def analyze(
        self,
        symbol: str,
        hotstuff_id: str | int,
        venues: dict[str, str],
        days: int = 7,
    ) -> HistoricalAnalysis:
        """Analyze historical divergence for one instrument.

        Args:
            symbol: Instrument symbol (e.g. "BTC-PERP")
            hotstuff_id: Hotstuff instrument ID or name for chart API
            venues: {venue_name: chart_url_template}
            days: Number of days of history
        """
        end_ts = int(time.time() * 1000)
        start_ts = end_ts - days * 86400 * 1000
        # Hotstuff chart API uses seconds, not milliseconds
        end_ts_sec = int(time.time())
        start_ts_sec = end_ts_sec - days * 86400

        # Fetch Hotstuff chart
        hs_chart = await self._fetch_hotstuff_chart(hotstuff_id, start_ts_sec, end_ts_sec)
        if not hs_chart:
            logger.warning(f"[Historical] No Hotstuff chart for {symbol}")
            return None

        # Fetch venue charts
        venue_charts = {}
        for venue_name, url_template in venues.items():
            chart = await self._fetch_venue_chart(url_template, start_ts, end_ts)
            if chart:
                venue_charts[venue_name] = chart

        # Compute divergences
        div_stats = {}
        for venue_name, vchart in venue_charts.items():
            stats = self._compute_divergence_stats(
                hs_chart, vchart, symbol, venue_name, start_ts, end_ts
            )
            if stats:
                div_stats[venue_name] = stats

        # Funding stats
        funding_stats = self._analyze_funding(hs_chart)

        # Recommendation
        rec = self._make_recommendation(div_stats)

        category = self._categorize(symbol)

        return HistoricalAnalysis(
            symbol=symbol,
            category=category,
            divergences=div_stats,
            funding_stats=funding_stats,
            recommendation=rec,
        )

    async def _fetch_hotstuff_chart(
        self, symbol: str | int, from_ts: int, to_ts: int, resolution: str = "60"
    ) -> list[dict]:
        """Fetch Hotstuff mark price chart."""
        client = await self._c()
        try:
            r = await client.post(
                "https://api.hotstuff.trade/info",
                json={
                    "method": "chart",
                    "params": {
                        "symbol": str(symbol),
                        "chart_type": "mark",
                        "resolution": resolution,
                        "from": from_ts,
                        "to": to_ts,
                    },
                },
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
            return []
        except Exception as e:
            logger.warning(f"[Historical] Hotstuff chart error: {e}")
            return []

    async def _fetch_venue_chart(
        self, url: str, from_ts: int, to_ts: int
    ) -> list[dict]:
        """Fetch chart data from other venues. Handles multiple formats."""
        client = await self._c()
        try:
            r = await client.get(url)
            if r.status_code != 200:
                return []
            data = r.json()
            if isinstance(data, dict):
                for key in ["data", "result", "candles", "items"]:
                    if key in data and isinstance(data[key], list):
                        data = data[key]
                        break
            if not isinstance(data, list) or len(data) == 0:
                return []
            if isinstance(data[0], list):
                candles = []
                for item in data:
                    if len(item) >= 5:
                        candles.append({
                            "time": item[0],
                            "open": float(item[1]),
                            "high": float(item[2]),
                            "low": float(item[3]),
                            "close": float(item[4]),
                            "volume": float(item[5]) if len(item) > 5 else 0,
                        })
                return candles
            return data if isinstance(data[0], dict) else []
        except Exception as e:
            logger.warning(f"[Historical] Venue chart error: {e}")
            return []

    def _compute_divergence_stats(
        self,
        hs_chart: list[dict],
        v_chart: list[dict],
        symbol: str,
        venue: str,
        start_ts: float,
        end_ts: float,
    ) -> DivergenceStats | None:
        """Compute divergence statistics from two aligned charts."""
        if not hs_chart or not v_chart:
            return None

        # Index venue chart by timestamp
        # Normalize: Hotstuff uses seconds, Binance uses ms
        # Convert everything to seconds for alignment
        v_by_ts = {}
        for c in v_chart:
            ts = c.get("time") or c.get("t") or c.get("timestamp") or c.get("T")
            if ts:
                # If timestamp > 1e12, it is in milliseconds - convert to seconds
                ts_sec = ts / 1000 if ts > 1e12 else ts
                v_by_ts[int(ts_sec)] = c

        # Compute point-by-point divergence
        divergences = []
        for hc in hs_chart:
            ts = hc.get("time")
            if ts is None:
                continue
            ts_int = int(ts / 1000 if ts > 1e12 else ts)
            if ts_int not in v_by_ts:
                continue

            hs_close = float(hc.get("close", 0))
            vc = v_by_ts[ts_int]
            v_close = float(vc.get("close") or vc.get("c") or vc.get("price", 0))

            if hs_close > 0 and v_close > 0:
                div_bps = (hs_close - v_close) / v_close * 10_000
                divergences.append(div_bps)

        if len(divergences) < 5:
            return None

        # Statistics
        divs_sorted = sorted(divergences)
        n = len(divs_sorted)

        mean_div = statistics.mean(divergences)
        median_div = statistics.median(divergences)
        std_div = statistics.stdev(divergences) if n > 1 else 0
        min_div = min(divergences)
        max_div = max(divergences)
        current_div = divergences[-1]

        p5 = divs_sorted[max(0, int(n * 0.05))]
        p25 = divs_sorted[max(0, int(n * 0.25))]
        p75 = divs_sorted[min(n - 1, int(n * 0.75))]
        p95 = divs_sorted[min(n - 1, int(n * 0.95))]

        # % time profitable (divergence > 5 bps — enough to overcome typical fees)
        profitable_count = sum(1 for d in divergences if abs(d) > 5)
        pct_profitable = profitable_count / n

        # Simple revertal: how often does divergence change sign
        sign_changes = 0
        for i in range(1, len(divergences)):
            if (divergences[i] > 0) != (divergences[i - 1] > 0):
                sign_changes += 1

        return DivergenceStats(
            symbol=symbol,
            venue=venue,
            start_ts=start_ts,
            end_ts=end_ts,
            num_observations=n,
            mean_div=mean_div,
            median_div=median_div,
            std_div=std_div,
            min_div=min_div,
            max_div=max_div,
            current_div=current_div,
            p5_div=p5,
            p25_div=p25,
            p75_div=p75,
            p95_div=p95,
            pct_time_profitable=pct_profitable,
            avg_revert_hours=0,  # Would need more sophisticated analysis
            max_drawdown_bps=max(abs(min_div), abs(max_div)),
        )

    def _analyze_funding(self, hs_chart: list[dict]) -> dict:
        """Analyze funding rate patterns from chart data."""
        if not hs_chart:
            return {}

        # Extract mark prices to infer funding from premium changes
        closes = [c.get("close", 0) for c in hs_chart if c.get("close", 0) > 0]
        if len(closes) < 2:
            return {}

        # Simple volatility
        returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
        avg_return = statistics.mean(returns) if returns else 0
        vol = statistics.stdev(returns) if len(returns) > 1 else 0

        return {
            "num_candles": len(hs_chart),
            "avg_hourly_return": avg_return,
            "hourly_volatility": vol,
            "max_hourly_return": max(returns) if returns else 0,
            "min_hourly_return": min(returns) if returns else 0,
        }

    def _make_recommendation(self, div_stats: dict[str, DivergenceStats]) -> str:
        """Make a recommendation based on historical analysis."""
        if not div_stats:
            return "neutral"

        # Check if any venue shows consistent profitable divergence
        for venue, stats in div_stats.items():
            if stats.pct_time_profitable > 0.6 and abs(stats.mean_div) > 10:
                return "arb_favorable"
            if stats.std_div > 50:
                return "unfavorable"  # Too volatile

        return "neutral"

    @staticmethod
    def _categorize(symbol: str) -> str:
        base = symbol.replace("-PERP", "")
        commodities = {"GOLD", "SILVER", "NATGAS", "WTIOIL", "BRENTOIL"}
        rwa = {"AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "PLTR", "META", "TSLA", "JPM", "GS", "BAC", "V", "MA", "JNJ", "INTC", "ORCL", "CSCO", "ABBV", "CRWD", "HOOD", "MSTR", "COIN", "CRM", "SPY", "QQQ", "EWJ", "EWY", "CVX", "LIN", "MRVL", "APP", "UNH", "AVGO", "NFLX", "AMD", "XOM", "LLY", "KO", "PEP", "WMT", "DIS", "HD", "PG", "PFE", "MRK", "BMY", "GILD", "AMGN", "MDT", "TMO", "ABT", "DHR", "ACN", "QCOM", "TXN", "CAT", "BA", "GE", "HON", "UPS", "DE", "MMM", "AXP", "LOW", "SBUX", "NKE", "MCD", "TGT", "COST", "ZTS", "SPGI", "BLK", "MS", "BK", "C", "USB", "SCHW", "VRTX", "BIIB", "REGN", "ILMN", "IDXX", "WAT", "DXCM", "BIO", "RGEN", "TECH", "PACB", "TWST", "DNA", "HOOD", "CRCL", "MSTR", "COIN", "SPACEX"}
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
