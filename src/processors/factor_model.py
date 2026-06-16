"""Crypto Factor Model: Q-7 + Momentum + Dead/Alive.

Implements the QuantNest-7 model from Babayeva & Aliyev (2026) plus two
additional factors the paper is missing:

Q-7 Factors (from paper):
  1. Market (MKT)     — cap-weighted market return
  2. Size (SMB)       — negative log market cap
  3. Reversal (REV)   — negated 21-day return (short-term mean reversion)
  4. Value (VAL)      — on-chain valuation composite (orthogonalized to size)
  5. Volatility (VOL) — residual volatility (orthogonalized to size)
  6. Quality (QUA)    — on-chain activity composite (orthogonalized to size, value)
  7. Funding (FND)    — perpetual funding rate (orthogonalized to size, reversal)

Additional factors:
  8. Momentum (MOM)   — multi-horizon momentum (7d/14d/21d/42d/63d robust average)
  9. Dead/Alive (DA)  — coin longevity + survival signal using full history

Data sources (all free):
  - CoinGecko: prices, market caps, volumes, 7d/30d changes
  - DefiLlama: TVL per chain (proxy for on-chain activity)
  - Hyperliquid: funding rates, mark prices
  - Hotstuff: funding rates, mark prices

The paper uses CoinMetrics (paid). We approximate with free sources:
  - Quality: active addresses → DefiLlama TVL + CoinGecko volume
  - Value: NVT ratio → market cap / (volume * 30) as proxy
  - Volatility: residual from market beta using EWMA
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field

import httpx
from loguru import logger


# ── Data fetching ────────────────────────────────────────────────────

@dataclass
class TokenData:
    """All data for a single token."""
    symbol: str
    name: str
    coingecko_id: str
    sector: str  # L1, DeFi, Infrastructure, L2, Meme, Gaming, Other

    # Price data (daily, last N days)
    prices: list[float] = field(default_factory=list)       # USD prices, oldest first
    volumes: list[float] = field(default_factory=list)
    market_caps: list[float] = field(default_factory=list)
    timestamps: list[int] = field(default_factory=list)      # unix timestamps

    # On-chain proxies
    tvl: float = 0.0           # DefiLlama TVL (for DeFi tokens)
    tvl_history: list[float] = field(default_factory=list)

    # Derivatives
    funding_rate: float = 0.0  # Current 8h funding rate

    # Metadata
    days_alive: int = 0        # Days since first CoinGecko listing
    rank: int = 0              # CoinGecko market cap rank

    @property
    def price(self) -> float:
        return self.prices[-1] if self.prices else 0

    @property
    def market_cap(self) -> float:
        return self.market_caps[-1] if self.market_caps else 0

    @property
    def volume_24h(self) -> float:
        return self.volumes[-1] if self.volumes else 0

    @property
    def n_return(self) -> float:
        """Return over last N days (using available history)."""
        if len(self.prices) < 2 or self.prices[0] <= 0:
            return 0
        return (self.prices[-1] - self.prices[0]) / self.prices[0]


class DataFetcher:
    """Fetches market data from free APIs."""

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

    async def fetch_coingecko_market_data(self, coin_id: str, days: int = 90) -> dict | None:
        """Fetch price, volume, market cap history from CoinGecko."""
        client = await self._c()
        try:
            r = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days, "interval": "daily"},
            )
            if r.status_code != 200:
                return None
            data = r.json()

            prices = [p[1] for p in data.get("prices", [])]
            volumes = [v[1] for v in data.get("total_volumes", [])]
            mcaps = [m[1] for m in data.get("market_caps", [])]
            timestamps = [p[0] for p in data.get("prices", [])]

            return {
                "prices": prices,
                "volumes": volumes,
                "market_caps": mcaps,
                "timestamps": timestamps,
            }
        except Exception as e:
            logger.warning(f"[FactorModel] CoinGecko error for {coin_id}: {e}")
            return None

    async def fetch_coingecko_overview(self) -> list[dict]:
        """Fetch top 100 coins with metadata."""
        client = await self._c()
        try:
            r = await client.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 100,
                    "page": 1,
                    "price_change_percentage": "7d,30d",
                },
            )
            if r.status_code != 200:
                return []
            return r.json()
        except Exception as e:
            logger.warning(f"[FactorModel] CoinGecko overview error: {e}")
            return []

    async def fetch_defillama_tvl(self, chain: str) -> list[dict] | None:
        """Fetch TVL history for a chain/protocol."""
        client = await self._c()
        try:
            r = await client.get(f"https://api.llama.fi/v2/historicalChainTvl/{chain}")
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, list):
                return [{"date": d["date"], "tvl": d["tvl"]} for d in data]
            return None
        except Exception as e:
            logger.warning(f"[FactorModel] DefiLlama error for {chain}: {e}")
            return None

    async def fetch_funding_rate(self, symbol: str, venue: str = "hyperliquid") -> float:
        """Fetch current funding rate."""
        client = await self._c()
        try:
            if venue == "hyperliquid":
                r = await client.post("https://api.hyperliquid.xyz/info", json={
                    "type": "metaAndAssetCtxs",
                })
                if r.status_code == 200:
                    meta, ctxs = r.json()
                    for i, u in enumerate(meta.get("universe", [])):
                        if u["name"] == symbol and i < len(ctxs):
                            return float(ctxs[i].get("funding", 0))
            return 0
        except Exception:
            return 0


# ── Factor computation ───────────────────────────────────────────────

@dataclass
class FactorScores:
    """All factor exposures for one token on one day."""
    symbol: str
    sector: str = ""
    # Raw factors
    # Raw factors
    size: float = 0.0           # -log(market_cap)
    reversal: float = 0.0       # -1 * 21-day return
    momentum_7d: float = 0.0    # 7-day return
    momentum_14d: float = 0.0   # 14-day return
    momentum_21d: float = 0.0   # 21-day return
    momentum_42d: float = 0.0   # 42-day return
    momentum_63d: float = 0.0   # 63-day return
    momentum: float = 0.0       # Robust multi-horizon momentum
    value: float = 0.0          # On-chain valuation (NVT proxy)
    volatility: float = 0.0     # Residual volatility
    quality: float = 0.0        # On-chain activity composite
    funding: float = 0.0        # Funding rate (contrarian)
    dead_alive: float = 0.0     # Longevity / survival score

    # Standardized (z-scored cross-sectionally)
    size_z: float = 0.0
    reversal_z: float = 0.0
    momentum_z: float = 0.0
    value_z: float = 0.0
    volatility_z: float = 0.0
    quality_z: float = 0.0
    funding_z: float = 0.0
    dead_alive_z: float = 0.0

    # Composite
    composite_score: float = 0.0


class FactorModel:
    """Computes Q-7 + Momentum + Dead/Alive factor exposures."""

    # Sector mapping for major tokens
    SECTOR_MAP = {
        # L1
        "bitcoin": "L1", "ethereum": "L1", "solana": "L1", "bnb": "L1",
        "ripple": "L1", "cardano": "L1", "dogecoin": "L1", "polkadot": "L1",
        "avalanche-2": "L1", "chainlink": "L1", "near": "L1", "aptos": "L1",
        "sui": "L1", "toncoin": "L1", "tron": "L1", "litecoin": "L1",
        "bitcoin-cash": "L1", "zcash": "L1", "cosmos": "L1", "hedera": "L1",
        "algorand": "L1", "fantom": "L1", "elrond-erd-2": "L1", "tezos": "L1",
        "eos": "L1", "stellar": "L1", "neo": "L1", "iota": "L1",
        "kaspa": "L1", "flow": "L1", "mina-protocol": "L1", "stacks": "L1",
        "theta-token": "L1", "vechain": "L1", "internet-computer": "L1",
        "filecoin": "L1", "arweave": "L1", "sei-network": "L1",
        "celestia": "L1", "optimism": "L2", "arbitrum": "L2",
        "polygon-ecosystem-token": "L2", "immutable-x": "L2",
        "starknet": "L2", "scroll": "L2", "mantle": "L2", "blast": "L2",
        "linea": "L2", "base": "L2",
        # DeFi
        "uniswap": "DeFi", "aave": "DeFi", "maker": "DeFi", "compound-governance-token": "DeFi",
        "curve-dao-token": "DeFi", "synthetix-network-token": "DeFi", "yearn-finance": "DeFi",
        "sushi": "DeFi", "pancakeswap-token": "DeFi", "balancer": "DeFi",
        "frax-share": "DeFi", "lido-dao": "DeFi", "rocket-pool": "DeFi",
        "pendle": "DeFi", "convex-finance": "DeFi", "frax": "DeFi",
        "dydx": "DeFi", "gmx": "DeFi", "velodrome-finance": "DeFi",
        "camelot-token": "DeFi", "trader-joe": "DeFi", "radiant-capital": "DeFi",
        "benqi": "DeFi", "spookyswap": "DeFi", "beefy-finance": "DeFi",
        "stargate-finance": "DeFi", "hop-protocol": "DeFi", "across-protocol": "DeFi",
        "gains-network": "DeFi", "gnd-protocol": "DeFi",
        # Infrastructure
        "the-graph": "Infrastructure", "render-token": "Infrastructure",
        "fetch-ai": "Infrastructure", "ocean-protocol": "Infrastructure",
        "chainlink": "Infrastructure", "api3": "Infrastructure",
        "band-protocol": "Infrastructure", "pyth-network": "Infrastructure",
        "wormhole": "Infrastructure", "layerzero": "Infrastructure",
        "axelar": "Infrastructure", "celo": "Infrastructure",
        # Meme
        "pepe": "Meme", "shiba-inu": "Meme", "floki": "Meme",
        "bonk": "Meme", "dogwifhat": "Meme", "book-of-meme": "Meme",
        "memecoin-2": "Meme", "turbo": "Meme", "brett": "Meme",
        # Gaming
        "axie-infinity": "Gaming", "the-sandbox": "Gaming", "decentraland": "Gaming",
        "gala": "Gaming", "illuvium": "Gaming", "yield-guild-games": "Gaming",
        "big-time": "Gaming", "immutable": "Gaming",
    }

    # Chain name mapping for DefiLlama
    CHAIN_MAP = {
        "solana": "solana", "ethereum": "ethereum", "bnb": "bnb",
        "avalanche-2": "avalanche", "polygon-ecosystem-token": "polygon",
        "arbitrum": "arbitrum", "optimism": "optimism", "fantom": "fantom",
        "cosmos": "cosmos", "near": "near", "aptos": "aptos", "sui": "sui",
        "toncoin": "ton", "tron": "tron", "cardano": "cardano",
        "polkadot": "polkadot", "tezos": "tezos", "stellar": "stellar",
        "hedera": "hedera", "algorand": "algorand", "kaspa": "kaspa",
        "elrond-erd-2": "elrond", "flow": "flow", "stacks": "stacks",
        "internet-computer": "icp", "filecoin": "filecoin",
    }

    def __init__(self):
        self.fetcher = DataFetcher()

    async def close(self):
        await self.fetcher.close()

    def classify_sector(self, coingecko_id: str) -> str:
        return self.SECTOR_MAP.get(coingecko_id, "Other")

    async def _fetch_genesis_date(self, fetcher: DataFetcher, coin_id: str) -> int:
        """Fetch genesis date and return days alive."""
        client = await fetcher._c()
        try:
            r = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}",
                params={"localization": "false", "tickers": "false",
                        "market_data": "false", "community_data": "false"},
            )
            if r.status_code == 200:
                data = r.json()
                genesis_str = data.get("genesis_date")
                if genesis_str:
                    from datetime import datetime
                    genesis = datetime.strptime(genesis_str, "%Y-%m-%d")
                    return (datetime.now() - genesis).days
        except Exception:
            pass
        return 0

    def compute_factors(self, tokens: list[TokenData]) -> list[FactorScores]:
        """Compute all factor exposures for all tokens.

        Follows the paper's methodology:
        1. Compute raw factor values
        2. Standardize cross-sectionally (z-score)
        3. Orthogonalize where specified
        """
        scores = []

        # ── Step 1: Raw factors ──────────────────────────────────────
        for token in tokens:
            s = FactorScores(symbol=token.symbol)
            s.sector = token.sector

            # Size: -log(market_cap)
            if token.market_cap > 0:
                s.size = -math.log(token.market_cap)

            # Reversal: -1 * 21-day return
            if len(token.prices) >= 22 and token.prices[-22] > 0:
                ret_21d = (token.prices[-1] - token.prices[-22]) / token.prices[-22]
                s.reversal = -ret_21d

            # Momentum at multiple horizons
            for days, attr in [(7, "momentum_7d"), (14, "momentum_14d"),
                               (21, "momentum_21d"), (42, "momentum_42d"),
                               (63, "momentum_63d")]:
                if len(token.prices) > days and token.prices[-(days + 1)] > 0:
                    ret = (token.prices[-1] - token.prices[-(days + 1)]) / token.prices[-(days + 1)]
                    setattr(s, attr, ret)

            # Robust momentum: average of all available horizons
            mom_values = []
            for attr in ["momentum_7d", "momentum_14d", "momentum_21d",
                         "momentum_42d", "momentum_63d"]:
                v = getattr(s, attr)
                if v != 0:
                    mom_values.append(v)
            if mom_values:
                s.momentum = statistics.mean(mom_values)

            # Value: NVT proxy = market_cap / (avg_daily_volume * 30)
            if len(token.volumes) >= 30:
                avg_vol = statistics.mean(token.volumes[-30:])
                if avg_vol > 0:
                    s.value = math.log(token.market_cap / (avg_vol * 30))

            # Quality: on-chain activity composite
            quality_signals = []
            # Signal 1: Volume / market cap (turnover ratio)
            if token.market_cap > 0 and token.volume_24h > 0:
                turnover = token.volume_24h / token.market_cap
                quality_signals.append(turnover)
            # Signal 2: TVL / market cap (for DeFi)
            if token.market_cap > 0 and token.tvl > 0:
                tvl_ratio = token.tvl / token.market_cap
                quality_signals.append(tvl_ratio)
            # Signal 3: Volume trend (recent 7d avg vs prior 30d avg)
            if len(token.volumes) >= 30:
                recent = statistics.mean(token.volumes[-7:])
                prior = statistics.mean(token.volumes[-30:-7]) if len(token.volumes) > 7 else 0
                if prior > 0:
                    quality_signals.append(recent / prior)
            if quality_signals:
                s.quality = statistics.mean(quality_signals)

            # Volatility: EWMA residual volatility
            if len(token.prices) >= 30:
                returns = []
                for i in range(1, len(token.prices)):
                    if token.prices[i - 1] > 0:
                        returns.append((token.prices[i] - token.prices[i - 1]) / token.prices[i - 1])
                if len(returns) >= 14:
                    # EWMA with half-life of 14 days
                    lambda_ = 0.5 ** (1 / 14)
                    ewma_var = returns[0] ** 2
                    for r in returns[1:]:
                        ewma_var = lambda_ * ewma_var + (1 - lambda_) * r ** 2
                    s.volatility = math.sqrt(ewma_var)

            # Funding: current rate (contrarian — high funding = bearish signal)
            s.funding = -token.funding_rate  # Negate: high funding → negative exposure

            # Dead/Age: longevity score
            # Coins that have survived longer with consistent activity score higher
            # Think: HYPE (new but hyperactive) vs BCH (old but dying)
            if token.days_alive > 0:
                # Log of days alive, scaled
                age_score = math.log(token.days_alive)
                # Penalize if volume is declining (dying coin)
                if len(token.volumes) >= 30:
                    recent_vol = statistics.mean(token.volumes[-7:])
                    old_vol = statistics.mean(token.volumes[-30:-7]) if len(token.volumes) > 7 else recent_vol
                    if old_vol > 0:
                        vol_trend = recent_vol / old_vol
                        # Dying coin: old but volume declining → penalize
                        if vol_trend < 0.5 and token.days_alive > 365:
                            age_score *= 0.5
                        # Thriving coin: volume growing → boost
                        elif vol_trend > 2.0:
                            age_score *= 1.5
                s.dead_alive = age_score

            scores.append(s)

        # ── Step 2: Cross-sectional z-scoring ────────────────────────
        self._z_score(scores, "size")
        self._z_score(scores, "reversal")
        self._z_score(scores, "momentum")
        self._z_score(scores, "value")
        self._z_score(scores, "volatility")
        self._z_score(scores, "quality")
        self._z_score(scores, "funding")
        self._z_score(scores, "dead_alive")

        # ── Step 3: Orthogonalization ────────────────────────────────
        # Value orthogonalized to Size
        self._orthogonalize(scores, "value", ["size"])
        # Volatility orthogonalized to Size
        self._orthogonalize(scores, "volatility", ["size"])
        # Quality orthogonalized to Size and Value
        self._orthogonalize(scores, "quality", ["size", "value"])
        # Funding orthogonalized to Size and Reversal
        self._orthogonalize(scores, "funding", ["size", "reversal"])

        # ── Step 4: Composite score ──────────────────────────────────
        # Equal-weighted average of all standardized factor z-scores
        for s in scores:
            factors = [
                s.size_z, s.reversal_z, s.momentum_z, s.value_z,
                s.volatility_z, s.quality_z, s.funding_z, s.dead_alive_z,
            ]
            # Only average non-NaN factors
            valid = [f for f in factors if f == f]  # NaN check
            if valid:
                s.composite_score = statistics.mean(valid)

        return scores

    def _z_score(self, scores: list[FactorScores], attr: str):
        """Z-score a factor cross-sectionally."""
        values = [getattr(s, attr) for s in scores]
        non_zero = [v for v in values if v != 0]
        if len(non_zero) < 3:
            return
        mean = statistics.mean(non_zero)
        std = statistics.stdev(non_zero)
        if std == 0:
            return
        for s in scores:
            v = getattr(s, attr)
            if v != 0:
                setattr(s, f"{attr}_z", (v - mean) / std)
            else:
                setattr(s, f"{attr}_z", 0)

    def _orthogonalize(self, scores: list[FactorScores], target: str, controls: list[str]):
        """Orthogonalize target factor to control factors via cross-sectional regression.

        For each token: target_z = a + b1*control1_z + b2*control2_z + residual
        Replace target_z with residual.
        """
        # Collect data points
        y_vals = []
        x_vals = []
        for s in scores:
            y = getattr(s, f"{target}_z")
            if y == 0:
                continue
            xs = []
            for c in controls:
                xs.append(getattr(s, f"{c}_z"))
            y_vals.append(y)
            x_vals.append(xs)

        if len(y_vals) < 5:
            return

        # Simple OLS: y = Xb + e
        # For single control: b = sum(x*y) / sum(x^2)
        # For multiple controls, use simple approach
        n_controls = len(controls)
        if n_controls == 1:
            x = [xs[0] for xs in x_vals]
            ss_xy = sum(xi * yi for xi, yi in zip(x, y_vals))
            ss_xx = sum(xi ** 2 for xi in x)
            if ss_xx == 0:
                return
            b = ss_xy / ss_xx
            for s in scores:
                y = getattr(s, f"{target}_z")
                if y == 0:
                    continue
                x_val = getattr(s, f"{controls[0]}_z")
                residual = y - b * x_val
                setattr(s, f"{target}_z", residual)
        else:
            # Multiple controls: sequential orthogonalization
            for s in scores:
                y = getattr(s, f"{target}_z")
                if y == 0:
                    continue
                residual = y
                for c in controls:
                    x_val = getattr(s, f"{c}_z")
                    # Simple regression coefficient
                    x_all = [getattr(sc, f"{c}_z") for sc in scores if getattr(sc, f"{target}_z") != 0]
                    y_all = [getattr(sc, f"{target}_z") for sc in scores if getattr(sc, f"{target}_z") != 0]
                    ss_xy = sum(xi * yi for xi, yi in zip(x_all, y_all))
                    ss_xx = sum(xi ** 2 for xi in x_all)
                    if ss_xx > 0:
                        b = ss_xy / ss_xx
                        residual -= b * x_val
                setattr(s, f"{target}_z", residual)


# ── Universe builder ─────────────────────────────────────────────────

async def build_universe(fetcher: DataFetcher, max_tokens: int = 50) -> list[TokenData]:
    """Build token universe from CoinGecko top coins."""
    overview = await fetcher.fetch_coingecko_overview()
    if not overview:
        return []

    tokens = []
    for coin in overview[:max_tokens]:
        # Skip stablecoins and wrapped tokens
        symbol = coin["symbol"].lower()
        cid = coin["id"].lower()
        if any(kw in symbol for kw in ["usd", "dai", "busd", "tusd", "usdc", "usdt"]):
            continue
        if any(kw in cid for kw in ["wrapped", "bridged", "synthetic"]):
            continue

        sector = FactorModel.SECTOR_MAP.get(coin["id"], "Other")

        # Fetch historical data
        hist = await fetcher.fetch_coingecko_market_data(coin["id"], days=90)

        token = TokenData(
            symbol=coin["symbol"].upper(),
            name=coin["name"],
            coingecko_id=coin["id"],
            sector=sector,
            rank=coin.get("market_cap_rank", 0),
        )
        # Fetch genesis date from coin endpoint
        client = await fetcher._c()
        try:
            gr = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin['id']}",
                params={"localization": "false", "tickers": "false",
                        "market_data": "false", "community_data": "false"},
            )
            if gr.status_code == 200:
                gdata = gr.json()
                genesis_str = gdata.get("genesis_date")
                if genesis_str:
                    from datetime import datetime
                    genesis = datetime.strptime(genesis_str, "%Y-%m-%d")
                    token.days_alive = (datetime.now() - genesis).days
        except Exception:
            pass

        if hist and hist["prices"]:
            token.prices = hist["prices"]
            token.volumes = hist["volumes"]
            token.market_caps = hist["market_caps"]
            token.timestamps = hist["timestamps"]

        # Fetch TVL for DeFi tokens
        chain = FactorModel.CHAIN_MAP.get(coin["id"])
        if chain:
            tvl_data = await fetcher.fetch_defillama_tvl(chain)
            if tvl_data:
                token.tvl = tvl_data[-1]["tvl"] if tvl_data else 0
                token.tvl_history = [d["tvl"] for d in tvl_data[-90:]]

        # Fetch funding rate
        token.funding_rate = await fetcher.fetch_funding_rate(coin["symbol"].upper())

        tokens.append(token)

    return tokens
