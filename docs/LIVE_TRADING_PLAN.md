# Live trading plan

Written 11 October 2026. This is a design. It does not change trading rules. `rules.frozen_since` stays `2026-10-10` until about 1 November. No wallet, API key, or account number belongs in this file. The dashboard is served from `docs/`, so this page is public for as long as the repository is public.

## Executive summary

The paper bot can stay as it is. Real money needs a separate step that takes an order the strategy has already decided and either sends it to a venue or, until we trust that step, only writes down what it would have sent.

Use three venues, matching the accounts that already exist. Crypto that is actually swappable on Monad (MON, wrapped BTC, wrapped ETH, and other tokens on the official Monad token list) goes out through KyberSwap from a new hot wallet that holds only bot funds. XRP, WLD, DOGE, PEPE, and the other coins that are not realistically swappable on Monad stay on Kraken, where the owner already has Kraken+. Stocks go to a new Interactive Brokers account at the UK entity, on IBKR Pro, holding only bot funds and not an ISA. Trading 212 is not used. Their published API Terms prohibit algorithmic trading, which is what this bot does.

The stock path is IB Gateway on the VPS, spoken to by the TWS API. Interactive Brokers say a headless session with no GUI is not supported, so the practical setup is Gateway plus a community login tool (IBC) and a virtual screen. Gateway is built to restart itself once a day and needs a full login again after each Sunday 01:00 US Eastern. Their UK site also says fractional shares are not available through the API, so a live stock order is a whole number of shares. At about £100 in the stock account, UK shares fail a simple fee test (a £1–£3 minimum each side, plus 0.5% stamp duty on a non-AIM UK purchase). Live stocks, when they start, are US shares large enough that the round-trip commission stays under 1.5% of the trade, which is about £40 at the published US tiered minimum. Smaller stock ideas stay on paper.

The hot wallet, the Kraken key, and the IBKR login live only on the Hetzner VPS, encrypted, never in git and never in GitHub Actions. The Kraken key can trade and cannot withdraw. The repository should be made private before any of those secrets exist. Making it private takes the public GitHub Pages site down. That is intended. A public page of positions and cash is the wrong place for a live book. The owner can still open the dashboard through a private login.

Live safety is a second set of limits, tighter than the strategy and unwilling to bend when the strategy would. One order cannot be large, one hour cannot place many orders, one day cannot lose more than a fixed number of pounds, and a token that is not on an allowlist of contract addresses cannot be swapped. A halt the owner can hit from a phone stops new orders. If the VPS goes quiet, an outside check sends an alert and does not start a second copy of the bot.

The path to real money is: build the venue adapters and test them on recordings, run a shadow mode that fetches real quotes and sends nothing, then trade about £20, then the rest of the £200. The strategy rules themselves wait until the November freeze lifts, and even then only if paper trading has been profitable for several weeks, which was already the rule in `PROJECT_BRIEF.md`.

## What already exists

The cycle runs on the Hetzner VPS (Ubuntu 24.04, user `bot`) every five minutes from `trading-cycle.timer`. `run_cycle.sh` takes a lock, syncs to `origin/main`, and runs `scripts/ci_cycle.sh`. Prices come from Kraken, Coinbase, and KyberSwap quotes. `trader.py` then writes paper fills into SQLite. A `KILL` file in the repo root makes `trader.main` return before any action, including exits. `RISK_RESUME` is the manual restart after the Risk pot’s drawdown halt. CoinGecko’s key, when set, is read from `~/.trading_env` (mode 600) and is not logged. GitHub Actions is manual only, so it is not a second writer. The owner’s other computer runs a cycle if no `cycle` commit has landed for 20 minutes.

That last fallback is right for paper and wrong for live. Two machines must not both be able to sign.

## 1. Architecture

### One brain, three brokers

Keep the strategy in the modules that already decide (`trader.py`, `daypot.py`, `riskpot.py`, `stockrank.py`). They keep producing the same decision they produce today: which asset, which side, what size, what reason. They do not learn how to talk to Kyber, Kraken, or Interactive Brokers.

A small broker layer, called only after that decision, does the sending. Paper and live share it.

```text
strategy decision
    -> risk gate (hard caps, allowlist, price check, kill switch)
    -> Broker.submit(order) -> venue
    -> Broker.reconcile() every cycle, before any new order
```

Four implementations of one interface:

| Adapter | When it is used |
| --- | --- |
| `PaperBroker` | Today’s behaviour. Writes the fill into SQLite at the decision price, plus the paper fee and gas. This is the default until a later change switches the config. |
| `KyberMonadBroker` | Monad swaps for tokens on the official token list. MON, wrapped BTC (`cbBTC` / `WBTC`), wrapped ETH (`WETH`). Not XRP, WLD, DOGE, PEPE, or the other coins that have no usable Monad pool. |
| `KrakenBroker` | Those other coins, on the owner’s Kraken+ account, trade-only API. |
| `IbkrBroker` | Stocks, through IB Gateway and the TWS API, on a dedicated IBKR Pro account at the UK entity. Not constructed until the paper-account tests in section 4 have passed. Not an ISA. |

The interface is intentionally small:

- `quote(order) -> Quote` with price, size the venue will accept, fee if the venue states it, and an expiry time.
- `submit(order) -> VenueOrder` once, with a client id that was saved before the call.
- `fetch(client_id) -> VenueOrder` for status, filled quantity, and average price.
- `cancel(client_id)`.
- `balances() -> {asset: amount}` and `open_orders()`.
- `reconcile(book) -> list of breaks`. A break stops new entries.

Nothing in the running paper path imports this layer until a later pull request wires `PaperBroker` in behind a config flag that defaults to the current direct fill. That wiring is not part of this document’s change.

### Where each coin goes

| Book | Venue | Why |
| --- | --- | --- |
| MON, BTC, ETH, other tokens on the official Monad list with a real pool | KyberSwap on Monad, chain id 143 | This is the execution venue already assumed for those assets. |
| XRP, WLD, DOGE, PEPE, and other Kraken-listed coins | Kraken spot | They are not swappable on Monad at a sane cost. FXRP exists as a Flare LayerZero token on Monad but has no Kyber market. Wormhole SOL on Monad round-trips at about 26% on £5. Neither is a live route. |
| Shares | Interactive Brokers (U.K.) Limited, IBKR Pro, not an ISA | Whole shares over the TWS API. US names first, because UK minimums do not fit a £100 stock account. See section 5. |

The crypto account and the stocks account stay separate, as they are now. Nothing in the bot bridges pounds from Kraken to the wallet or from Interactive Brokers to Kraken. The owner moves cash by hand. IBKR’s own auto currency conversion is the only FX the stock orders use. Do not send a separate spot-FX order. That product has a $2 minimum.

### Order state machine

Every live intent is a row written **before** the network call. The client id is the primary key.

`new` → `quoted` → `submitting` → `acked` → `partial` → `filled`

From `acked` or `partial` the order can also become `cancelled`, `expired`, or `rejected`. If the call returns and we do not know whether the venue accepted it, the state is `unknown`. `unknown` blocks every new order in that account until `fetch` or `open_orders` resolves it. The bot never “tries again to be sure” while the state is `unknown`.

Partial fills are normal on Kraken and on Interactive Brokers. The position quantity is the filled quantity, not the requested quantity. The unfilled remainder is left on the venue only if the order type allows it. A limit-IOC order (fill what you can up to a price, cancel the rest) is the default for Kraken so a thin book cannot walk the price. The leftover is not resubmitted in the same cycle. On IBKR, do not modify a resting order to chase the price. Their commission notes treat a modify as a cancel plus a new order, and the per-order minimum can then be charged twice. Cancel at the next cycle if the quote has expired, and send a new decision with a new order id.

### Idempotency

| Venue | Rule |
| --- | --- |
| Kraken | Send `cl_ord_id` (a UUID) on `AddOrder`. Kraken rejects a second open order with the same id. `cl_ord_id` and `userref` cannot both be set. `cl_ord_id` is unique among **open** orders, not among all historical orders, so do not reuse an id after the order closes. The nonce is a single increasing counter for that API key, stored in SQLite under the same lock as the cycle. A nonce window of a few seconds is set on the key so a slightly reordered request does not ban the key. |
| Interactive Brokers | The TWS API order id is chosen by the client. It must be unique for that client id and larger than every id already used. On connect, read `nextValidId`, and write the next id into SQLite **before** `placeOrder`. If the socket drops after the send, call `reqOpenOrders` and match on that order id (and on `permId` once the ack has one) before deciding the send failed. Never place a second order for the same decision row. |
| Kyber / Monad | The client id is our row id. The on-chain id is the transaction hash, saved when the node accepts the raw transaction. A resend uses the **same nonce and the same signed bytes** only if the node never accepted them. A new swap is a new quote, a new row, and a new nonce. |

### Reconciliation, every cycle, before new orders

The venue is right and the database is a cache.

- Kraken: balances, open orders, and the status of every row still in `submitting`, `acked`, `partial`, or `unknown`.
- Monad: token balances of the hot wallet, including native MON, read with `eth_call` / `eth_getBalance`. Compare with the sum of filled quantities. Dust below a configured threshold (start at the equivalent of £0.05) is logged, not traded.
- Interactive Brokers, once the adapter is on: positions and open orders from the Gateway. If the Gateway is down, send no stock order, alert, and let the crypto half of the cycle continue.
- Cash in the database is then set from the venue balances, converted to pounds at the stored FX rate. A break larger than dust halts new entries and sends an alert. Exits of positions the venue still shows can proceed. Inventing a fill the venue does not show cannot proceed.

### Kyber execution

The quote the collector already uses is only the first call.

1. `GET https://aggregator-api.kyberswap.com/monad/api/v1/routes` with `tokenIn`, `tokenOut`, and `amountIn`.
2. Immediately, in the same process, `POST .../monad/api/v1/route/build` with that `routeSummary` unchanged, plus `sender`, `recipient` (both the hot wallet), `slippageTolerance`, and `deadline`.
3. Sign the returned calldata to the returned `routerAddress` and send it.
4. Wait for a transaction receipt.

`slippageTolerance` is in basis points where 10 means 0.1%, and the allowed range is 0 to 2000. If it is omitted, Kyber sets it to 0, which will revert on any price move. Set it explicitly. The live cap is 50 (0.5%) and the config is not allowed to raise it above that. `deadline` is a unix time about 120 seconds ahead. If more than 20 seconds pass between the GET and the signed transaction, throw the quote away and wait for the next cycle.

The router address is pinned after the first successful build. If a later build returns a different router, the order is not sent and an alert fires. The token address must be on the allowlist taken from the official Monad token list. The transaction’s `to` must be the pinned router, never the token.

### Gas, nonce, confirmation, reverts

One process holds the wallet. The cycle already uses a file lock. The live signer refuses to start if it cannot take that lock.

- Read the pending nonce with `eth_getTransactionCount` at the start of the cycle. Trust the chain if it disagrees with the local counter, and alert.
- Cap gas in pounds. The paper config already assumes a small Monad fee. If the quoted gas is above £0.05, do not send. That number is a safety cap, not a forecast.
- Submit one swap at a time. Do not queue a second nonce until the first receipt is in.
- A receipt status of success fills the order at the on-chain amounts (tokens out, MON spent on gas). A revert marks the order `rejected`, does not change the position, and does not retry inside the cycle. The next cycle may quote again if the strategy still wants the trade.
- If the node accepted the transaction and no receipt arrives within the next few blocks, leave the row `unknown` and let Monad’s own forwarding do its resend. Monad forwards a transaction to the next 3 leaders and the originating node retries that a limited number of times. Do not invent a second transaction with a higher nonce. Replacing the same nonce with a higher fee is allowed only after that wait, and only up to one replacement, and only if gas is still under the £0.05 cap.

### MEV and sandwich risk on Monad

Monad has no global mempool. A transaction is forwarded to the next few leaders’ local mempools (currently the next 3). That is the protection the chain gives a normal user. It is not a private relay, and it is not a promise.

FastLane runs an on-chain auction so searchers can bid for ordering. It is not a private RPC for users to hide a swap. Do not integrate it for the bot. Do not shop for a “private mempool” product. The practical limits are the ones already above: small size, a 0.5% slippage cap, a short deadline, one transaction at a time, and a reference-price check before send.

### Reference price

Before send, compare the venue quote with a second price.

- Kraken orders: the limit price is within 0.5% of the same public ticker the strategy just used.
- Monad BTC, ETH, and MON: Kyber’s implied price is within 1% of the Kraken mid. If Kraken has no book, do not send.
- Any other Monad token: it is on the official token list, the Kyber route’s own price impact is inside the 0.5% cap, and the token is on the allowlist. There is no second exchange to lean on. Keep these sizes inside the per-order cap.

## 2. Security

### Keys

| Secret | Where it lives | What it can do |
| --- | --- | --- |
| Monad hot wallet | A new key, generated on the VPS. Encrypted with `age`. The passphrase is a systemd credential (`systemd-creds`), not a file in the repo and not a GitHub secret. The cycle decrypts it in memory and wipes it after signing. | Sign swaps and gas. It cannot reach the owner’s personal wallets because it is a different key. |
| Kraken API key | Same secret store. A second key, later, may be cancel-only and may live on the owner’s other computer. | Query funds, query orders and trades, create and modify orders, cancel orders. **No** Withdraw Funds. **No** Deposit Funds. **No** Add Withdrawal Addresses. IP whitelist is the VPS address only. Set an expiry (90 days) and a short nonce window. |
| IBKR login | Same secret store: username, password, and, only if the owner accepts open decision 11, the Mobile Authenticator TOTP seed. Gateway binds `127.0.0.1` only. | Place and cancel orders on the dedicated bot account through the local socket. Withdrawals stay a website action behind IBKR Mobile. Never the ISA. |
| CoinGecko demo key | Stays in `~/.trading_env`, mode 600, as now. | Market data only. |

`age` is the encryption tool. `sops` is the alternative if we later want a file that can be edited in place. Start with `age` plus systemd credentials. One secret, one recipient key, the private recipient key itself only in the systemd credential store.

The hot wallet’s on-chain balance is the most it can lose to a stolen key. Fund it with the crypto allocation only (about £100 at the full start, about £10 during the canary). Do not park spare pounds there. There is no contract limiting this key to a single destination, so a stolen key can send the balance anywhere. A weekly manual sweep of profits to a hardware wallet is a habit, not a technical guarantee.

GitHub Actions stays unable to sign. Do not add these secrets to the repository’s Actions secrets. The manual workflow remains a paper fallback only, and once live it should not run the trader against the live database at all.

### Make the repository private

The repository is public, and every cycle commits `data/trading.db` and `docs/data.json`. That is acceptable for paper. It is not acceptable once the database contains a live Kraken balance or a wallet address tied to real funds.

On GitHub Free, a private repository is available, and **GitHub Pages for that repository is unpublished automatically**. GitHub Pro (the pricing page on 11 October 2026 lists $4 per user per month for the first 12 months) turns Pages back on for a private repository, but the site is still **public on the internet** unless the account is an organisation on GitHub Enterprise Cloud with the site visibility set to private. Paying for Pro does not hide the dashboard. Enterprise Cloud is the wrong amount of machinery for a £200 book.

Do this instead:

1. Make `oscarwy08/trading-dashboard` private. Confirm the Pages site goes offline.
2. Stop publishing positions. The owner reads the dashboard on the VPS, bound to localhost, behind the HTTP basic auth the box already knows how to serve, reached through an SSH tunnel from the phone or a laptop. Tailscale or Cloudflare Access is the upgrade if a tunnel becomes annoying. Either one is a login. A public URL is not.
3. If a public scoreboard is still wanted, publish a second repository that contains only a redacted page (no cash, no wallet, no order ids, no average prices). Generate that page in the cycle and push it with a token that cannot read the private repo’s secrets. Do not point that token at the private repo with write access to code.

The VPS git remote must be switched to SSH or a fine-grained token that can push the private repo and cannot admin keys. The token sits in the same secret store.

### VPS hardening

Already in place, and keep it: a non-root user `bot`, a systemd timer, a file lock around the cycle, env file mode 600, logs rotated, CoinGecko key not printed.

Add before any live key exists:

- SSH keys only. Password login off. Root login off. `ufw` default deny incoming. SSH allowed only from the owner’s IP, or only over Tailscale, not from the whole internet.
- `unattended-upgrades` for security updates. Reboot once a month, not during the UK session if stocks are live.
- `fail2ban` or an equivalent ban on SSH failures, if SSH is still on a public port.
- The `bot` user is not in sudo. Anything that needs root is a separate admin login.
- systemd unit for the cycle: `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict` with an explicit read-write path for the repo and the log. The process can read the decrypted key only via the credential systemd passes in.
- No dashboard port on the public interface. `sshd` and, if used, Tailscale are the only listeners.
- `cycle.log` is checked once, on purpose, to prove a failed order does not print the key, the signed transaction’s private inputs, or the Kraken secret. The existing CoinGecko rule is the pattern.
- Full-disk encryption: Hetzner’s disk is not a customer-held key. The age passphrase in systemd credentials is the control that matters. Do not also keep a plaintext copy in `~/.trading_env`.
- Turn off the 20-minute box fallback that runs a full cycle. Replace it with the dead-man check in section 3, which alerts and does not trade.

### Rotation, logs, alerts, 2FA

- Kraken key: 90-day expiry, so rotation is forced. Create the new key, whitelist the same IP, switch the secret, place one read-only balance call, then delete the old key. Monad key: rotate only if the VPS is rebuilt or the key is suspected. Rotation means a new wallet and a manual sweep, which is a real on-chain transfer, done by hand, not by the strategy.
- Audit log: a table, also appended to a file outside the repo, with time, client id, venue, asset, side, requested size, filled size, price, fee, gas, and the decision id. No secrets. This file is the tax export as well as the incident log. It is copied off the VPS daily.
- Alerts, both email and Telegram, on: any `unknown` order, any reconciliation break, any revert, gas above the cap, daily loss past half the hard cap, the kill switch, and a missed cycle. Telegram is a bot in a private chat with the owner. The bot token is a secret. Commands it accepts are listed in section 3.
- 2FA on Kraken, Interactive Brokers, GitHub, Hetzner, the email account that receives alerts, and the Telegram account. Prefer an authenticator app or a hardware key. SMS is the weak option. The Kraken API key does not get a withdraw permission, so 2FA on the website is what stops a withdrawal even if the trade key leaks. IBKR already requires a second factor on the Gateway login. Storing the TOTP seed on the VPS makes that factor automatic and weaker. See section 5 and open decision 11.

## 3. Risk controls for live

These sit in front of the strategy. The strategy can want a trade and still be refused. They do not replace the Day pot’s 15% cap, the Risk pot’s 30% halt, or the Core stop. Those stay. The live caps are smaller and apply to the whole account.

Suggested figures for a £200 start. They are a proposal for the owner to accept, not a change to `config.yaml` today.

| Control | Canary, about £20 deployed | Full £200 |
| --- | --- | --- |
| Max notional per order | £8 | £20 |
| Max position in one asset | £10 | £40 |
| Max daily loss per account, after which new entries stop | £2 | £10 |
| Max new orders per hour, all venues | 4 | 6 |
| Max open orders | 2 | 8 |
| Slippage versus the reference price | 0.5% Kraken, 1% Monad majors | same |
| Gas | abort above £0.05 | same |
| Tradable set | Allowlist only | same |

The hourly cap matters because the timer fires twelve times an hour. A bug that re-sends a signal must hit this wall.

Allowlist:

- Monad: contract addresses copied from `tokenlist-mainnet.json` in `monad-crypto/token-list`, checked into the repo as data, not typed from memory. A swap whose token is not on that list is refused. Wrapped SOL and FXRP stay off the list until a later decision says the pool is deep enough. Today they are not.
- Kraken: a list of pairs (`XRP/USD`, `WLD/USD`, `XDG/USD`, `PEPE/USD`, and any other pair the owner adds). A pair absent from the list is refused even if the strategy names it.
- Stocks: the existing universe tickers, whole shares only, and only while the exchange is open, which the paper bot already enforces. A stock order is also refused when the expected round-trip cost (two commission minimums, or the per-share formula if that is larger, plus stamp duty, plus the 0.03% auto-FX each way) is more than 1.5% of the notional. At the rates in section 5 that floor is about £40 for a US share and about £200 for a UK share. The £8 and £20 crypto caps in the table above are not the stock cap. A US order has to be allowed to reach the fee floor, and it still cannot exceed the cash in that pot.

Price check: section 1. A quote that fails the check is not sent and is not retried until the next cycle.

### Kill switch from a phone

Two different actions, because today’s `KILL` file stops **everything**, including exits. For live that is the wrong single button.

- **Halt.** No new orders. Exits the strategy already wants still run. Open venue orders are cancelled. Trigger: Telegram `/halt`, or `touch` a `HALT` file over SSH. This is the button for “something looks wrong”.
- **Flatten.** Halt, then market-sell (or limit-IOC through the slippage cap) every live position. Trigger: Telegram `/flatten` plus a second confirmation message within two minutes, so a stray tap does not empty the account. This is the button for “get back to cash”.

Both reply in the same chat with the balances they just read, so the owner can see it worked. The Telegram bot can halt and flatten. It cannot change config, cannot print secrets, and cannot add a withdrawal address.

The website kill on Kraken (delete or disable the API key) still works if the VPS is the thing that has gone wrong. For stocks, IBKR Mobile can cancel open orders and log the session out without our code. Halt should cancel IBKR orders before it stops Gateway. A logout on its own leaves orders that are already at the exchange.

### Dead-man switch

If the VPS is down, a program on the VPS cannot help. The dead-man lives on the owner’s other computer, the one that already watches for a stale cycle.

- The VPS touches a heartbeat at the end of every successful cycle (a row in the database and a ping to a dead-man service, or a signed file the other computer pulls).
- If the heartbeat is older than 20 minutes during hours the bot is supposed to run, the other computer sends Telegram and email. It does **not** start `trader.py`. It does **not** hold the Monad key.
- Optional, and only with a second Kraken key that can cancel and cannot create orders: that computer calls cancel-all. It still cannot move funds.
- Positions that are already open stay open until the owner flattens them from the Kraken app or until the VPS comes back and the strategy’s exits run. That gap is acceptable at this size because the wallet cannot hold more than the capped balance. It is not acceptable to “solve” it by giving the second computer the hot wallet.

## 4. Testing

### Recorded fixtures

Each adapter has unit tests that post a recorded HTTP response and assert the state machine. No test hits the network. Cases to record once and keep:

- Kraken: accepted, partial fill, full fill, reject, `EAPI:Invalid nonce`, unknown id on a status poll, duplicate `cl_ord_id`.
- Kyber: route, build, receipt success, receipt revert, router address changed, quote older than 20 seconds.
- Interactive Brokers: a recorded `placeOrder` ack, a partial fill, a reject, and a socket drop after the send followed by `reqOpenOrders` already containing that order id. Recordings come from the paper account, not the live one.
- Reconciliation: venue balance differs from the database by more than dust, and the gate refuses a new order.

### Venues’ own test environments

| Venue | What exists |
| --- | --- |
| Monad | Testnet, chain id 10143, faucet, public RPCs including `https://testnet-rpc.monad.xyz`. Use it to test nonce, gas cap, receipt, and revert handling. Kyber may or may not route on testnet. If it does not, test the chain path against a tiny self-sent transfer and test the Kyber encode path with a recorded mainnet quote that is never broadcast. |
| Kraken spot | No self-service spot sandbox. A spot UAT environment exists **on request** through an account manager, with its own URL and keys. The futures demo at `demo-futures.kraken.com` is self-service and is the wrong product. This bot does not trade futures. Do not use the futures demo as a stand-in. The practical spot test is the shadow mode below, then the £20 canary. |
| Interactive Brokers | A paper trading account, created with the live account, with its own username. IBKR’s glossary says new clients receive one with $1,000,000 of simulated equity, and that the Web API and the TWS API work on it with minimal differences from live. IB Gateway’s default paper socket is port 4002. Live is 4001. Log in with the paper username. Do not point a paper client at the live port. Market-data sharing with the live login defaults to off, and only one session can use a shared subscription at a time. |

### Shadow mode

Config `broker: shadow`. The strategy runs on the real cycle. The broker fetches a real Kraken order book, a real Kyber route, and, for a stock, a quote from the IBKR paper Gateway. It runs the risk gate and writes the order it would have sent, including the price and the reason it would have been refused. It does not sign and does not place an order. A later step, still before any live share, places that same order on the IBKR paper account (port 4002) so the simulator, the order id, and the whole-share rounding get a real run. The dashboard gains a column for those rows so the owner can read, for a couple of weeks, “this is what live would have done”. Shadow mode is a success only if those rows match the paper fills on side, asset, and rough size, and if every refused row has a clear reason. A stock row that the fee floor refuses is a success, not a bug.

### Canary, then the rest

After shadow mode has been boring, deploy about £20. Suggested split: £10 in the hot wallet and £10 on Kraken. No stocks. One round trip on a liquid Kraken pair and one small Monad swap, then a week of the real strategy inside the canary caps. Reconcile every cycle. If any `unknown` order or any balance break appears, stop and fix it before adding money.

Only then fund the rest of the £200. The stock half stays on the internal paper book until the IBKR paper account has done the same round trip cleanly. The first live stock is one US whole share that clears the fee floor in section 5. UK shares stay on the internal paper book at this account size.

### CI and property tests

Add a pull-request workflow that runs the unit tests and `test/check_data.py`. It does not run the live cycle and it has no secrets. The existing manual `cycle.yml` stays manual.

Property tests, on a fake venue that randomly partial-fills and reorders replies:

- Cash does not go negative.
- Position size equals the sum of signed fills.
- Submitting the same client id twice does not create two fills.
- A revert does not change cash or position.
- After `reconcile`, the database balances equal the fake venue or the gate is shut.
- The sum of pound values, fees, and gas in the audit log equals the change in cash, within a penny.

`test/sim.py` keeps covering the strategy. The new tests cover the broker. A strategy change is still frozen until November. A broker change is not a strategy change, but it still has to pass both suites.

## 5. UK specifics

This is not tax advice. It is the record the bot should keep so an accountant, or the owner, can file. The owner is UK-based. The book is about £200. At that size, gains may fall inside the annual exempt amount, which for 2026 to 2027 is £3,000. Being inside the allowance does not remove the duty to keep records, and it does not remove the duty to report if HMRC’s reporting thresholds are met. Check the current GOV.UK page at filing time.

### Crypto

HMRC treats disposals of cryptoassets as capital gains for an individual in the usual case. A swap is a disposal of the token sold and an acquisition of the token bought. Gas paid in MON is a disposal of that MON as well as a transaction fee. Transaction fees are an allowable cost. Keep both numbers and let the filing work out the line.

Each token has its own pool (a section 104 pool). Adding to the pool adds the pounds spent, including fees. Selling removes a proportional slice of the pooled cost. Two exceptions, which the bot must not “simplify” away:

- Tokens bought the same day are matched to disposals that day first.
- Tokens bought in the next 30 days are matched to the earlier disposal (the bed-and-breakfast rule).

A day-trading bot will hit both rules constantly. The audit log is therefore one row per fill, in pounds, with the FX rate used, the timestamp in Europe/London, the token amount, the fee, the gas in MON and in pounds, the venue, and the transaction id or Kraken trade id. Export that log to CSV. Do not rely on git history of `main`. The cycle squashes history on purpose.

Kraken+ changes the commission. It does not remove the spread, and it does not remove the need to store the fee the fill actually charged. Read the fee off the fill. Do not hardcode a percentage from a marketing page.

### Stocks

The IBKR account is a dealing account, not an ISA. Gains are taxable. Share pooling uses the same section 104 idea, with the same 30-day rule. Stamp duty, the commission, and the FX adjustment belong on the row. IBKR’s Flex statement is the cross-check, not the only record. Flex can be pulled later without a trading session. It is not the order API.

### Why not Trading 212

Trading 212’s published API Terms prohibit algorithmic trading. They define it as a computer algorithm that determines whether to start an order, the timing, the price, or the quantity, with little or no human intervention. That is what this bot does. The same terms require written consent for automated mass data entry and for a custom interface. The Cyprus Invest Terms repeat the ban. The UK entity’s text might differ, and it does not matter for this plan: the stock broker is Interactive Brokers, whose API exists so a client program can place orders. The owner still accepts IBKR’s own API agreement at login. Paper stocks on this dashboard continue either way.

### Interactive Brokers, for this account

Checked 11 October 2026 against the UK site. The account is Interactive Brokers (U.K.) Limited, IBKR Pro. IBKR Lite is for US residents. The UK required-minimums page lists an account minimum of $0 and an inactivity fee of $0. The TWS API and the REST API over their internet gateway also show a $0 monthly minimum. FIX is a four-figure monthly minimum and is the wrong product.

**Which API.** Use IB Gateway and the TWS socket API. From the client’s point of view Gateway and Trader Workstation are the same server. Gateway is the lighter one. IBKR’s own page says a headless session with no GUI is not supported, and that both programs are meant to be restarted daily. From version 974 an auto-restart can keep the session from Sunday to Sunday without typing the password again. The auto-restart token is thrown away each Sunday at 01:00 US Eastern, and also if the process is killed mid-week, so the first start of the week (and any start after a VPS reboot) needs a full second factor.

IBC is the community tool that drives that login, answers the dialogs, and restarts Gateway. It is not an IBKR product. A push notification in IBKR Mobile still needs a thumb. An unattended weekly login means storing the Mobile Authenticator TOTP seed on the VPS and letting IBC type the code. That puts the second factor next to the password. Open decision 11. The alternative is a phone tap once a week and after every reboot.

Gateway stays up. The five-minute cycle connects, reconciles, and disconnects. It does not restart Gateway. Default sockets are 4001 for live and 4002 for paper. Bind them to localhost. The “Read-Only API” box is on by default and blocks every order until it is turned off. Precaution pop-ups in Gateway will stall a bot that cannot click them. IBC has to be configured to dismiss the ones that are safe to dismiss, and to stop if a dialog is unexpected.

The Client Portal Web API is a local Java gateway and REST calls. IBKR are folding it, Digital Account Management, and Flex into one Web API with OAuth 2.0. The old endpoints stay. IBKR’s own lesson says this API is younger than the TWS API and may not have the same coverage. Keeping a brokerage session alive on it is a second daily chore. Use Flex later for the tax CSV. Do not use Client Portal as the order path.

**Fractions.** The UK fractional-trading page says fractional shares are on every IBKR platform except the IBKR API, and that whole shares are on every platform. UK accounts can hold fractions except in an ISA or a Junior ISA. That exception does not help, because the API column is the one this bot uses. A May 2022 IBKR UK note on European fractions also limits them to selected exchanges, average daily volume above $5 million, and market cap above $5 billion, which is not the AIM small-cap book. The adapter sizes in whole shares. Whether a cash-quantity order sneaks a fraction through is something to try once on the paper account (open decision 14), not something to build the £100 book on.

**Commissions, IBKR Pro, first volume tier.** US shares, tiered: $0.0035 per share, minimum $0.35 per order, maximum 1% of trade value, with exchange, clearing, and regulatory fees passed through. Fixed: $0.005 per share, minimum $1.00, maximum 1% of trade value. Directed API orders cannot use tiered pricing. Smart-routed API orders can. Use SMART and tiered. UK GBP shares, tiered: 0.05% of trade value, minimum £1.00 per order. Fixed with SmartRouting: 0.05%, minimum £3.00. Fixed direct: 0.10%, minimum £4.00. IBKR’s page also says Western European trades are £3 for a typical size. That matches the fixed SmartRouting minimum, not a second charge on top of tiered. Code against the table.

**Stamp and the PTM levy.** Electronic UK share purchases pay Stamp Duty Reserve Tax at 0.5%. IBKR say UK stamp duty is applied to UK share purchases. HMRC exempt securities that are admitted to trading on a recognised growth market and are not listed on any market. AIM is the usual case. A line that is also listed on a recognised market does not get that exemption. Read the flag off the fill rather than guessing from the ticker. The PTM levy is £1.50 on a buy and on a sell when the consideration is over £10,000. It will not bite this book. It is still a column on the audit row so a later, larger trade is not a surprise.

**FX.** For an automatic currency conversion IBKR typically add or subtract 0.03% to the rate and do not charge a separate commission. That is the path for a GBP account buying a US share. A manual spot-FX order is 0.20 basis points with a $2 minimum at the first tier. Two dollars on a small conversion is an absurd fee. Do not send one.

**Market data.** The strategy can keep using the Yahoo prices the collector already stores. A live order still wants a broker quote as the reference check, and that quote is a subscription.

- US Real-Time Non Consolidated streaming quotes are free. They are not the consolidated national best bid and offer.
- The US Securities Snapshot and Futures Value Bundle is $10 a month for a non-professional, waived if the account generates more than $5 of commissions that month, plus $0.01 per snapshot. A five-minute poll of the whole watchlist will run up the snapshot meter, and IBKR then switch that exchange to a streaming subscription for the rest of the month. One snapshot of the symbol about to be traded is the shape that stays cheap. A $10 month that is not waived is about 1% of a £100 stock account.
- LSE International Level 1 is £1 a month for a non-professional, and LSE UK Level 1 is another £1. The “Pro” column on that page (£31 and £56) is the exchange’s professional-user status. It is not the IBKR Pro commission plan.
- ASX, if a live Australian name is ever sent, is a separate subscription and was not priced for this note.

**How big a stock order has to be.** The paper bot’s £5 minimum is a paper rule. It is a bad live stock order. Round trip, tiered, one fill each side, no modify:

| Market | Floor used | Smallest notional whose costs stay under 1.5% |
| --- | --- | --- |
| US share, tiered | $0.35 × 2 ≈ £0.53, plus about 0.06% auto-FX | about £40 |
| AIM, tiered, stamp exempt | £1 × 2 | about £135 for commission alone. Use £200 so the round trip is about 1%. |
| UK main market, tiered | £2 commission plus 0.5% stamp on the buy | about £200 (that is £2 + £1 stamp = 1.5%) |

A £100 stock account is split £50 Core, £30 Day, £20 Risk. One US order around £40 fits in Core. It does not fit in Risk. A £200 UK or AIM order does not fit anywhere. Live UK and AIM names stay on the internal paper book until the stock account is larger. Day and Risk stock trades at £5 to £8 stay on paper too. The 1.5% test is a live gate. It is not a change to the frozen paper rules.

### Kraken

Kraken’s own developer documentation treats algorithmic spot trading as a normal use of the API and lists the permissions for it. The owner should still read the UK customer terms attached to the account before the canary. The operational limits we will rely on are the ones in section 2: trade-only key, IP whitelist, nonce, no withdraw permission.

Kraken spot has no self-service sandbox. See section 4.

## 6. Rollout

Observation windows below are calendar time because a market has to actually move. Build sizes are the work, not a promise.

### Phase 0. Decisions

Owner only. No code.

- [ ] Confirm paper trading is still the only thing running.
- [ ] Open the Interactive Brokers (U.K.) account as IBKR Pro, not an ISA, and wait for the paper username. Do not install Gateway yet.
- [ ] Decide the canary amount (£20 is the proposal) and the hard caps in section 3, including the stock fee floor.
- [ ] Decide that XRP, WLD, DOGE, and PEPE are Kraken orders, not Monad swaps.
- [ ] Create the Kraken+ checklist: 2FA, a new trade-only key later, not now.
- [ ] Decide whether the VPS may store the IBKR TOTP seed (open decision 11).

Effort: an evening, plus the time IBKR take to open the account.

### Phase 1. Lock the doors

Before any key is created.

- [ ] Private repository. Confirm Pages is offline.
- [ ] Dashboard only via SSH or Tailscale, with a login.
- [ ] Turn off the box fallback that runs a trading cycle.
- [ ] VPS checklist in section 2.
- [ ] Secret store (`age` + systemd credentials) empty and tested with a dummy value.
- [ ] Alerts reach the phone (Telegram and email), tested with a fake message.
- [ ] GitHub Actions has no trading secrets.

Effort: about a day of server work.

### Phase 2. Adapters and shadow mode

After the November rule freeze, or earlier if the owner is content that this pull request does not touch strategy thresholds. The code change is the broker interface, `PaperBroker` as the default, and `shadow` as a flag. Strategy numbers stay put.

- [ ] Interface and state machine.
- [ ] Fixture tests.
- [ ] Shadow mode writing “would have sent” rows.
- [ ] Reconciliation against a fake venue.
- [ ] Property tests.
- [ ] Pull-request CI running those tests.

Effort: the largest code change in this plan. One review cycle is not enough. Shadow mode then runs for two weeks of real cycles.

### Phase 3. Monad testnet and Kraken read-only

- [ ] Hot wallet created on testnet. Faucet MON. Send a transfer. Observe nonce, receipt, and a deliberate revert.
- [ ] Kraken key created with the live restrictions, used only to read balances. No order yet.
- [ ] A recorded mainnet Kyber build is decoded in a test and checked: router pinned, token on the allowlist, slippage set, deadline set. It is not broadcast.

Effort: a day, after phase 2 exists.

### Phase 4. Canary

- [ ] £10 on Kraken, £10 of MON or USDC in the hot wallet.
- [ ] One round trip on each, by hand-triggered orders first, then the strategy inside the canary caps.
- [ ] A week with no `unknown` orders and no reconciliation breaks.
- [ ] Tax CSV opened and checked against the Kraken statement for those fills.

Effort: a week of watching, and a small amount of code if the first real fill disagrees with the database.

### Phase 5. The rest of the £200

- [ ] Fund up to the planned split, still with the full-book caps.
- [ ] Stocks still on the internal paper book. IBKR paper (port 4002) may run in parallel. No live share yet.
- [ ] Dead-man and both phone commands tested once on purpose, with a tiny position.
- [ ] First weekly backup of the audit log stored somewhere that is not the VPS.

### Phase 6. IBKR, paper first, then one US share

- [ ] Gateway and IBC on the VPS, paper username, port 4002 bound to localhost, Read-Only API off.
- [ ] One paper round trip in a whole US share. Order id stored before the send. Disconnect after the send and recover the order from `reqOpenOrders`.
- [ ] Live login only after that. Fund the stock half. First live order is one US whole share that clears the £40 fee floor. Cancel it from IBKR Mobile once, on purpose, to prove the phone path.
- [ ] UK and AIM names stay on the internal paper book.

Do not send a £5 UK share to find out the minimum. The rate card already says what it costs.

## Open decisions for the owner

1. Stock broker. This plan uses Interactive Brokers (U.K.) Limited, IBKR Pro, not an ISA, because Trading 212’s API terms ban algorithmic trading. Confirm that entity and that the new account holds only bot funds.
2. Are the hard caps in section 3 the right pounds? In particular the £10 full-book daily loss halt, which is tighter than the strategy’s 15% Day cap, and the stock rule that a live order must keep round-trip costs under 1.5%.
3. Public scoreboard: none, or a redacted second site?
4. Phone path: Telegram plus SSH, or Tailscale only?
5. Should the other computer hold a Kraken cancel-only key, or only send an alert?
6. Kraken+ fee. Confirm in the account which pairs are zero-commission and up to what volume. The bot will record the fill’s actual fee either way.
7. Monad token allowlist. Start with MON, cbBTC or WBTC, and WETH only, and add other official-list tokens one at a time?
8. When to rotate the hot wallet. Proposal: only on a VPS rebuild or a suspected leak, because rotation is a real transfer.
9. Whether shadow mode may start before 1 November. It does not change a threshold, but it is still a code change next to a frozen strategy. Default in this plan: wait, unless the owner says otherwise.
10. Tax. Use an accountant for the first return, or file from the CSV? The bot’s job is the CSV either way.
11. IBKR second factor. Store the Mobile Authenticator TOTP seed on the VPS so the Sunday login and any reboot are unattended, or tap the phone once a week and after every reboot? Storing it makes 2FA automatic and weaker. The plan prefers the phone tap unless the owner wants the bot to survive a Sunday with nobody around.
12. Market data. Pay $10 a month for the US snapshot bundle (waived only if commissions exceed $5 that month) and £1 plus £1 for LSE Level 1, or trade US names off the free non-consolidated US stream plus the Yahoo price the collector already stores, and leave LSE on the internal paper book?
13. Live stock universe. Proposal: US whole shares at or above about £40, and no live UK or AIM order until the stock account can hold about £200 in one name. Agree, or fund the stock account up so UK names clear the fee floor?
14. Try one cash-quantity order on the IBKR paper account to see whether a fraction is accepted. The published table says fractions are not available via the API. Until that test succeeds, the adapter sends whole shares only.

## Sources

Checked 11 October 2026.

KyberSwap

- EVM swap API, Monad as chain id 143, path `monad`: https://docs.kyberswap.com/developer-guide/aggregator-api/aggregator-api-specification/evm-swaps
- `slippageTolerance` in bps, 10 = 0.1%, range 0 to 2000, omitted means 0; `POST /{chain}/api/v1/route/build`: same page, and the how-to https://docs.kyberswap.com/developer-guide/aggregator-api/how-to-guides/execute-a-swap-with-the-aggregator-api

Monad

- No global mempool. Transactions go to the next 3 leaders. The originating node retries a limited number of times: https://docs.monad.xyz/monad-arch/consensus/local-mempool
- Testnet chain id 10143 and public RPCs: https://docs.monad.xyz/developer-essentials/testnets
- Official bridged-token list, including which bridge: https://docs.monad.xyz/developer-essentials/network-information/tokens-and-bridges
- Machine-readable list: https://github.com/monad-crypto/token-list
- FastLane is a searcher auction, not a private user RPC: https://www.fastlane.xyz/blog/fastlane-mev-protocol

Kraken

- Permissions for algorithmic spot trading, and the instruction to whitelist IPs and withhold `Withdraw Funds`: https://docs.kraken.com/exchange/guides/rest/api-keys
- Creating a key, nonce window, IP whitelist, expiry: https://support.kraken.com/gb/articles/360000919966-how-to-create-an-api-key
- Nonce: https://docs.kraken.com/exchange/guides/rest/authentication
- `AddOrder` and `cl_ord_id`: https://docs.kraken.com/api-reference/trading/add-order
- `cl_ord_id` uniqueness is among open orders: https://docs.kraken.com/exchange/guides/general/clordid
- Spot UAT is on request. Futures demo is self-service and separate: https://docs.kraken.com/home/guides/quickstart and https://docs.kraken.com/exchange/guides/overview
- Futures demo base URL: https://docs.kraken.com/exchange/guides/futures/introduction

Interactive Brokers

- IB Gateway is the same socket server as TWS from the client’s point of view. A headless session with no GUI is not supported. Daily restart, and auto-restart from version 974: https://www.interactivebrokers.com/docs/tws-api/doc/architecture/the-trader-workstation/the-ib-gateway.md
- API settings, Read-Only API, Gateway ports 4001 live and 4002 paper: https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/
- Auto-restart token invalidated Sunday 01:00 ET: https://www.ibkrguides.com/traderworkstation/auto-restart-considerations.htm
- IBC user guide (community tool, daily restart without re-typing the password, 2FA prompts): https://github.com/IbcAlpha/IBC/blob/master/userguide.md
- Client Portal / Web API, and the note that it may not match TWS feature for feature: https://www.interactivebrokers.com/campus/trading-lessons/what-is-ibkrs-client-portal-api/ and https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/
- `placeOrder` and the client-side order id: https://interactivebrokers.github.io/tws-api/order_submission.html
- Paper trading account, $1,000,000 simulated equity, same Web API and TWS API: https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/
- Fractional shares, UK, “except via IBKR API”: https://www.interactivebrokers.co.uk/en/trading/fractional-trading.php
- European fractional eligibility (volume and market-cap filter), May 2022: https://www.interactivebrokers.co.uk/en/general/about/mediaRelations/6-1-22-frac-shares.php
- UK stock commissions, US tiered/fixed minimums, UK £1 / £3 / £4 minimums, stamp duty line, directed API orders cannot use tiered: https://www.interactivebrokers.co.uk/en/pricing/commissions-stocks.php
- Spot FX commission and the 0.03% auto-conversion adjustment: https://www.interactivebrokers.co.uk/en/pricing/commissions-spot-currencies.php
- Account minimum $0, inactivity fee $0, API monthly minimum $0: https://www.interactivebrokers.co.uk/en/accounts/required-minimums.php
- Market data. Free US non-consolidated stream, $10 US snapshot bundle, LSE Level 1 at £1: https://www.interactivebrokers.co.uk/en/pricing/market-data-pricing.php
- PTM levy £1.50 above £10,000: https://www.interactivebrokers.co.uk/en/includes/disclosures/eu-stk-ptm-levy.php

Trading 212, kept because it is why this plan does not use them

- API Terms, including the algorithmic-trading prohibition: https://www.trading212.com/legal-documentation/API-Terms_EN.pdf
- Invest Terms (Cyprus entity), same prohibition: https://www.trading212.com/legal-documentation/cy/invest/Invest-Terms_EN.pdf

GitHub

- A private repository on Free unpublishes Pages: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility
- Pro adds Pages for private repositories. A privately published Pages site needs an organisation on Enterprise Cloud: https://docs.github.com/en/get-started/learning-about-github/githubs-plans
- Price page (Pro listed at $4 per user per month for the first 12 months, with an asterisk): https://github.com/pricing

UK tax

- Electronic share purchases, SDRT at 0.5%: https://www.gov.uk/tax-buy-shares/buy-shares-electronically
- Growth-market (AIM) SDRT exemption: https://www.gov.uk/hmrc-internal-manuals/stamp-taxes-shares-manual/stsm041270
- When a crypto disposal is taxable, pooling, same-day and 30-day matching, fees as allowable costs: https://www.gov.uk/guidance/check-if-you-need-to-pay-tax-when-you-sell-cryptoassets
- Section 104 pooling: https://www.gov.uk/hmrc-internal-manuals/cryptoassets-manual/crypto22200
- Annual exempt amount £3,000 for 2026 to 2027, and the rates from 6 April 2026: https://www.gov.uk/guidance/capital-gains-tax-rates-and-allowances

Secrets

- `age`: https://github.com/FiloSottile/age
- `sops`: https://github.com/getsops/sops
- systemd credentials: https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html
