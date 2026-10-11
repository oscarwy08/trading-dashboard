# Live trading plan

Written 11 October 2026. This is a design. It does not change trading rules. `rules.frozen_since` stays `2026-10-10` until about 1 November. No wallet, API key, or account number belongs in this file. The dashboard is served from `docs/`, so this page is public for as long as the repository is public.

## Executive summary

The paper bot can stay as it is. Real money needs a separate step that takes an order the strategy has already decided and either sends it to a venue or, until we trust that step, only writes down what it would have sent.

Use three venues, matching the accounts that already exist. Crypto that is actually swappable on Monad (MON, wrapped BTC, wrapped ETH, and other tokens on the official Monad token list) goes out through KyberSwap from a new hot wallet that holds only bot funds. XRP, WLD, DOGE, PEPE, and the other coins that are not realistically swappable on Monad stay on Kraken, where the owner already has Kraken+. Stocks stay on a new Trading 212 Invest account, not the ISA. There is a hard stop in front of that last one: Trading 212’s published API Terms prohibit algorithmic trading, and they define that as a computer choosing whether to order, when, at what price, or for what size. Do not send a live stock order until Trading 212 has said yes in writing. Until then the stock book stays on paper and the owner can place those trades by hand from the dashboard.

The hot wallet, the Kraken key, and any Trading 212 key live only on the Hetzner VPS, encrypted, never in git and never in GitHub Actions. The Kraken key can trade and cannot withdraw. The repository should be made private before any of those keys exist. Making it private takes the public GitHub Pages site down. That is intended. A public page of positions and cash is the wrong place for a live book. The owner can still open the dashboard through a private login.

Live safety is a second set of limits, tighter than the strategy and unwilling to bend when the strategy would. One order cannot be large, one hour cannot place many orders, one day cannot lose more than a fixed number of pounds, and a token that is not on an allowlist of contract addresses cannot be swapped. A halt the owner can hit from a phone stops new orders. If the VPS goes quiet, an outside check sends an alert and does not start a second copy of the bot.

The path to real money is: build the venue adapters and test them on recordings, run a shadow mode that fetches real quotes and sends nothing, then trade about £20, then the rest of the £200. The strategy rules themselves wait until the November freeze lifts, and even then only if paper trading has been profitable for several weeks, which was already the rule in `PROJECT_BRIEF.md`.

## What already exists

The cycle runs on the Hetzner VPS (Ubuntu 24.04, user `bot`) every five minutes from `trading-cycle.timer`. `run_cycle.sh` takes a lock, syncs to `origin/main`, and runs `scripts/ci_cycle.sh`. Prices come from Kraken, Coinbase, and KyberSwap quotes. `trader.py` then writes paper fills into SQLite. A `KILL` file in the repo root makes `trader.main` return before any action, including exits. `RISK_RESUME` is the manual restart after the Risk pot’s drawdown halt. CoinGecko’s key, when set, is read from `~/.trading_env` (mode 600) and is not logged. GitHub Actions is manual only, so it is not a second writer. The owner’s other computer runs a cycle if no `cycle` commit has landed for 20 minutes.

That last fallback is right for paper and wrong for live. Two machines must not both be able to sign.

## 1. Architecture

### One brain, three brokers

Keep the strategy in the modules that already decide (`trader.py`, `daypot.py`, `riskpot.py`, `stockrank.py`). They keep producing the same decision they produce today: which asset, which side, what size, what reason. They do not learn how to talk to Kyber, Kraken, or Trading 212.

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
| `T212Broker` | Stocks, only after written permission. Until then this adapter is not constructed. |

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
| Shares | Trading 212 Invest, separate from the ISA | Only with written permission. See section 5. |

The crypto account and the stocks account stay separate, as they are now. Nothing in the bot bridges pounds from Kraken to the wallet or from Trading 212 to Kraken. The owner moves cash by hand.

### Order state machine

Every live intent is a row written **before** the network call. The client id is the primary key.

`new` → `quoted` → `submitting` → `acked` → `partial` → `filled`

From `acked` or `partial` the order can also become `cancelled`, `expired`, or `rejected`. If the call returns and we do not know whether the venue accepted it, the state is `unknown`. `unknown` blocks every new order in that account until `fetch` or `open_orders` resolves it. The bot never “tries again to be sure” while the state is `unknown`.

Partial fills are normal on Kraken and possible on Trading 212. The position quantity is the filled quantity, not the requested quantity. The unfilled remainder is left on the venue only if the order type allows it. A limit-IOC order (fill what you can up to a price, cancel the rest) is the default for Kraken so a thin book cannot walk the price. The leftover is not resubmitted in the same cycle.

### Idempotency

| Venue | Rule |
| --- | --- |
| Kraken | Send `cl_ord_id` (a UUID) on `AddOrder`. Kraken rejects a second open order with the same id. `cl_ord_id` and `userref` cannot both be set. `cl_ord_id` is unique among **open** orders, not among all historical orders, so do not reuse an id after the order closes. The nonce is a single increasing counter for that API key, stored in SQLite under the same lock as the cycle. A nonce window of a few seconds is set on the key so a slightly reordered request does not ban the key. |
| Trading 212 | The market-order endpoint is documented as **not idempotent**. A retry can double-fill. Never retry a POST. If the response is lost, list open and recent orders and match on ticker, side, and quantity before deciding the call failed. Prefer a limit order once permission exists. |
| Kyber / Monad | The client id is our row id. The on-chain id is the transaction hash, saved when the node accepts the raw transaction. A resend uses the **same nonce and the same signed bytes** only if the node never accepted them. A new swap is a new quote, a new row, and a new nonce. |

### Reconciliation, every cycle, before new orders

The venue is right and the database is a cache.

- Kraken: balances, open orders, and the status of every row still in `submitting`, `acked`, `partial`, or `unknown`.
- Monad: token balances of the hot wallet, including native MON, read with `eth_call` / `eth_getBalance`. Compare with the sum of filled quantities. Dust below a configured threshold (start at the equivalent of £0.05) is logged, not traded.
- Trading 212, if it is ever enabled: positions and pending orders from their read endpoints.
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
| Trading 212 key | Not created until section 5 is resolved. If it is created: same store, IP whitelist on, a dedicated Invest account that holds only bot money. | Whatever scope the app offers for orders on that account. Never the ISA. |
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
- 2FA on Kraken, Trading 212, GitHub, Hetzner, the email account that receives alerts, and the Telegram account. Prefer an authenticator app or a hardware key. SMS is the weak option. The Kraken API key does not get a withdraw permission, so 2FA on the website is what stops a withdrawal even if the trade key leaks.

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
- Stocks: the existing universe tickers, and only while the exchange is open, which the paper bot already enforces. Still blocked entirely until Trading 212 agrees in writing.

Price check: section 1. A quote that fails the check is not sent and is not retried until the next cycle.

### Kill switch from a phone

Two different actions, because today’s `KILL` file stops **everything**, including exits. For live that is the wrong single button.

- **Halt.** No new orders. Exits the strategy already wants still run. Open venue orders are cancelled. Trigger: Telegram `/halt`, or `touch` a `HALT` file over SSH. This is the button for “something looks wrong”.
- **Flatten.** Halt, then market-sell (or limit-IOC through the slippage cap) every live position. Trigger: Telegram `/flatten` plus a second confirmation message within two minutes, so a stray tap does not empty the account. This is the button for “get back to cash”.

Both reply in the same chat with the balances they just read, so the owner can see it worked. The Telegram bot can halt and flatten. It cannot change config, cannot print secrets, and cannot add a withdrawal address.

The website kill on Kraken (delete or disable the API key) and the Trading 212 app (revoke the key) still work if the VPS is the thing that has gone wrong. Those are the controls that do not depend on our code.

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
- Trading 212: a lost HTTP response followed by an order list that already contains the order, to prove we do not send twice. Use the demo host for the recording, not the live host.
- Reconciliation: venue balance differs from the database by more than dust, and the gate refuses a new order.

### Venues’ own test environments

| Venue | What exists |
| --- | --- |
| Monad | Testnet, chain id 10143, faucet, public RPCs including `https://testnet-rpc.monad.xyz`. Use it to test nonce, gas cap, receipt, and revert handling. Kyber may or may not route on testnet. If it does not, test the chain path against a tiny self-sent transfer and test the Kyber encode path with a recorded mainnet quote that is never broadcast. |
| Kraken spot | No self-service spot sandbox. A spot UAT environment exists **on request** through an account manager, with its own URL and keys. The futures demo at `demo-futures.kraken.com` is self-service and is the wrong product. This bot does not trade futures. Do not use the futures demo as a stand-in. The practical spot test is the shadow mode below, then the £20 canary. |
| Trading 212 | A demo base URL, `https://demo.trading212.com/api/v0`, and a live one, `https://live.trading212.com/api/v0`. Keys come from the app. Demo is where any stock adapter is tested. Live is not used until the terms question is answered. |

### Shadow mode

Config `broker: shadow`. The strategy runs on the real cycle. The broker fetches a real Kraken order book and a real Kyber route, runs the risk gate, and writes the order it would have sent, including the price and the reason it would have been refused. It does not sign and does not POST an order. The dashboard gains a column for those rows so the owner can read, for a couple of weeks, “this is what live would have done”. Shadow mode is a success only if those rows match the paper fills on side, asset, and rough size, and if every refused row has a clear reason.

### Canary, then the rest

After shadow mode has been boring, deploy about £20. Suggested split: £10 in the hot wallet and £10 on Kraken. No stocks. One round trip on a liquid Kraken pair and one small Monad swap, then a week of the real strategy inside the canary caps. Reconcile every cycle. If any `unknown` order or any balance break appears, stop and fix it before adding money.

Only then fund the rest of the £200. The stock half stays in paper, or in the owner’s own hands, until Trading 212 has agreed.

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

A Trading 212 Invest account is not an ISA. Gains are taxable. Share pooling uses the same section 104 idea, with the same 30-day rule. Stamp duty and the FX fee, when they are charged, belong on the row. Trading 212’s own annual statement is the cross-check, not the only record.

### Trading 212 terms

The published API Terms say the client is expressly prohibited from using the API for algorithmic trading. The definition is any trading where a computer algorithm automatically determines parameters of orders, such as whether to start the order, the timing, the price, or the quantity, with little or no human intervention. That is what this bot does.

The same terms say the API licence is for personal use and for testing, that a high-speed or automated mass data entry system needs prior written consent, and that a custom interface needs prior written consent and must be tested before live use. The market-order call is also documented as not idempotent, and orders execute only in the account’s main currency. The demo host is `https://demo.trading212.com/api/v0`.

The Invest Terms published for the Cyprus entity repeat the algorithmic-trading prohibition and use the same definition. The owner’s UK account may be a different Trading 212 entity. The binding text is the one shown in the app when the key is created. Until that text, or a written reply from Trading 212, allows this bot, the stock adapter is not switched on. Asking them is an owner action, not a code change. A reasonable thing to ask for is permission for a personal account, a handful of orders a day, no third-party money, and no resale of their data.

Paper stocks can continue. The owner can also read the dashboard and place the Invest trades by hand. Hand placement is slow and is compatible with a human decision. It is not a way to sneak the same unattended bot through.

### Kraken

Kraken’s own developer documentation treats algorithmic spot trading as a normal use of the API and lists the permissions for it. That is a different position from Trading 212. The owner should still read the UK customer terms attached to the account before the canary. The operational limits we will rely on are the ones in section 2: trade-only key, IP whitelist, nonce, no withdraw permission.

Kraken spot has no self-service sandbox. See section 4.

## 6. Rollout

Observation windows below are calendar time because a market has to actually move. Build sizes are the work, not a promise.

### Phase 0. Decisions

Owner only. No code.

- [ ] Confirm paper trading is still the only thing running.
- [ ] Read the Trading 212 terms in the UK app and, if the ban is still there, email them for written consent. Until the reply, stocks stay on paper.
- [ ] Decide the canary amount (£20 is the proposal) and the hard caps in section 3.
- [ ] Decide that XRP, WLD, DOGE, and PEPE are Kraken orders, not Monad swaps.
- [ ] Create the Kraken+ checklist: 2FA, a new trade-only key later, not now.

Effort: an evening, plus waiting for Trading 212.

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
- [ ] Stocks still paper, unless the written Trading 212 answer is yes and the demo adapter has passed.
- [ ] Dead-man and both phone commands tested once on purpose, with a tiny position.
- [ ] First weekly backup of the audit log stored somewhere that is not the VPS.

### Phase 6. Only if stocks are allowed

- [ ] New Invest account, not the ISA, funded with the stock half only.
- [ ] Demo API used until the adapter’s lost-response test passes against the demo host.
- [ ] Live key, IP whitelist, one £5 order, then the strategy.

Do not start phase 6 on a shrug. The published terms say no.

## Open decisions for the owner

1. Trading 212. Ask for written consent, keep stocks on paper, or place stock orders by hand? The plan assumes paper or by hand until they agree.
2. Are the hard caps in section 3 the right pounds? In particular the £10 full-book daily loss halt, which is tighter than the strategy’s 15% Day cap.
3. Public scoreboard: none, or a redacted second site?
4. Phone path: Telegram plus SSH, or Tailscale only?
5. Should the other computer hold a Kraken cancel-only key, or only send an alert?
6. Kraken+ fee. Confirm in the account which pairs are zero-commission and up to what volume. The bot will record the fill’s actual fee either way.
7. Monad token allowlist. Start with MON, cbBTC or WBTC, and WETH only, and add other official-list tokens one at a time?
8. When to rotate the hot wallet. Proposal: only on a VPS rebuild or a suspected leak, because rotation is a real transfer.
9. Whether shadow mode may start before 1 November. It does not change a threshold, but it is still a code change next to a frozen strategy. Default in this plan: wait, unless the owner says otherwise.
10. Tax. Use an accountant for the first return, or file from the CSV? The bot’s job is the CSV either way.

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

Trading 212

- API introduction, demo and live base URLs, Invest and ISA only, IP restriction optional: https://docs.trading212.com/api
- Place market order. Not idempotent. Rate limit stated there as 50 requests per minute. Main-currency only: https://docs.trading212.com/api/orders/placemarketorder
- API Terms, including clause 4.2.a (no algorithmic trading), clause 6.2 (personal use and testing), clause 6.6 (automated mass data entry needs written consent), and the definition of algorithmic trading: https://www.trading212.com/legal-documentation/API-Terms_EN.pdf
- Invest Terms (Cyprus entity) clause 5.1 and the same definition: https://www.trading212.com/legal-documentation/cy/invest/Invest-Terms_EN.pdf

GitHub

- A private repository on Free unpublishes Pages: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility
- Pro adds Pages for private repositories. A privately published Pages site needs an organisation on Enterprise Cloud: https://docs.github.com/en/get-started/learning-about-github/githubs-plans
- Price page (Pro listed at $4 per user per month for the first 12 months, with an asterisk): https://github.com/pricing

UK tax

- When a crypto disposal is taxable, pooling, same-day and 30-day matching, fees as allowable costs: https://www.gov.uk/guidance/check-if-you-need-to-pay-tax-when-you-sell-cryptoassets
- Section 104 pooling: https://www.gov.uk/hmrc-internal-manuals/cryptoassets-manual/crypto22200
- Annual exempt amount £3,000 for 2026 to 2027, and the rates from 6 April 2026: https://www.gov.uk/guidance/capital-gains-tax-rates-and-allowances

Secrets

- `age`: https://github.com/FiloSottile/age
- `sops`: https://github.com/getsops/sops
- systemd credentials: https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html
