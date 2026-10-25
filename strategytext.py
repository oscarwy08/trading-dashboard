"""Plain-English rule cards for the Strategies tab, and one-line tweaks for the Variants tab.

A strategy card is a rule set that does not follow the main Core, Day and Risk pots.
A tweak still follows those pots and changes one part of them. Tweaks are not listed
on the Strategies tab. A signal card is shown only once that paper book is in the cycle,
because its Test 2 row does not exist until then.
"""

# kind: rules | book | tweak
# show: always (Strategies tab) | when_live (only with a Test 2 row) | never (Variants tab)


def _card(name, kind, label, show="always", **fields):
    row = dict(name=name, kind=kind, label=label, show=show)
    row.update(fields)
    return row


RULES = _card(
    "main", "rules", "Main rules",
    holds=("Two accounts that never mix: £100 in crypto and £100 in stocks at the Test 2 start. "
           "Each account is split Core £50, Day £30 and Risk £20. Core holds longer growth names. "
           "Day holds two intraday books, momentum and burst, sharing that pot. Risk holds smaller "
           "speculative names. A pot may lend cash to another pot in the same account. Cash never "
           "crosses between the crypto account and the stock account."),
    entry=("Core adds to a name in daily slices after the weekly pick. Day momentum may buy from "
           "08:00 UK when the score and the expected value after costs both clear, and it does not "
           "buy at or after 21:00. Burst buys a breakout that has enough volume. Risk buys a breakout, "
           "or a dip that has already bounced while the trend is still up. A name that is still falling "
           "is skipped. Less cash raises the hurdle and cuts the size. It does not halt the book."),
    exit=("Core sells a position that is 20% under its cost. There is no Core target; the weekly pick "
          "trims a winner. Day momentum uses a stop of about 2% to 5% and a target of about 4% to 12%, "
          "then a trail. Burst is tighter and also exits after 6 hours. Every Day position is sold at "
          "22:00 UK, and a stock is sold 15 minutes before its market closes. A 15% loss on the Day pot "
          "stops new Day buys for the rest of that day. Risk sells at a 12% stop, or 10% under the peak "
          "after a 15% gain, or after 7 days. A 30% drop in the Risk pot stops new Risk buys."),
    rebalance=("Core picks again on Monday at 09:00 UK. Day does not hold a basket overnight, so it "
               "does not rebalance one. Risk does not rebalance; each position is its own trade."),
    data=("Live prices from Kraken, Coinbase, KyberSwap and Yahoo, a pound-dollar rate, Monad gas, "
          "and the sentiment file scored from news and X. A price that is getting old lowers "
          "confidence. It does not by itself block a sell."),
    tweak="The frozen Core, Day and Risk rules. Every other row in this group changes one of them.",
)

BOOKS = [
    _card(
        "btc_trend", "book", "BTC trend",
        holds="The crypto half is only BTC. The stock half is VUSA, or EQQQ when VUSA has no history. Otherwise that half is cash.",
        entry="Buy the whole sleeve when the latest daily close is above its 50-day average.",
        exit="Sell the whole sleeve when the close falls back to that average or below it.",
        rebalance="Checked once a day. A sleeve is either fully invested or in cash.",
        data="Kraken daily closes for BTC. Yahoo daily closes for the stock. Held through the night and the weekend.",
    ),
    _card(
        "btc_trend_vol", "book", "BTC trend, vol sized",
        holds="The same two sleeves as the plain trend book: BTC, and VUSA or EQQQ.",
        entry=("In only while the close is above the 50-day average. The size is the smaller of full "
               "size and 40% divided by the last 30 days of realised volatility."),
        exit="Out completely when the close is no longer above the average.",
        rebalance="Once a day. A size change smaller than 5 points is skipped, so a quiet day does not trade.",
        data="The same daily closes as the plain trend book. Held through the night and the weekend.",
    ),
    _card(
        "majors_rotation", "book", "Majors rotation",
        holds="One of BTC, ETH or SOL. The stock half stays in cash.",
        entry="Once a week, buy the best 28-day return among those three that are above their own 50-day average.",
        exit="Cash when none of the three are above that average, and when the weekly pick changes.",
        rebalance="Once a week. One name, fully invested, or cash.",
        data="Kraken daily closes for BTC, ETH and SOL. Held through the night and the weekend.",
    ),
    _card(
        "btc_core_satellite", "book", "BTC core + satellite",
        holds="Mostly BTC, plus at most one other coin. The stock half stays in cash.",
        entry=("85% in BTC. The other 15% is one coin that has beaten BTC over both 30 and 90 days "
               "and is above its 100-day average."),
        exit="The satellite is sold when it no longer qualifies, but not during its first 5 days.",
        rebalance="Once a week. BTC is put back to 85% only after it has drifted by more than 5 points.",
        data="Kraken daily closes for BTC and the liquid alt list. Held through the night and the weekend.",
    ),
    _card(
        "rs_rotation_veto", "book", "RS rotation, veto",
        holds=("Up to three liquid coins, or BTC if none qualify. Cash when BTC itself is below its "
               "50-day average. The stock half stays in cash."),
        entry=("Once a week, the three strongest 4-week returns versus BTC, among names above their "
               "50-day average. A buzz jump of 3 or more, or a 24-hour price jump of 15% or more, is skipped."),
        exit="Sold when it drops out of that weekly list. The whole sleeve is cash when BTC is below its 50-day average.",
        rebalance="Once a week. The picks are equal weight.",
        data="Kraken daily closes, plus the buzz and the 24-hour move already stored by the cycle.",
    ),
    _card(
        "quiet_strength", "book", "Quiet strength",
        holds="Coins that are quiet and close to their recent high. The stock half stays in cash.",
        entry=("Once a week, names within 10% of their 30-day high, with buzz below the universe median "
               "and volume over market cap in the top quarter."),
        exit="Checked every day. Sold on a 12% trail from the peak, or when buzz jumps to 3 or more after a 15% run-up.",
        rebalance="New names are chosen once a week. An exit does not wait for that day.",
        data="Kraken daily closes, buzz, and volume over market cap. Held through the night and the weekend.",
    ),
    _card(
        "best_of_plus", "book", "Best of plus trend",
        holds=("85% follows the BTC and VUSA trend. The other 15% may be used by the best-of Day and Risk "
               "tweaks: sentiment split, the 15-minute entry watch, and the burst runner. Core alt picks are off."),
        entry="The trend half buys on the plain 50-day rule. The small half uses those three tweaks and nothing else from the variant list.",
        exit="The trend half exits with the 50-day rule. The small half uses the Day and Risk exits. Nothing is forced flat at 22:00.",
        rebalance="The 85/15 split is set once. The trend half is checked once a day.",
        data="Daily closes for the trend half. The same live prices and sentiment as the main book for the small half.",
    ),
    _card(
        "trend_carry_unlock", "book", "Trend, funding and unlocks",
        holds=("The strongest of BTC, ETH and SOL that is above its 50-day average, sized like the "
               "volatility book. The stock half follows that same volatility rule on VUSA."),
        entry=("Once a week. Crypto is halved when BTC perpetual funding is in the top tenth of the past "
               "year, or at 25% a year or more. An alt with more than 1% of supply unlocking within 30 days "
               "is left out. If that feed is missing, only BTC and ETH can be held."),
        exit="Out of a name that falls below its 50-day average, and out of an alt that fails the unlock rule.",
        rebalance="Once a week. A resize smaller than 10% of the current position is skipped.",
        data="Kraken daily closes, BTC funding, and unlock dates from PatternPlus. Held through the night and the weekend.",
    ),
    _card(
        "insider_rs_stocks", "book", "Insider relative strength",
        holds="US and UK shares in the stock half. The crypto half stays in cash.",
        entry=("Once a month, the strongest names by six-month return that are above a long average. "
               "A name already in the top half of that ranking is overweighted after an opportunistic "
               "open-market insider buy. A person who already traded that same calendar month in an earlier year is ignored."),
        exit="Sold at the next monthly pick when it drops out of that list.",
        rebalance="Once a month. Weights are the monthly list, with the insider tilt on top.",
        data="Yahoo daily closes and SEC Form 4 filings. Held through the night and the weekend.",
    ),
]

# Each of these is its own £200 book. The live row appears when that book is in the cycle.
SIGNALS = [
    _card(
        "signal_engine", "book", "Signal engine", "when_live",
        family="signal",
        holds="BTC and VUSA, sized by volatility, and only while each close is above its 50-day average. Cash stays cash.",
        entry=("Once a week, start from that volatility size. News, the routine sentiment file, funding, "
               "unlocks, insider buys, trend, momentum, volatility and a short forecast can tilt it. "
               "A signal counts only when an older window and a later window agree it ranked with the next week's return."),
        exit="Flat when the close is back under the 50-day average. A tilt cannot put cash into the market, and it cannot short.",
        rebalance="Once a week. A resize smaller than 10% of the position is skipped. No agreed signal means the plain volatility size.",
        data="Daily closes, the hourly news file, and funding, unlock and insider marks already stored by the other books. The trading cycle does not fetch news.",
    ),
    _card(
        "signal_purge", "book", "Signal engine, purged", "when_live",
        family="signal",
        holds="The same BTC and VUSA volatility trend as the signal engine.",
        entry=("The same tilt, except a training label that reaches into the later window is dropped, "
               "and the first five days of that later window are left out. The two windows no longer share a return."),
        exit="Flat when the close is back under the 50-day average. Cash stays cash.",
        rebalance="Once a week, with the same 10% resize skip.",
        data="The same closes and side files as the signal engine. Nothing extra is fetched.",
    ),
    _card(
        "signal_equal", "book", "Signal engine, equal weight", "when_live",
        family="signal",
        holds="The same BTC and VUSA volatility trend as the signal engine.",
        entry=("A signal that passes the same two-window test gets an equal vote. The size of the "
               "correlation is not used as the bet size. The tilt is the average of the votes."),
        exit="Flat when the close is back under the 50-day average. Cash stays cash.",
        rebalance="Once a week, with the same 10% resize skip.",
        data="The same closes and side files as the signal engine.",
    ),
    _card(
        "signal_multitest", "book", "Signal engine, multiple tests", "when_live",
        family="signal",
        holds="The same BTC and VUSA volatility trend as the signal engine.",
        entry=("The same tilt, but the bar is the correlation you would expect from the best of the "
               "nine signals if they were all noise. A weaker correlation is left out, even if both windows agree."),
        exit="Flat when the close is back under the 50-day average. If nothing clears the bar, the book is the plain volatility size.",
        rebalance="Once a week, with the same 10% resize skip.",
        data="The same closes and side files as the signal engine. The nine signals are the ones already in that book.",
    ),
    _card(
        "signal_news", "book", "Signal engine, news decay", "when_live",
        family="signal",
        holds="The same BTC and VUSA volatility trend as the signal engine.",
        entry=("The same tilt, except a headline fades with a three-day half life. An old headline "
               "does not stay fully in force until the next one."),
        exit="Flat when the close is back under the 50-day average. Cash stays cash.",
        rebalance="Once a week, with the same 10% resize skip.",
        data="The hourly news file, faded from the time it was scored. The trading cycle does not fetch it.",
    ),
    _card(
        "signal_funding_contrarian", "book", "Funding, cash when hot", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC unless yesterday's annualised perpetual funding is hot.",
        exit="Cash when funding is at 25% a year or above the prior year's 90th percentile. No funding history means cash.",
        rebalance="Once a week. The position is all or nothing.",
        data="Deribit BTC perpetual funding, lagged one day. The book reads a shared cache. It does not page the history itself.",
    ),
    _card(
        "signal_funding_carry", "book", "Funding carry", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC only when yesterday's annualised perpetual funding is hot: 25% a year, or above the prior year's 90th percentile.",
        exit="Cash when funding is not hot. No funding history means cash.",
        rebalance="Once a week. The position is all or nothing.",
        data="The same lagged Deribit funding cache as the cash-when-hot book. This is the opposite sign.",
    ),
    _card(
        "signal_basis", "book", "Basis, cash when rich", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC when the previous 7-day perp premium is not rich.",
        exit="Cash when that premium is above its trailing median. No history means cash.",
        rebalance="Once a week. The position is all or nothing.",
        data="Deribit BTC perpetual close over the BTC spot close, lagged. Shared cache.",
    ),
    _card(
        "signal_dvol", "book", "DVOL fear", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC when the previous Deribit volatility index is not in the top of its own past year.",
        exit="Cash when that index is above its trailing 252-day 80th percentile. No history means cash.",
        rebalance="Once a week. The position is all or nothing.",
        data="Deribit DVOL, lagged one day. Shared cache.",
    ),
    _card(
        "signal_vrp", "book", "Variance premium", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC when implied variance minus 30-day realised variance is above its trailing median.",
        exit="Cash when that premium is not above the median. No history means cash.",
        rebalance="Once a week. The position is all or nothing.",
        data="Deribit DVOL and BTC daily closes. Realised variance from daily closes is a coarse stand-in for a high-frequency measure.",
    ),
    _card(
        "signal_stablecoin", "book", "Stablecoin supply", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC when the lagged 30-day change in stablecoin supply is positive.",
        exit="Cash when that change is not positive. No history means cash. This is a hypothesis, not a published result.",
        rebalance="Once a week. The position is all or nothing.",
        data="Stablecoin supply, lagged one day. Shared cache.",
    ),
    _card(
        "signal_macro", "book", "Macro permission", "when_live",
        family="signal",
        holds="BTC in the crypto half and VUSA in the stock half, each on its own, or cash.",
        entry="Once a week, a sleeve is invested only when the Nasdaq is above its 200-day average and the dollar index is down over 50 days.",
        exit="That sleeve is cash when either condition fails. Missing market data leaves both sleeves in cash.",
        rebalance="Once a week. Each sleeve is all or nothing.",
        data="Nasdaq and the dollar index, lagged one day, plus BTC and VUSA closes. Shared cache.",
    ),
    _card(
        "signal_tsmom", "book", "12-month momentum", "when_live",
        family="signal",
        holds="BTC, and VUSA on its own price. Either sleeve can be cash.",
        entry="Once a month, hold the asset when its 12-month return, skipping the most recent month, is positive.",
        exit="Cash for that sleeve when the return is not positive.",
        rebalance="Once a month. A weekend does not sell the stock sleeve just because the market is closed.",
        data="BTC and VUSA daily closes only. No extra feed.",
    ),
    _card(
        "signal_gold", "book", "Gold risk-on", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a month, hold BTC when the lagged 126-day gold return is negative.",
        exit="Cash when gold is not down over that window. No gold history means cash. This is a hypothesis.",
        rebalance="Once a month. The position is all or nothing.",
        data="Gold daily closes, lagged one day. Shared cache.",
    ),
    _card(
        "signal_onchain", "book", "Active addresses", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC when the lagged 30-day change in unique Bitcoin addresses is positive.",
        exit="Cash when that change is not positive. No history means cash. This is a hypothesis.",
        rebalance="Once a week. The position is all or nothing.",
        data="Unique Bitcoin addresses, lagged one day. Shared cache.",
    ),
    _card(
        "signal_gdelt", "book", "News tone", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC when the previous 7-day average news tone for Bitcoin is positive.",
        exit="Cash when that tone is not positive. No tone history means cash.",
        rebalance="Once a week. The position is all or nothing.",
        data="GDELT tone, lagged so the week used ends the day before the decision. Shared cache. Separate from the hourly headline file.",
    ),
    _card(
        "signal_dominance", "book", "BTC dominance", "when_live",
        family="signal",
        holds="BTC, or cash. The stock half stays in cash.",
        entry="Once a week, hold BTC when lagged 30-day BTC dominance rose.",
        exit="Cash when dominance did not rise over those 30 days. No history means cash. This is a hypothesis.",
        rebalance="Once a week. The position is all or nothing.",
        data="BTC share of crypto market cap, lagged one day. Shared cache.",
    ),
]

TWEAKS = [
    _card("ev_exit", "tweak", "EV exit", "never",
          tweak="Would sell when live expected value turns negative. That rule is off: the live number rose as the trade fell. This book was reset and now follows the main rules."),
    _card("review_leftovers", "tweak", "Review leftovers", "never",
          tweak="Wider burst stop, a smaller Core fallback when a coin is extended, a 3-day uptrend on a new entry, and no Day plus Core buy of the same coin after midday."),
    _card("all_combined", "tweak", "Both together", "never",
          tweak="The review leftovers on one book. The burst runner is not part of this one."),
    _card("burst_runner", "tweak", "Burst runner", "never",
          tweak="A burst that hits its target keeps running, then gives back 20% of the peak gain."),
    _card("everything", "tweak", "Everything combined", "never",
          tweak="The review leftovers and the burst runner, together."),
    _card("midnight_crypto", "tweak", "Midnight crypto", "never",
          tweak="Day crypto may enter until 23:45 UK and flattens at midnight. Stocks still flatten before the close."),
    _card("entry_watch", "tweak", "Entry watch", "never",
          tweak="Buys between cycles when an armed price trigger trades. The main book only logs that as a would-buy."),
    _card("risk_giveback_15", "tweak", "Risk giveback 15", "never",
          tweak="After a Risk trade is up 15%, sell when the gain falls to 65% of the peak gain."),
    _card("day_trail_arm", "tweak", "Day trail arm", "never",
          tweak="A Day trail arms only when 60% of the peak gain clears the round-trip cost and the 1-hour range."),
    _card("day_ev_blend", "tweak", "Day EV blend", "never",
          tweak="A Day entry's expected value blends the model with that sleeve's own measured results."),
    _card("no_risk_clock", "tweak", "No risk clock", "never",
          tweak="Risk may re-enter the same name without waiting 24 hours."),
    _card("day_capital_caution", "tweak", "Day capital caution", "never",
          tweak="A losing Day account raises the entry hurdle and cuts size. The 15% halt on the Day pot stays."),
    _card("partial_take", "tweak", "Partial take", "never",
          tweak="At the trail arm, sell half. Buy that half back on a dip only when a fresh expected value clears."),
    _card("sent_split", "tweak", "Sentiment split", "never",
          tweak="Missing sentiment counts as neutral. A hot run-up is penalised, and a quiet tape can earn a small bonus."),
    _card("best_of", "tweak", "Best of", "never",
          tweak="Sentiment split, the 15-minute entry watch, and the burst runner, together. No other main-rule change."),
    _card("risk_ev_fix", "tweak", "Risk EV fix", "never",
          tweak="A Risk entry's expected value starts from a replay of the pot's own setups and exits on past prices, not a 3.5% win and a coin flip."),
]


def cards():
    """Every card the dashboard ships. Order is main, strategy books, signal books, then tweaks."""
    return [RULES] + list(BOOKS) + list(SIGNALS) + list(TWEAKS)


def by_name():
    return {row["name"]: row for row in cards()}


def strategy_names():
    """Names the Strategies tab may show. Tweaks are not in this list."""
    return [row["name"] for row in cards() if row["kind"] != "tweak"]


def tweak_names():
    return [row["name"] for row in cards() if row["kind"] == "tweak"]
