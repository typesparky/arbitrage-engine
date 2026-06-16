"""Hotstuff DEX market data client.

Fetches real-time perp prices, funding rates, and metadata from Hotstuff API.
All endpoints are public (no auth needed) POST to https://api.hotstuff.trade/info.

Key concepts:
    - Mark price: used for PnL / liquidations
    - Index price: external oracle reference (spot equivalent)
    - Funding rate: hourly rate paid/received between longs/shorts
    - Spread: bid-ask spread as % of mid price
    - Premium: (mark - index) / index — the divergence from spot reference

Arbitrage signals:
    1. Cross-venue: Hotstuff perp vs. other perps (dYdX, GMX, etc.) or spot (Binance)
    2. Funding rate arb: capture rebate when funding is very positive/negative
    3. RWA divergence: stock perps trade 24/7 but underlying only M-F 9:30-16:00 ET
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
from loguru import logger

_HOTSTUFF_BASE = "https://api.hotstuff.trade"


@dataclass
class HotstuffTicker:
    """Single instrument ticker."""
    symbol: str
    instrument_type: str  # "perp"
    instrument_id: int
    mark_price: float
    mid_price: float
    index_price: float
    best_bid_price: float
    best_ask_price: float
    best_bid_size: float
    best_ask_size: float
    funding_rate: float  # 8-hour rate
    open_interest: float
    volume_24h: float
    change_24h: float
    last_price: float

    # Derived
    spread_pct: float = 0.0       # (ask - bid) / mid * 100
    premium_pct: float = 0.0      # (mark - index) / index * 100
    hourly_funding: float = 0.0   # funding_rate / 8
    bid_depth_usd: float = 0.0    # bid_size * bid_price
    ask_depth_usd: float = 0.0    # ask_size * ask_price

    @property
    def category(self) -> str:
        """Classify instrument into category."""
        sym = self.symbol.replace("-PERP", "")
        rwa_stocks = {
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "PLTR", "META", "TSLA",
            "JPM", "GS", "BAC", "V", "MA", "JNJ", "INTC", "ORCL", "CSCO",
            "ABBV", "CRWD", "HOOD", "MSTR", "COIN", "CRM", "SPY", "QQQ",
            "EWJ", "EWY", "CVX", "LIN", "MRVL", "APP", "UNH", "AVGO",
        }
        commodities = {"GOLD", "SILVER", "NATGAS", "WTIOIL", "BRENTOIL"}
        forex = {"EURUSD", "USDJPY"}
        indices = {"USA500", "USA100"}

        if sym in rwa_stocks:
            return "rwa_stock"
        elif sym in commodities:
            return "commodity"
        elif sym in forex:
            return "forex"
        elif sym in indices:
            return "index"
        else:
            return "crypto"

    @property
    def is_liquid(self) -> bool:
        """Minimum liquidity check for safe trading."""
        return self.volume_24h > 10_000 and self.spread_pct < 0.5 and self.bid_depth_usd > 50 and self.ask_depth_usd > 50


@dataclass
class HotstuffInstrument:
    """Static instrument metadata."""
    id: int
    name: str
    price_index: str
    lot_size: float
    tick_size: float
    max_leverage: int
    only_isolated: bool
    delisted: bool
    min_notional_usd: float
    growth_mode: bool


class HotstuffClient:
    """Async client for Hotstuff public API."""

    def __init__(self, base_url: str = _HOTSTUFF_BASE, timeout: float = 10.0):
        self.base_url = base_url
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._instruments: dict[str, HotstuffInstrument] = {}
        self._perp_id_map: dict[int, str] = {}  # id -> symbol

    async def _client_obj(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _post(self, method: str, params: dict) -> dict | list:
        """Make a POST request to /info endpoint."""
        client = await self._client_obj()
        payload = {"method": method, "params": params}
        resp = await client.post(f"{self._base}/info", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def _post_exchange(self, method: str, params: dict) -> dict | list:
        """Make a POST request to /exchange endpoint (for orders)."""
        client = await self._client_obj()
        payload = {"method": method, "params": params}
        resp = await client.post(f"{self._base}/exchange", json=payload)
        resp.raise_for_status()
        return resp.json()

    @property
    def _base(self) -> str:
        return self.base_url

    async def get_instruments(self, instr_type: str = "perps") -> list[HotstuffInstrument]:
        """Fetch all instrument metadata."""
        data = await self._post("instruments", {"type": instr_type})
        perps = data.get("perps", [])
        instruments = []
        for p in perps:
            if p.get("delisted"):
                continue
            inst = HotstuffInstrument(
                id=p["id"],
                name=p["name"],
                price_index=p["price_index"],
                lot_size=p["lot_size"],
                tick_size=p["tick_size"],
                max_leverage=p["max_leverage"],
                only_isolated=p.get("only_isolated", False),
                delisted=p.get("delisted", False),
                min_notional_usd=p.get("min_notional_usd", 10),
                growth_mode=p.get("growth_mode", False),
            )
            instruments.append(inst)
            self._instruments[inst.name] = inst
            self._perp_id_map[inst.id] = inst.name
        logger.info(f"[Hotstuff] Loaded {len(instruments)} instruments ({instr_type})")
        return instruments

    async def get_tickers(self, symbol: str = "all") -> list[HotstuffTicker]:
        """Fetch tickers for all instruments or a specific symbol."""
        if symbol == "all":
            data = await self._post("ticker", {"symbol": "all"})
        else:
            data = await self._post("ticker", {"symbol": symbol})

        tickers = []
        for t in data:
            try:
                bp = float(t.get("best_bid_price", 0) or 0)
                ap = float(t.get("best_ask_price", 0) or 0)
                mp = float(t.get("mid_price", 0) or 0)
                idx = float(t.get("index_price", 0) or 0)
                mark = float(t.get("mark_price", 0) or 0)
                fund_raw = t.get("funding_rate", "0") or "0"
                fund = float(fund_raw)
                bs = float(t.get("best_bid_size", 0) or 0)
                a_s = float(t.get("best_ask_size", 0) or 0)
                oi = float(t.get("open_interest", 0) or 0)
                vol = float(t.get("volume_24h", 0) or 0)
                chg = float(t.get("change_24h", 0) or 0)
                lp = float(t.get("last_price", 0) or 0)

                spread_pct = (ap - bp) / mp * 100 if mp > 0 and ap > 0 and bp > 0 else 0.0
                premium_pct = (mark - idx) / idx * 100 if idx > 0 else 0.0

                # Find instrument id
                inst_id = 0
                if t["symbol"] in self._instruments:
                    inst_id = self._instruments[t["symbol"]].id
                elif t.get("instrument_id"):
                    inst_id = t["instrument_id"]

                ticker = HotstuffTicker(
                    symbol=t["symbol"],
                    instrument_type=t.get("type", "perp"),
                    instrument_id=inst_id,
                    mark_price=mark,
                    mid_price=mp,
                    index_price=idx,
                    best_bid_price=bp,
                    best_ask_price=ap,
                    best_bid_size=bs,
                    best_ask_size=a_s,
                    funding_rate=fund,
                    open_interest=oi,
                    volume_24h=vol,
                    change_24h=chg,
                    last_price=lp,
                    spread_pct=spread_pct,
                    premium_pct=premium_pct,
                    hourly_funding=fund / 8.0,
                    bid_depth_usd=bs * bp,
                    ask_depth_usd=a_s * ap,
                )
                tickers.append(ticker)
            except (ValueError, KeyError, TypeError) as e:
                logger.warning(f"[Hotstuff] Skipping ticker {t.get('symbol', '?')}: {e}")
                continue

        return tickers

    async def get_orderbook(self, symbol: str) -> dict:
        """Fetch orderbook for a specific symbol."""
        data = await self._post("orderbook", {"symbol": symbol})
        return data

    async def get_bbo(self, symbol: str = "all") -> list[dict]:
        """Fetch best bid/offer for all instruments."""
        data = await self._post("bbo", {"symbol": symbol})
        return data

    async def get_chart(
        self,
        symbol: str | int,
        chart_type: str = "mark",
        resolution: str = "60",
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[dict]:
        """Fetch OHLCV chart data.

        Args:
            symbol: Instrument name (e.g. "GOLD-PERP") or numeric id (e.g. 4)
            chart_type: "mark" | "index" | "ltp"
            resolution: "1"|"5"|"15"|"30"|"60"|"240"|"1D"|"1W"
            from_ts: Start timestamp in ms
            to_ts: End timestamp in ms
        """
        if from_ts is None:
            from_ts = int((time.time() - 7 * 86400) * 1000)
        if to_ts is None:
            to_ts = int(time.time() * 1000)

        data = await self._post("chart", {
            "symbol": str(symbol),
            "chart_type": chart_type,
            "resolution": resolution,
            "from": from_ts,
            "to": to_ts,
        })
        return data if isinstance(data, list) else []

    async def get_trades(self, symbol: str, limit: int = 50) -> list[dict]:
        """Fetch recent trades for a symbol."""
        data = await self._post("trades", {"symbol": symbol, "limit": limit})
        return data if isinstance(data, list) else []

    async def get_oracle(self, symbol: str) -> dict:
        """Fetch oracle price for a symbol."""
        data = await self._post("oracle", {"symbol": symbol})
        return data

    async def initialize(self) -> "HotstuffClient":
        """Load instruments — call before using get_tickers() with id resolution."""
        await self.get_instruments("perps")
        return self
