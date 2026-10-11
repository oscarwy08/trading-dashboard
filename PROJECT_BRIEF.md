# Project Brief: Agentic Paper Trading System

## Owner & Goal

**Owner**: UK-based (Europe/London timezone)

**Mission**: Agentic trading for crypto + stocks driven by sentiment/semantic analysis of X (Twitter) and news combined with market data, aiming for genuinely profitable trades. Currently in PAPER trading mode with a fresh £200 GBP start for initial testing and validation. Real money deployment will only proceed if the paper trading period demonstrates consistent profitability.

## Account Structure

The system operates two separate accounts with a 50/50 split (£100 each):

### Crypto Account (£100)
- **Future Live Venue**: Dedicated Monad wallet
- **Execution**: Swaps via KyberSwap on Monad network
- **Security**: Completely separate wallet from owner's personal holdings

### Stocks Account (£100)
- **Future Live Broker**: Trading 212 Invest account via public API
- **Critical**: NOT the owner's personal ISA account
- **Security**: Dedicated account holding only bot funds
- **Access**: Separate from all personal investments

## Pot Allocation

Each account divides its funds across three pots with distinct strategies:

### CORE Pot (50% - £50 per account)
- Long-term holds with dynamic growth-themed watchlist
- Weekly Monday rebalance (09:00 UK time)
- AI review required before significant position changes
- Staggered entry via tranches (~1 per day per asset)
- 20% stop-loss, no take-profit (rebalance trims winners)

### DAY Pot (30% - £30 per account)
- **Momentum Sub-Strategy** (50% share): Original momentum + sentiment entries with upgraded exits
- **Burst Sub-Strategy** (50% share): Quick breakout trades with confidence scoring
- Intraday only: flat by 22:00 UK, nothing held overnight
- 15% daily loss cap with gradual tightening of entry thresholds
- Each sub-strategy has independent cash/budget/P&L accounting

### RISK Pot (20% - £20 per account)
- Speculative small/mid-cap crypto and niche stocks
- Two entries, each with its own confidence: a breakout, or a dip that has bounced in an uptrend (a coin still falling is skipped)
- Same sizing, 12% stop-loss, trailing take-profit (10% from peak after +15%), 7-day maximum hold, 30% drawdown cap, borrowing rules
- The buy stores confidence, components and expected value after costs; open and closed rows show those entry figures plus a live EV now (a dash if the buy predates scoring)
- Max 5 open positions

## Inter-Pot Lending

- **Allowed**: Temporary loans between pots WITHIN one account only
- **Prohibited**: Never crypto ↔ stocks account transfers
- **Purpose**: Exceptional setups when a pot is short of cash
- **Limit**: 20% of account start value maximum outstanding per account
- **Repayment**: Principal repaid when borrower's position closes; P&L stays with borrower

## Asset Preferences

### Crypto Focus
- **Heavy Emphasis**: Monad (MON), Worldcoin (WLD), XRP
- **Major Tokens**: BTC, ETH, SOL, and other popular cryptocurrencies
- **Smaller Tokens**: Down to ~$50M market cap, liquidity permitting
- **Preferred Chain**: Monad ecosystem tokens prioritized
- **Memecoin Policy**: ALL memecoins excluded EXCEPT DOGE and PEPE (explicitly allowed)
- **Gold-backed tokens**: PAXG and XAUT are Core crypto candidates (real-world-asset theme), ranked with the stocks, never a fixed share, and never traded by the Day pot

### Stock Focus
- Growth-oriented with thematic concentration:
  - **Photonics & Optical**: Laser, fiber optic, and optical computing companies
  - **Quantum Computing**: Emerging quantum hardware and software firms
  - **Robotics & Automation**: Industrial and service robotics
  - **Real-World Assets**: Gold miners, rare earths, farmland REITs
  - **Early-Stage Companies**: British (LSE/AIM) and global small caps
- **Geography**: Primarily UK (LSE/AIM), US, Australia (ASX)
- **Regulatory Compliance**: NO xStocks (tokenized stocks not available to UK residents)

## Trading Philosophy

### Core Principles (Owner's Explicit Requirements)

1. **Dynamic Over Static**
   - No fixed trades-per-day limits
   - Confidence and expected value drive decisions
   - Rules adapt to market conditions

2. **Capital-Based Caution (NOT Emotional)**
   - Gradual tightening as losses deplete capital
   - Rising confidence threshold as cash decreases
   - Smaller position sizes with less available capital
   - **Rationale**: Less money = less room for error (rational constraint, not fear)

3. **No 'Human Emotion' Behaviours**
   - NO cooldowns after individual losses
   - NO losing-streak-based halts
   - NO panic selling on drawdowns (just tighter thresholds)
   - Decisions based purely on setup quality and available capital

4. **Preference for Gradual vs Hard Stops**
   - Day pot daily loss cap at 15% (not 5% like early versions)
   - Confidence threshold rises linearly from 0.55 → 0.85 across the full 15% range
   - System becomes more selective, not shut down entirely
   - Preserves ability to catch recovery opportunities

5. **Live Data Priority**
   - Prices should be as fresh as possible
   - Stale data accounted for in confidence scoring, not blocked entirely
   - Fetch fresh quotes immediately before fills
   - Data age becomes a confidence factor, degrading stale signals

## Operations

### Execution Cadence
- **Primary** (since 2026-10-11): Hetzner VPS, systemd timer every 5 min (`*:02/5`). Fast cycles re-price from Kraken,
  Coinbase and KyberSwap, then trade and exit. The full collector (daily closes, backfills) runs every ~15 min. The
  CoinGecko top-300 screen runs about every 3 hours so the demo key stays under 10,000 calls a month.
- **GitHub Actions**: workflow kept as manual-only fallback (no schedule - avoids two writers)
- **Backup**: Owner's assistant computer runs a cycle if no `cycle` commit lands on main for 20 minutes

### Data Sources
- **Crypto Prices**: Kraken and Coinbase public markets when the coin is listed (GBP pairs converted with the stored FX rate), KyberSwap for Monad tokens, CoinGecko only for the rest and for the occasional top-300 screen
- **Stock Prices**: Yahoo Finance (US, LSE, ASX tickers)
- **FX Rates**: USD→GBP conversion (CoinGecko derived + open.er-api.com fallback)
- **Gas Estimates**: Monad RPC (eth_gasPrice)
- **Sentiment**: Owner's AI assistant (Grok Bot) scores ~3x daily from X/news analysis

### AI Workflows
- **Grok Bot** (Owner's AI Assistant):
  - Sentiment analysis 3x daily → JSON drops to `data/sentiment/`
  - Evening performance scorecards
  - Strategic reviews and decision support
  - Delegates all code changes to Cursor agents

- **Cursor Agents** (Code Development):
  - Implement feature requests from owner or Grok Bot
  - Must run tests (`test/sim.py`, `test/check_data.py`) before PR
  - Must ensure dashboard renders without JS errors

### Dashboard
- **Hosting**: GitHub Pages (static site in `docs/`)
- **URL**: https://oscarwy08.github.io/trading-dashboard/
- **Aesthetic**: X.com/Uniswap style (black/white/grey, blue accent #1D9BF0, green/red for gains/losses)
- **Philosophy**: Plain-English explanations, every number must reconcile (automated checks)
- **Update Frequency**: Client-side refetch every 60 seconds

### State Management
- **Source of Truth**: SQLite database (`data/trading.db`)
- **Repository**: GitHub (branch: `main`)
- **Commits**: Every cycle commits DB + dashboard to git
- **History Pruning**: DB vacuumed daily; git squashed to single commit after >100 commits (keeps repo size manageable)

## Security & Safety (Future Live Trading)

### Access Controls
- **Secrets**: GitHub encrypted secrets (or dedicated VPS ~£4-5/mo)
- **Repository**: Must be private before real money
- **API Keys**: Injected as environment variables, never hardcoded
- **Rate Limiting**: Respect free-tier limits (batch calls, no per-asset spam)

### Safety Mechanisms
- **KILL Switch**: Create `KILL` file in repo root → all trading halts immediately
- **RISK_RESUME**: After Risk pot 30% drawdown, manual review required (create `RISK_RESUME` file to resume)
- **Hard Limits**: 
  - Day pot: 15% daily loss cap (all new entries stop)
  - Risk pot: 30% drawdown cap (no new entries until reviewed)
  - Core pot: 20% stop-loss per position
- **Account Isolation**: Bot funds completely separate from owner's personal accounts

### Deployment Prerequisites
1. Demonstrated profitability in paper trading (minimum several weeks)
2. Repository made private
3. Dedicated Monad wallet created (NOT owner's personal wallet)
4. Dedicated Trading 212 account opened (NOT owner's personal ISA)
5. API keys secured in GitHub secrets or VPS environment
6. Safety limits tested and verified
7. KILL switch tested and confirmed operational

## Testing Requirements

### Before Any PR
1. **Simulation**: `python3 test/sim.py` must pass
2. **Reconciliation**: `python3 test/check_data.py` must pass
3. **Dashboard**: Must render without JavaScript errors
4. **Data Integrity**: All totals must equal sum of parts (automated checks)

### Specific Test Scenarios
- Loan mechanics (if touching lending code)
- Market hours handling (if touching stock trading)
- Universe filtering (if touching crypto/stock selection)
- Sub-strategy accounting (if touching Day pot)
- Price staleness handling (if touching data collection)

## Important Constraints

### Regulatory
- **UK Resident**: Must comply with UK retail trading regulations
- **No xStocks**: Tokenized stocks not available to UK residents
- **Broker Access**: Must use brokers accessible to UK retail (e.g., Trading 212, IBKR)

### Technical
- **Data Granularity**: 15-minute snapshots (not tick-by-tick)
- **Slippage**: Exits can overshoot stops due to 15-min cycles
- **GitHub Actions**: Unreliable timing, must handle delays gracefully
- **API Rate Limits**: Free tiers require batching and caching

### Operational
- **No Manual Trading**: All decisions automated (human review only for major changes)
- **Separate Accounts**: Bot funds never commingled with personal money
- **Timezone**: All times in UK (Europe/London) for consistency

## Development Guidelines

### For Cursor Agents
1. **Read First**: Always read PROJECT_BRIEF.md + README.md before starting
2. **Test Always**: Run tests before opening PR
3. **No Generated Docs**: Don't create .md files unless explicitly requested
4. **Preserve State**: Never commit changes to `data/trading.db` or `docs/` (generated files)
5. **Match Style**: Follow existing code patterns and dashboard aesthetic
6. **Reconciliation**: All numbers must add up (check_data.py enforces this)

### Code Quality
- Plain Python, no unnecessary dependencies
- Clear variable names and logic flow
- Comments only for non-obvious intent (not narration)
- Match existing patterns (don't introduce new paradigms)

### Dashboard Updates
- Black/white/grey base, blue accent (#1D9BF0)
- Plain-English labels and tooltips
- Numbers formatted with £ prefix and 2 decimal places
- Percentages with +/- signs and 1-2 decimal places
- Every figure must reconcile with its components

## Current Status (October 2026)

- **Version**: v7.8 (rules frozen 2026-10-10: honest EV and a 0.3% hurdle, liquidity-based crypto costs, no chase on a flat range / RSI 80 / a 7-day run, scorecard on the dashboard)
- **Start Date**: 2026-10-10 (great reset after initial development)
- **Balance**: £200 GBP (£100 crypto, £100 stocks)
- **Cycle Frequency**: Every 5 minutes on the VPS (exchange prices each cycle; daily history about every 15 min; CoinGecko top-300 about every 3 hours)
- **Data Quality**: Building 15-min price history; some assets still "warming up"
- **Mode**: PAPER TRADING (no real money)
- **Next Milestone**: Several weeks of profitable paper trading before considering live deployment
