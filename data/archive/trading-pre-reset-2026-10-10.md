# Archive of the paper-trading run before the great reset (2026-10-10)

- Database: `data/archive/trading-pre-reset-2026-10-10.db` (3.3 MB, full trade/equity/signal history; open with sqlite3)
- Dashboard data at the reset: `data/archive/dashboard-pre-reset-2026-10-10.json`
- Period: 2026-10-09T20:22:32+00:00 -> 2026-10-10T13:45:11+00:00 (UTC equity snapshots)

**Final equity £199.94** vs £200 start = **£-0.06 (-0.03%)**; vs BTC buy&hold +0.45%; max drawdown -0.41%
Trades: 28 order rows (BUY 19, CONVERT 6, SELL 3); closed 3, realised £-0.19; fees+gas £0.60

| Pot / account | Start £ | Final £ | P&L £ | P&L % | Trades | Realised £ |
|---|---|---|---|---|---|---|
| CORE pot | 100.00 | 100.45 | +0.45 | +0.45% | 16 | -0.07 |
| DAY pot | 60.00 | 59.61 | -0.39 | -0.65% | 4 | -0.12 |
| RISK pot | 40.00 | 39.88 | -0.12 | -0.31% | 2 | +0.00 |
| Crypto account | 100.00 | 100.25 | +0.25 | +0.25% | 10 | +0.00 |
| Stocks account | 100.00 | 99.69 | -0.31 | -0.31% | 12 | -0.19 |
| CORE x crypto | 40.00 | 40.64 | +0.64 | +1.60% | 6 | +0.00 |
| CORE x stocks | 60.00 | 59.81 | -0.19 | -0.32% | 10 | -0.07 |
| DAY x crypto | 40.00 | 39.73 | -0.27 | -0.68% | 2 | +0.00 |
| DAY x stocks | 20.00 | 19.88 | -0.12 | -0.60% | 2 | -0.12 |
| RISK x crypto | 20.00 | 19.88 | -0.12 | -0.62% | 2 | +0.00 |
| RISK x stocks | 20.00 | 20.00 | +0.00 | +0.00% | 0 | +0.00 |

Open positions at the reset (cost basis £; discarded by the reset, never sold):

- CORE AMD (stock): qty 0.0173529, cost £7.99
- CORE ANET (stock): qty 0.0486871, cost £7.99
- CORE BTC (crypto): qty 8.01531e-05, cost £5.00
- CORE ETH (crypto): qty 0.00266388, cost £5.00
- CORE MON (crypto): qty 543.071, cost £10.00
- CORE MSFT (stock): qty 0.0196946, cost £7.99
- CORE PLTR (stock): qty 0.0506128, cost £7.99
- CORE TEAM (stock): qty 0.0510848, cost £7.99
- CORE WLD (crypto): qty 13.2292, cost £5.00
- CORE XRP (crypto): qty 4.74835, cost £5.00
- CORE ZS (stock): qty 0.045044, cost £7.99
- DAY ADA (crypto): qty 103.512, cost £20.00
- DAY WLD (crypto): qty 48.1902, cost £20.00
- RISK DRV (crypto): qty 19.1189, cost £8.00
- RISK PLUME (crypto): qty 558.669, cost £8.00

Pending v6 settlement dropped by the reset: crypto DAY->CORE £10.00; stocks CORE->DAY £10.00

Scorecard at the reset (daily_summary.py on the archived DB):

```
PAPER scorecard Sat 10 Oct 14:52 UK
TOTAL Equity £199.94 (-0.03% since £200 start) | Day P&L £-0.01
vs BTC buy&hold (£) +0.45% | Max DD -0.41% | Win rate 0.00% over 3 closed
CRYPTO acct (Monad wallet) £100.25 (+0.25% vs £100) | P&L £+0.25 | day £+0.14 | cash £14.00 | invested £86.25 | realised £+0.00 | fees £0.26 (fee £0.257, gas £0.002) | by pot: CORE £40.64 (cash £10.00), DAY £39.73 (cash £0.00), RISK £19.88 (cash £4.00)
STOCKS acct (Trading 212) £99.69 (-0.31% vs £100) | P&L £-0.31 | day £-0.15 | cash £51.87 | invested £47.82 | realised £-0.19 | fees £0.34 (xStock fee+gas £0.339) | by pot: CORE £59.81 (cash £11.99), DAY £19.88 (cash £19.88), RISK £20.00 (cash £20.00)
Account split: pending settlement inside each account (when the cash is free): crypto DAY->CORE £10.00; stocks CORE->DAY £10.00
CORE £100.45 (+0.45% vs £100) | day £+0.50 | realised £-0.07 | win 0.00% of 2 | today 7 trades (1 buys, 0 sells) | built 10/11 | open: AMD, ANET, BTC, ETH, MON, MSFT, PLTR, TEAM, WLD, XRP, ZS | crypto £40.64 (cash £10.00) / stocks £59.81 (cash £11.99)
DAY  £59.61 (-0.65% vs £60) | day £-0.39 | realised £-0.12 | win 0.00% of 1 | today 4 trades (3 buys, 1 sells), best CRCLx £-0.12, worst CRCLx £-0.12 | entries open | open: ADA, WLD | crypto £39.73 (cash £0.00) / stocks £19.88 (cash £19.88)
RISK £39.88 (-0.31% vs £40) | day £-0.12 | realised £+0.00 | win n/a of 0 | today 2 trades (2 buys, 0 sells) | active, dd -0.3%, 2 new today | open: DRV, PLUME | crypto £19.88 (cash £4.00) / stocks £20.00 (cash £20.00)
  DAY momentum all: 1 trades, win 0.0%, avg win n/a, avg loss -0.60%, PF 0.0, P&L £-0.12 after £0.24 fees, expectancy -0.60%/trade | today: 1 trades, P&L £-0.12
  DAY burst    all: 0 trades, win n/a%, avg win n/a, avg loss n/a, PF n/a, P&L £+0.00 after £0.00 fees, expectancy n/a/trade | today: 0 trades, P&L £+0.00
  DAY burst threshold 0.563 (Day pot today -0.65%), 0/1 setups taken last cycle
GBP/USD 1.3241 (coingecko, 2026-10-10T13:30 UTC)
```
