"""Reference price fetchers for cross-venue arbitrage comparison.

For each Hotstuff perp category, we need reference prices from other venues:

RWA Stocks (AAPL, MSFT, NVDA, PLTR, etc.):
  - Yahoo Finance (primary) — delayed but reliable for US equities
  - Google Finance — alternative
  - Compare Hotstuff perp price vs. stock spot price
  - Key: stocks trade M-F 9:30-16:00 ET only; perps trade 24/7
  - After-hours divergence is the main arb opportunity

Commodities (GOLD, SILVER, OIL, NATGAS):
  - Yahoo Finance futures (GC=F, SI=F, CL=F, NG=F)
  - Alpha Vantage for spot prices
  - Compare perp vs. nearest futures contract

Crypto (BTC, ETH, SOL, etc.):
  - Binance API (public, no auth)
  - Compare Hotstuff perp vs. Binance perp/spot
  - Tighter spreads, lower arb potential but more liquid

Forex (EURUSD, USDJPY):
  - ExchangeRate-API or similar
  - Compare perp vs. forex spot

Indices (USA500, USA100):
  - Yahoo Finance (^GSPC, ^NDX)
  - Similar to stocks — only trade market hours
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
from loguru import logger


@dataclass
class ReferencePrice:
    """A reference price from an external venue."""
    symbol: str              # e.g. "AAPL"
    venue: str               # e.g. "yahoo_finance"
    price: float
    timestamp: float         # unix timestamp
    bid: float = 0.0
    ask: float = 0.0
    source: str = ""         # e.g. "spot", "perp", "futures"
    raw: dict = field(default_factory=dict)

    @property
    def spread_pct(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.ask - self.bid) / self.price * 100
        return 0.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp


@dataclass
class PriceDivergence:
    """Divergence between Hotstuff perp and a reference venue."""
    hotstuff_symbol: str
    reference_venue: str
    reference_symbol: str
    hotstuff_price: float
    reference_price: float
    divergence_usd: float
    divergence_bps: float
    hotstuff_funding_8h: float
    timestamp: float

    @property
    def direction(self) -> str:
        """Which way to trade: 'long_hotstuff' or 'short_hotstuff'."""
        if self.divergence_bps > 0:
            return "short_hotstuff"  # Hotstuff is expensive → short it
        else:
            return "long_hotstuff"   # Hotstuff is cheap → long it

    def summary(self) -> str:
        return (
            f"{self.hotstuff_symbol}: Hotstuff={self.hotstuff_price:.2f} vs "
            f"{self.reference_venue}={self.reference_price:.2f} | "
            f"div={self.divergence_bps:+.1f}bps ({self.divergence_usd:+.2f}) | "
            f"trade: {self.direction} | fund={self.hotstuff_funding_8h:+.6f}"
        )


class YahooFinanceClient:
    """Fetch reference prices from Yahoo Finance (public API, no auth)."""

    BASE = "https://query1.finance.yahoo.com"

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _client_obj(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                },
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_quote(self, symbol: str) -> ReferencePrice | None:
        """Get current quote for a Yahoo Finance symbol."""
        try:
            client = await self._client_obj()
            url = f"{self.BASE}/v8/finance/chart/{symbol}"
            params = {"interval": "1m", "range": "1d"}
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            result = data.get("chart", {}).get("result", [])
            if not result:
                return None

            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice", 0)
            if price == 0:
                # Try indicators
                indicators = result[0].get("indicators", {}).get("quote", [{}])
                if indicators:
                    closes = indicators[0].get("close", [])
                    if closes:
                        price = closes[-1]

            if not price:
                return None

            return ReferencePrice(
                symbol=symbol,
                venue="yahoo_finance",
                price=float(price),
                timestamp=time.time(),
                bid=float(meta.get("bid", 0) or 0),
                ask=float(meta.get("ask", 0) or 0),
                source="spot",
                raw=meta,
            )
        except Exception as e:
            logger.warning(f"[YahooFinance] Failed to fetch {symbol}: {e}")
            return None

    async def get_quotes(self, symbols: list[str]) -> dict[str, ReferencePrice]:
        """Fetch multiple quotes. Yahoo allows batch via comma-separated."""
        results = {}
        # Yahoo allows up to ~10 symbols per request
        chunk_size = 8
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            try:
                client = await self._client_obj()
                symbols_str = ",".join(chunk)
                url = f"{self.BASE}/v8/finance/chart/{symbols_str}"
                # Actually Yahoo batch is different — do individual for reliability
                tasks = [self.get_quote(s) for s in chunk]
                for sym, result in zip(chunk, tasks):
                    r = await result
                    if r:
                        results[sym] = r
            except Exception as e:
                logger.warning(f"[YahooFinance] Batch fetch failed: {e}")
        return results


class BinanceClient:
    """Fetch reference prices from Binance public API."""

    BASE = "https://api.binance.com"
    FAPI_BASE = "https://fapi.binance.com"

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _client_obj(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_spot_price(self, symbol: str) -> ReferencePrice | None:
        """Get spot price (e.g. 'BTCUSDT')."""
        try:
            client = await self._client_obj()
            resp = await client.get(f"{self.BASE}/api/v3/ticker/bookTicker", params={"symbol": symbol})
            resp.raise_for_status()
            data = resp.json()
            return ReferencePrice(
                symbol=symbol,
                venue="binance_spot",
                price=(float(data["bidPrice"]) + float(data["askPrice"])) / 2,
                timestamp=time.time(),
                bid=float(data["bidPrice"]),
                ask=float(data["askPrice"]),
                source="spot",
            )
        except Exception as e:
            logger.warning(f"[Binance] Failed to fetch spot {symbol}: {e}")
            return None

    async def get_perp_price(self, symbol: str) -> ReferencePrice | None:
        """Get perp/futures price (e.g. 'BTCUSDT' for USDT-margined perp)."""
        try:
            client = await self._client_obj()
            resp = await client.get(
                f"{self.FAPI_BASE}/fapi/v1/ticker/bookTicker",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            data = resp.json()
            return ReferencePrice(
                symbol=symbol,
                venue="binance_perp",
                price=(float(data["bidPrice"]) + float(data["askPrice"])) / 2,
                timestamp=time.time(),
                bid=float(data["bidPrice"]),
                ask=float(data["askPrice"]),
                source="perp",
            )
        except Exception as e:
            logger.warning(f"[Binance] Failed to fetch perp {symbol}: {e}")
            return None


# ── Symbol mapping: Hotstuff perp → reference venue symbols ──────────────────

# RWA stocks: Hotstuff uses AAPL-PERP, Yahoo uses AAPL
RWA_SYMBOL_MAP: dict[str, str] = {
    "AAPL-PERP": "AAPL",
    "MSFT-PERP": "MSFT",
    "GOOGL-PERP": "GOOGL",
    "AMZN-PERP": "AMZN",
    "NVDA-PERP": "NVDA",
    "PLTR-PERP": "PLTR",
    "META-PERP": "META",
    "TSLA-PERP": "TSLA",
    "JPM-PERP": "JPM",
    "GS-PERP": "GS",
    "BAC-PERP": "BAC",
    "V-PERP": "V",
    "MA-PERP": "MA",
    "JNJ-PERP": "JNJ",
    "INTC-PERP": "INTC",
    "ORCL-PERP": "ORCL",
    "CSCO-PERP": "CSCO",
    "ABBV-PERP": "ABBV",
    "CRWD-PERP": "CRWD",
    "HOOD-PERP": "HOOD",
    "MSTR-PERP": "MSTR",
    "COIN-PERP": "COIN",
    "CRM-PERP": "CRM",
    "SPY-PERP": "SPY",
    "QQQ-PERP": "QQQ",
    "EWJ-PERP": "EWJ",
    "EWY-PERP": "EWY",
    "CVX-PERP": "CVX",
    "LIN-PERP": "LIN",
    "MRVL-PERP": "MRVL",
    "APP-PERP": "APP",
    "UNH-PERP": "UNH",
    "AVGO-PERP": "AVGO",
}

# Commodities: Hotstuff → Yahoo futures
COMMODITY_SYMBOL_MAP: dict[str, str] = {
    "GOLD-PERP": "GC=F",       # Gold futures
    "SILVER-PERP": "SI=F",     # Silver futures
    "WTIOIL-PERP": "CL=F",     # WTI Crude Oil futures
    "BRENTOIL-PERP": "BZ=F",   # Brent Oil futures
    "NATGAS-PERP": "NG=F",     # Natural Gas futures
}

# Crypto: Hotstuff → Binance
CRYPTO_SYMBOL_MAP: dict[str, str] = {
    "BTC-PERP": "BTCUSDT",
    "ETH-PERP": "ETHUSDT",
    "SOL-PERP": "SOLUSDT",
    "XRP-PERP": "XRPUSDT",
    "BNB-PERP": "BNBUSDT",
    "HYPE-PERP": "HYPEUSDT",
}

# Indices: Hotstuff → Yahoo
INDEX_SYMBOL_MAP: dict[str, str] = {
    "USA500-PERP": "^GSPC",    # S&P 500
    "USA100-PERP": "^NDX",     # Nasdaq 100
}


def compute_divergence(
    hotstuff_price: float,
    reference_price: float,
    hotstuff_symbol: str,
    reference_venue: str,
    reference_symbol: str,
    funding_8h: float = 0.0,
) -> PriceDivergence:
    """Compute price divergence between Hotstuff and reference."""
    div_usd = hotstuff_price - reference_price
    div_bps = div_usd / reference_price * 10_000 if reference_price > 0 else 0
    return PriceDivergence(
        hotstuff_symbol=hotstuff_symbol,
        reference_venue=reference_venue,
        reference_symbol=reference_symbol,
        hotstuff_price=hotstuff_price,
        reference_price=reference_price,
        divergence_usd=div_usd,
        divergence_bps=div_bps,
        hotstuff_funding_8h=funding_8h,
        timestamp=time.time(),
    )
