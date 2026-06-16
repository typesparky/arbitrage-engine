# Factor Model v2 Research Spec

## Dead/Alive Factor (Survival/Activity)

### Core Idea
Captures whether a crypto project is actively maintained, used, and viable versus abandoned ("zombie" or dead coins). Orthogonal to price/size/momentum — many coins linger with low/no activity but still have market cap.

**Classic trade: Long HYPE / short BCH**

### Data Sources
- CoinGecko/CoinGecko APIs: listings, volume, market cap, exchange count
- DefiLlama: TVL per chain
- Hyperliquid: funding rates, OI
- GitHub: commits, stars, forks, contributors (via API)
- LunarCrush/Santiment: social volume, sentiment
- Google Trends: search volume

### Feature Categories

#### 1. On-Chain Activity (Strongest Signals)
- Active addresses: daily/weekly unique senders/receivers, trend slope (30-90d)
- Transaction count/volume: on-chain txs/day, adjusted for spam
- Transaction fees paid: total fees (real usage vs dust)
- Active supply / HODL waves: % supply moved in 30d/90d/1y
- Coin-age destroyed: volume × days since last move
- NVT ratio: network value / transaction volume
- Developer/contract activity: gas used, unique contracts, DeFi TVL
- Node count / hashrate (PoW): declining = dying security

#### 2. Development & Maintenance
- GitHub activity: commits, stars, forks, contributors, issues/PRs closed (30-90d)
- Full-history decay: exponential weighted activity
- Whitepaper/repo age vs recent updates
- Token contract changes: renounced ownership, minting events

#### 3. Market & Liquidity Signals
- Trading volume / market cap ratio: low and declining = zombie
- Exchange listings: number, tier, recent additions/delisting risk
- Bid-ask spread / depth: illiquidity signals death
- Market cap rank stability: sharp drops
- Age of coin: older coins need stronger activity to score "alive"

#### 4. Social & Attention
- Social volume/sentiment: LunarCrush, Santiment
- Google Trends / search volume
- Community metrics: Telegram/Discord active users

#### 5. Composite Transforms
- Survival score: PCA or weighted sum (40% on-chain, 30% dev, 20% liquidity, 10% social)
- Decay metrics: EMA of activity with 90d half-life
- Full-history: cumulative activity normalized by age, % of peak activity
- Volatility-adjusted: activity / realized vol
- Peer-relative: z-score vs same-age or same-sector coins
- Hazard rate: low MCAP/vol/social + high vol → higher death probability

### Implementation Notes
- Handle missing data (new coins short history, dead coins drop from APIs)
- Rebalance weekly/monthly
- Backtest with delisting returns (-50% to -100%)
- Survivorship bias is huge — use point-in-time universe
- Orthogonal to Q-7: should add explanatory power for small/illiquid names
- Premium for "alive" coins especially in bear markets

---

## Enhanced Momentum Factor

### Short-Term (7-21d)
- Raw past return: cumulative 7d/14d/21d (skip last 1d)
- Risk-adjusted: return/volatility (Sharpe over lookback)
- Volume-weighted: past return × avg volume
- Cross-sectional rank: percentile rank vs all coins
- Time-series: coin return vs own moving average

### Long-Term / Full-History Transforms
- Cumulative return since inception vs BTC/ETH (relative strength)
- EWMA momentum: long half-life (6-12 months)
- Price relative to ATH: % from ATH, time since ATH
- Hurst exponent: long-memory in returns (full series)
- Regime-based: HIDDEN Markov Model or MA crossover over full history
- Log-price trend slope: linear regression on log(price)
- Momentum decay: recent return minus long-term average
- Multi-horizon blend: short (7-21d) + medium (3-6m) + long (1y+)
- Liquidity-filtered: only compute on high-volume periods
- Relative to peers/sector: full-history beta to sector + residual momentum

### Composite Transforms
- MACD with very long parameters
- 52-week high ratio (adapted to coin age)
- Kalman filter smoothed trend
- ML features: LSTM autoencoder on full price/volume history

### Practical Notes
- Crypto momentum is short-lived and volatile
- Transaction costs kill long holds — use for ranking + overlays
- Orthogonal to dead/alive: dead coin can have short-term momentum (pumps)
- Backtest with survivorship-free data, account for delistings

---

## Q-7 Factors (from Babayeva & Aliyev 2026)
1. Market (MKT): cap-weighted market return
2. Size (SMB): negative log market cap
3. Reversal (REV): negated 21-day return
4. Value (VAL): on-chain valuation composite (orthogonalized to size)
5. Volatility (VOL): residual volatility (orthogonalized to size)
6. Quality (QUA): on-chain activity composite (orthogonalized to size, value)
7. Funding (FND): perpetual funding rate (orthogonalized to size, reversal)

## Extended Model: Q-7 + DA + MOM = Q-9
8. Dead/Alive (DA): survival/activity composite
9. Momentum (MOM): multi-horizon enhanced momentum

## Backtest Methodology
- Fama-MacBeth cross-sectional regressions
- Quintile long-short portfolios (top minus bottom)
- IS/OOS split (70/30)
- Newey-West t-statistics (5 lags)
- Cross-sectional R² and portfolio R²
- Sharpe ratios, max drawdown
- Survivorship bias adjustment
