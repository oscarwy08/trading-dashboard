# Instructions for AI Coding Agents

If you are an AI agent (from Cursor, Claude, GPT, or similar) working on this codebase:

## Before You Start

**REQUIRED READING** (in this order):

1. **[PROJECT_BRIEF.md](PROJECT_BRIEF.md)** - Owner's goals, trading philosophy, system architecture
2. **[README.md](README.md)** - Technical implementation details, version history, file layout

These two files contain everything you need to understand what this project does, why it does it that way, and how all the pieces fit together.

## Critical Constraints

### What NOT to Commit
- **NEVER commit changes to `data/trading.db`** - this is the live state database, modified only by automated cycles
- **NEVER commit changes to `docs/data.json`** or any generated files in `docs/` - these are built by `dashboard.py`
- **Code and tests only** - you may change `.py`, `.yaml`, `.js`, `.css`, `.html`, `.md`, and test files

### Before Opening a PR
1. Run `python3 test/sim.py` - must pass
2. Run `python3 test/check_data.py` - must pass  
3. Check dashboard renders without JavaScript errors
4. Extend tests if you added new features (e.g., add checks to `test/check_data.py` for new reconciliation requirements)

### Repository Lifecycle
- Automated cycles commit `data/trading.db` and `docs/` every 15 minutes
- Keep your PR mergeable: don't touch auto-generated files
- The system squashes git history periodically to manage repo size (don't rely on git history for audit)

## Common Tasks

### Adding a New Feature
1. Read PROJECT_BRIEF.md and README.md to understand the context
2. Find the relevant module (see "Layout" section in README.md)
3. Make your changes following existing patterns
4. Update or add tests
5. Run tests and verify dashboard
6. Create PR with clear description

### Modifying Trading Logic
- Core pot: `stockrank.py` (stock picking), relevant section of `trader.py`
- Day pot: `daypot.py` (helpers), `trader.py` (execution)
- Risk pot: `riskpot.py` (screening), `trader.py` (execution)
- **Always** update `test/sim.py` to cover your changes
- **Always** update reconciliation checks in `test/check_data.py` if you change accounting

### Updating the Dashboard
- Templates: `web/index.html`, `web/app.js`, `web/styles.css`
- Data export: `dashboard.py` (builds `docs/data.json`)
- Style: Black/white/grey, blue accent (#1D9BF0), X.com/Uniswap aesthetic
- **Every number must reconcile** - add checks to `test/check_data.py`

### Understanding Money Flow
- Two accounts (crypto/stocks), never mixed
- Three pots per account (core/day/risk)
- Day pot has two sub-strategies (momentum/burst)
- Loans allowed within one account, never across accounts
- See `common.py` for cash/position/transfer accounting

## Development Philosophy

The owner values:
- **Dynamic rules** over static limits (no arbitrary caps)
- **Gradual caution** over hard stops (confidence tightening, not halts)
- **Capital-based risk** over "emotional" behavior (less cash = tighter, not scared)
- **Live data** over stale (fetch fresh, or account for staleness)
- **Plain English** in the UI (every term explained, numbers add up)

When in doubt, read the comments in `config.yaml` - they explain the "why" behind many parameters.

## Getting Help

If the codebase is unclear:
1. Check the version history at the top of README.md
2. Look for similar patterns in existing code  
3. Read the module docstrings (top of each .py file)
4. Check `common.py` for shared definitions and helper functions

## Notes for Future Agents

This system will eventually trade real money. Your changes must be:
- **Correct**: Trades must use the right prices, accounts, and cash
- **Safe**: Respect stop-losses, position limits, and the KILL switch
- **Testable**: Covered by `test/sim.py` or other test scripts
- **Auditable**: Clear logic, meaningful variable names, reconcilable numbers

The owner reviews all PRs personally. Write code you'd be comfortable explaining.
