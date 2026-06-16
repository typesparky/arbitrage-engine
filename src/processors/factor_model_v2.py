"""Crypto Factor Model v2: Q-7 + Dead/Alive + Enhanced Momentum.

Extends QuantNest-7 (Babayeva & Aliyev 2026) with:
  8. Dead/Alive: survival/activity composite (on-chain, liquidity, longevity)
  9. Momentum: multi-horizon (7-63d) + full-history transforms

Data: CoinGecko (free), DefiLlama, Hyperliquid. All rate-limited.
"""

from __future__ import annotations

import asyncio
import math
import statistics
from dataclasses import dataclass, field

import httpx
from loguru import logger


# ── Data ──────────────────────────────────────────────────────────────

@dataclass
class TokenData:
    symbol: str
    name: str
    coingecko_id: str
    sector: str = "Other"
    prices: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    market_caps: list[float] = field(default_factory=list)
    tvl: float = 0.0
    funding_rate: float = 0.0
    days_alive: int = 0
    rank: int = 0
    num_exchanges: int = 0

    @property
    def price(self) -> float:
        return self.prices[-1] if self.prices else 0

    @property
    def market_cap(self) -> float:
        return self.market_caps[-1] if self.market_caps else 0

    @property
    def volume_24h(self) -> float:
        return self.volumes[-1] if self.volumes else 0


class DataFetcher:
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

    async def _rl(self):
        await asyncio.sleep(3)

    async def fetch_overview(self, per_page: int = 25) -> list[dict]:
        client = await self._c()
        try:
            r = await client.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": per_page, "page": 1,
                        "price_change_percentage": "7d,30d"},
            )
            return r.json() if r.status_code == 200 else []
        except Exception as e:
            logger.warning(f"Overview error: {e}")
            return []

    async def fetch_history(self, coin_id: str, days: int = 90) -> dict | None:
        await self._rl()
        client = await self._c()
        try:
            r = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days, "interval": "daily"},
            )
            if r.status_code != 200:
                return None
            d = r.json()
            return {
                "prices": [p[1] for p in d.get("prices", [])],
                "volumes": [v[1] for v in d.get("total_volumes", [])],
                "market_caps": [m[1] for m in d.get("market_caps", [])],
            }
        except Exception as e:
            logger.warning(f"History error {coin_id}: {e}")
            return None

    async def fetch_info(self, coin_id: str) -> dict | None:
        await self._rl()
        client = await self._c()
        try:
            r = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}",
                params={"localization": "false", "tickers": "false",
                        "market_data": "false", "community_data": "false"},
            )
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            logger.warning(f"Info error {coin_id}: {e}")
            return None

    async def fetch_tvl(self, chain: str) -> list[dict] | None:
        client = await self._c()
        try:
            r = await client.get(f"https://api.llama.fi/v2/historicalChainTvl/{chain}")
            if r.status_code == 200 and isinstance(r.json(), list):
                return [{"date": d["date"], "tvl": d["tvl"]} for d in r.json()]
            return None
        except Exception as e:
            logger.warning(f"TVL error {chain}: {e}")
            return None

    async def fetch_funding(self, symbol: str) -> float:
        client = await self._c()
        try:
            r = await client.post("https://api.hyperliquid.xyz/info",
                                  json={"type": "metaAndAssetCtxs"})
            if r.status_code == 200:
                meta, ctxs = r.json()
                for i, u in enumerate(meta.get("universe", [])):
                    if u["name"] == symbol and i < len(ctxs):
                        return float(ctxs[i].get("funding", 0))
            return 0
        except Exception:
            return 0


SECTOR_MAP = {
    "bitcoin": "L1", "ethereum": "L1", "solana": "L1", "binancecoin": "L1",
    "ripple": "L1", "cardano": "L1", "dogecoin": "L1", "polkadot": "L1",
    "avalanche-2": "L1", "chainlink": "L1", "near": "L1", "aptos": "L1",
    "sui": "L1", "toncoin": "L1", "tron": "L1", "litecoin": "L1",
    "bitcoin-cash": "L1", "zcash": "L1", "cosmos": "L1", "hedera": "L1",
    "algorand": "L1", "fantom": "L1", "tezos": "L1", "eos": "L1",
    "stellar": "L1", "kaspa": "L1", "stacks": "L1", "filecoin": "L1",
    "optimism": "L2", "arbitrum": "L2", "polygon-ecosystem-token": "L2",
    "immutable-x": "L2", "starknet": "L2", "scroll": "L2", "mantle": "L2",
    "uniswap": "DeFi", "aave": "DeFi", "maker": "DeFi",
    "curve-dao-token": "DeFi", "lido-dao": "DeFi", "pendle": "DeFi",
    "dydx": "DeFi", "gmx": "DeFi", "the-graph": "Infra",
    "render-token": "Infra", "fetch-ai": "Infra",
    "pepe": "Meme", "shiba-inu": "Meme", "floki": "Meme",
    "bonk": "Meme", "dogwifhat": "Meme",
    "axie-infinity": "Game", "the-sandbox": "Game", "decentraland": "Game", "gala": "Game",
}

CHAIN_MAP = {
    "solana": "solana", "ethereum": "ethereum", "binancecoin": "bnb",
    "avalanche-2": "avalanche", "polygon-ecosystem-token": "polygon",
    "arbitrum": "arbitrum", "optimism": "optimism", "fantom": "fantom",
    "cosmos": "cosmos", "near": "near", "aptos": "aptos", "sui": "sui",
    "toncoin": "ton", "tron": "tron", "cardano": "cardano",
    "polkadot": "polkadot", "tezos": "tezos", "stellar": "stellar",
    "hedera": "hedera", "algorand": "algorand", "kaspa": "kaspa",
    "elrond-erd-2": "elrond", "flow": "flow", "stacks": "stacks",
    "internet-computer": "icp", "filecoin": "filecoin",
}


# ── Factors ───────────────────────────────────────────────────────────

@dataclass
class FactorScores:
    symbol: str
    sector: str = ""
    size: float = 0.0
    reversal: float = 0.0
    momentum_7d: float = 0.0
    momentum_14d: float = 0.0
    momentum_21d: float = 0.0
    momentum_42d: float = 0.0
    momentum_63d: float = 0.0
    momentum: float = 0.0
    value: float = 0.0
    volatility: float = 0.0
    quality: float = 0.0
    funding: float = 0.0
    dead_alive: float = 0.0
    size_z: float = 0.0
    reversal_z: float = 0.0
    momentum_z: float = 0.0
    value_z: float = 0.0
    volatility_z: float = 0.0
    quality_z: float = 0.0
    funding_z: float = 0.0
    dead_alive_z: float = 0.0
    composite_score: float = 0.0


class FactorModel:
    def compute_factors(self, tokens: list[TokenData]) -> list[FactorScores]:
        scores = []
        for token in tokens:
            s = FactorScores(symbol=token.symbol, sector=token.sector)

            if token.market_cap > 0:
                s.size = -math.log(token.market_cap)

            if len(token.prices) >= 22 and token.prices[-22] > 0:
                ret_21d = (token.prices[-1] - token.prices[-22]) / token.prices[-22]
                s.reversal = -ret_21d

            if token.market_cap > 0 and len(token.volumes) >= 30:
                avg_vol = statistics.mean(token.volumes[-30:])
                if avg_vol > 0:
                    s.value = math.log(token.market_cap / (avg_vol * 30))

            if len(token.prices) >= 30:
                rets = []
                for i in range(max(1, len(token.prices) - 30), len(token.prices)):
                    if token.prices[i - 1] > 0:
                        rets.append((token.prices[i] - token.prices[i - 1]) / token.prices[i - 1])
                if len(rets) >= 10:
                    lam = 0.5 ** (1 / 14)
                    var = rets[0] ** 2
                    for r in rets[1:]:
                        var = lam * var + (1 - lam) * r ** 2
                    s.volatility = math.sqrt(var)

            quality_signals = []
            if token.market_cap > 0 and token.volume_24h > 0:
                quality_signals.append(token.volume_24h / token.market_cap)
            if token.market_cap > 0 and token.tvl > 0:
                quality_signals.append(token.tvl / token.market_cap)
            if len(token.volumes) >= 30:
                recent = statistics.mean(token.volumes[-7:])
                prior = statistics.mean(token.volumes[-30:-7]) if len(token.volumes) > 7 else 0
                if prior > 0:
                    quality_signals.append(recent / prior)
            if quality_signals:
                s.quality = statistics.mean(quality_signals)

            if token.funding_rate != 0:
                s.funding = -token.funding_rate

            for days, attr in [(7, "momentum_7d"), (14, "momentum_14d"),
                               (21, "momentum_21d"), (42, "momentum_42d"),
                               (63, "momentum_63d")]:
                if len(token.prices) > days + 1 and token.prices[-(days + 2)] > 0:
                    ret = (token.prices[-2] - token.prices[-(days + 2)]) / token.prices[-(days + 2)]
                    setattr(s, attr, ret)

            mom_vals = [getattr(s, a) for a in ["momentum_7d", "momentum_14d", "momentum_21d",
                                                  "momentum_42d", "momentum_63d"] if getattr(s, a) != 0]
            if mom_vals:
                s.momentum = statistics.mean(mom_vals)

            # Dead/Alive composite
            da_signals = []
            onchain = []
            if token.market_cap > 0 and token.volume_24h > 0:
                onchain.append(min(token.volume_24h / token.market_cap * 100, 5))
            if token.tvl > 0 and token.market_cap > 0:
                onchain.append(min(token.tvl / token.market_cap * 10, 5))
            if len(token.volumes) >= 30:
                rv = statistics.mean(token.volumes[-7:])
                ov = statistics.mean(token.volumes[-30:-7]) if len(token.volumes) > 7 else rv
                if ov > 0:
                    onchain.append(min(rv / ov, 5))
            if onchain:
                da_signals.append((statistics.mean(onchain), 0.4))

            liquidity = []
            if token.market_cap > 0 and token.volume_24h > 0:
                liquidity.append(min(token.volume_24h / token.market_cap * 50, 5))
            if token.num_exchanges > 0:
                liquidity.append(min(token.num_exchanges / 10, 5))
            if liquidity:
                da_signals.append((statistics.mean(liquidity), 0.3))

            longevity = []
            if token.days_alive > 0:
                age_score = min(math.log(token.days_alive) / math.log(3650), 1) * 5
                longevity.append(age_score)
                if token.days_alive > 365 and len(token.volumes) >= 30:
                    rv = statistics.mean(token.volumes[-7:])
                    ov = statistics.mean(token.volumes[-30:-7]) if len(token.volumes) > 7 else rv
                    if ov > 0 and rv / ov < 0.3:
                        longevity.append(0.5)
                    elif ov > 0 and rv / ov > 2.0:
                        longevity.append(5.0)
            if longevity:
                da_signals.append((statistics.mean(longevity), 0.3))

            if da_signals:
                tw = sum(w for _, w in da_signals)
                s.dead_alive = sum(sc * w for sc, w in da_signals) / tw if tw > 0 else 0

            scores.append(s)

        for attr in ["size", "reversal", "momentum", "value", "volatility",
                     "quality", "funding", "dead_alive"]:
            self._z_score(scores, attr)

        self._orthogonalize(scores, "value", ["size"])
        self._orthogonalize(scores, "volatility", ["size"])
        self._orthogonalize(scores, "quality", ["size", "value"])
        self._orthogonalize(scores, "funding", ["size", "reversal"])

        for s in scores:
            factors = [s.size_z, s.reversal_z, s.momentum_z, s.value_z,
                       s.volatility_z, s.quality_z, s.funding_z, s.dead_alive_z]
            valid = [f for f in factors if f == f]
            if valid:
                s.composite_score = statistics.mean(valid)

        return scores

    def _z_score(self, scores: list[FactorScores], attr: str):
        vals = [(s, getattr(s, attr)) for s in scores if getattr(s, attr) != 0]
        if len(vals) < 3:
            return
        v = [x for _, x in vals]
        mean, std = statistics.mean(v), statistics.stdev(v)
        if std == 0:
            return
        for s, val in vals:
            setattr(s, f"{attr}_z", (val - mean) / std)

    def _orthogonalize(self, scores: list[FactorScores], target: str, controls: list[str]):
        for s in scores:
            y = getattr(s, f"{target}_z")
            if y == 0:
                continue
            residual = y
            for c in controls:
                x_val = getattr(s, f"{c}_z")
                x_all = [getattr(sc, f"{c}_z") for sc in scores if getattr(sc, f"{target}_z") != 0]
                y_all = [getattr(sc, f"{target}_z") for sc in scores if getattr(sc, f"{target}_z") != 0]
                ss_xy = sum(xi * yi for xi, yi in zip(x_all, y_all))
                ss_xx = sum(xi ** 2 for xi in x_all)
                if ss_xx > 0:
                    residual -= (ss_xy / ss_xx) * x_val
            setattr(s, f"{target}_z", residual)


# ── Universe ──────────────────────────────────────────────────────────

async def build_universe(fetcher: DataFetcher, max_tokens: int = 20) -> list[TokenData]:
    overview = await fetcher.fetch_overview(per_page=max_tokens)
    if not overview:
        return []

    tokens = []
    for coin in overview:
        sym = coin["symbol"].lower()
        cid = coin["id"].lower()
        if any(k in sym for k in ["usd", "dai", "busd", "tusd", "usdc", "usdt"]):
            continue
        if any(k in cid for k in ["wrapped", "bridged", "synthetic"]):
            continue

        sector = SECTOR_MAP.get(coin["id"], "Other")
        hist = await fetcher.fetch_history(coin["id"], days=90)
        info = await fetcher.fetch_info(coin["id"])

        token = TokenData(
            symbol=coin["symbol"].upper(), name=coin["name"],
            coingecko_id=coin["id"], sector=sector,
            rank=coin.get("market_cap_rank", 0),
        )

        if hist and hist["prices"]:
            token.prices = hist["prices"]
            token.volumes = hist["volumes"]
            token.market_caps = hist["market_caps"]

        if info:
            gd = info.get("genesis_date")
            if gd:
                try:
                    from datetime import datetime
                    token.days_alive = (datetime.now() - datetime.strptime(gd, "%Y-%m-%d")).days
                except Exception:
                    pass
            tickers = info.get("tickers", [])
            if tickers:
                token.num_exchanges = len(set(
                    t.get("market", {}).get("identifier", "")
                    for t in tickers if t.get("market")
                ))

        chain = CHAIN_MAP.get(coin["id"])
        if chain:
            tvl_data = await fetcher.fetch_tvl(chain)
            if tvl_data:
                token.tvl = tvl_data[-1]["tvl"] if tvl_data else 0

        token.funding_rate = await fetcher.fetch_funding(coin["symbol"].upper())
        tokens.append(token)

    return tokens
