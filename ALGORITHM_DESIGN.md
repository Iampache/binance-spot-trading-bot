# NEXVANT algorithmic proposal design

Display version: 1.10.0. Strategy identity: `atr-structure-v2`.
Reviewed public documentation on 31 October 2026 (Asia/Dubai).

This is a deterministic rules-based proposal generator, not a trained AI model,
a price prediction, or evidence of positive expected returns. No profitable edge,
win rate or capital safety has been established by the engineering tests. It can
correctly reject every current symbol if the coin's own setup fails. Telegram
approval remains mandatory, as explicitly confirmed by the account owner.

## User control

V1.10.0 adds guided, owner-requested reason cards and read-only setup observations
lasting 1/3/15 minutes. They repeatedly analyze fresh data; only a validated live
proposal triggers a new notification. Watching is not approval and reserves no
cash/slot. An approved entry watch remains a separate bounded execution lifecycle.
Cancel, deadline, context, settings revision and generation checks prevent stale
observations from registering proposals. All buys still require final approval.

Readiness checks run before mode selection, after chosen levels and before final
approval. Capacity must permit one more trade: current count **less than** the
limit. Funds must support both exchange-valid sell legs after quantity rounding
and commission reserve, at the selected stop when known. Without levels the check
is only a lower bound. Normal buffers, sizing and daily-risk gates remain;
Manual retains explicit strategy/risk exceptions but cannot bypass hard checks.
The exchange account permission is checked again immediately before a new buy.
These are early diagnostics/guards, not altered targets or profitability claims.

Startup asks for 1–10 parallel trades before network initialization. Telegram
`/setup` modifies the current run only; count is asked again next startup and
investment reverts to its saved baseline. Permanent operator updates remain a
local API, not an ordinary-chat capability or implemented Helpdesk approval flow.
Old durable audits remain readable; effective counts are capped at ten and lower
limits never close filled positions. Confirmed changes invalidate old approvals
and untriggered watches. Existing journals and strategy identity are unchanged.
Approval compares actual limit values alongside the revision; repeated process
sessions cannot reuse an ordinal to authorize changed limits. Invalid saved
settings block new plans while existing-position startup recovery still runs.

V1.9.0 makes automatic scan notifications proposal-only. Rejected/cooldown pairs
remain in logs and the owner-requested `/scanlog` snapshot; delivered proposals
retain their normal Approve/Reject/Details controls. No numeric tolerance, target
adjustment, strategy relaxation or new buying authority is introduced. The 1.5R
minimum still rejects a genuinely weaker plan after the checked price/tick rules.
Trade lifecycle and protection/recovery notifications retain their existing paths.

V1.8.0 adds reference-valued position performance and `/portfolio`. Net acquisition
cost includes actual supported fees, with base fees reflected in net units. Cost
is allocated pro rata to sold versus remaining units, so realized and unrealized
results do not double count TP1. Remaining marked P&L subtracts the current
conservative sell-fee reserve from reference value; historical BNB acquisition
fees retain fill-time reference valuation. This is not an executable exit quote.
Portfolio risk shows the existing full active-plan/watch reservation and retained
dust cost, UTC net realized loss and remaining 3 USDT budget. A separate original
stop scenario does not alter the reservation or guarantee protection/loss limits.
All values are observational; no entry/exit formula, approval or journal schema
changes. Missing evidence and concurrent state changes withhold affected totals.

V1.7.0 adds read-only `/health` observations for account/order reads, monitors,
scanner progress, local state and entry blockers. These observations neither
authorize buys nor add/bypass strategy or risk gates. Full slots and normal-risk
exhaustion are shown separately from worker faults. Saved quantities remain
labeled as saved until monitor reconciliation; the health check is not full
fill/fee reconciliation or an executable approval.

- **Manual:** provide ENTRY, STOP, TP1, TP2. Manual risk exceptions stay explicit.
  A local watch waits for the entry cap for up to 15 minutes. If already eligible,
  it can attempt entry promptly. It never leaves an unprotected resting buy.
- **Assisted:** the algorithm suggests a current capped entry and statistical stop;
  the user supplies TWO profit targets. Targets must remain within the model's
  reward/resistance bounds and pass net fee-reserved reward/risk validation. An
  indicative quote can change before the final proposal is displayed; approved
  prices, quantities and risk cannot subsequently be increased without approval.
- **Auto Setup:** the algorithm suggests all levels. No typing is needed, but the
  user still reviews the chart, Details, and explicitly approves or rejects.

“EXIT” in the four-price input means STOP, the stop-loss trigger. OCO stop-market
execution can slip. It does not mean a guaranteed exit price or automatic
breakeven. Two targets retain the existing half/remainder OCO design.

## Market observations

Use actual installed `python-binance==1.0.37` interfaces: `get_server_time`,
`get_klines`, `get_orderbook_ticker` and existing symbol/filter/commission reads.
No Binance account or market endpoints were contacted during development; tests
supply synthetic data behind the real SDK transport interface.

For each calculation request:

1. Fetch up to 201 candles for symbol 15m, symbol 1h and BTCUSDT 1h. Require at
   least 120 closed candles in each series. Timestamp, OHLC, positivity, volume,
   chronology, exact interval and freshness checks reject malformed/stale data.
   Exclude the current unfinished candle, even when its apparent price spikes.
   UTC candle boundaries are the Binance default. No future bar is inspected.
2. The symbol's 1h must satisfy close > EMA20 > EMA50, with EMA50 greater
   than its value five closed bars earlier. Evaluate the same trend condition on
   BTC 1h, but treat failure as a visible weak/downward-market **warning**, not a
   rejection. This owner-requested v2 policy matches the background scanner's
   advisory role for BTC; the exact trend formulas need not identify identical
   regimes. Missing/stale/malformed BTC evidence still prevents a proposal.
   Proposals and Details show the warning, and final validation refreshes it.
   A BTC trend change alone cannot move the approved entry/exits or risk bounds.
3. On symbol 15m, compute EMA20, Wilder RSI14, Wilder ATR14, and latest volume
   divided by the average of the preceding 20 closed bars. Require either:
   - Pullback: a low among the last three bars touched EMA20 + 0.3%, the last
     candle is green and closes above EMA20, RSI is 38–60, relative volume ≥0.8.
   - Breakout: green close above the preceding 20-bar high, relative volume ≥1.5,
     RSI 45–68.
   Reject a close more than 2.5% above EMA20. These parameter choices are
   conservative heuristics, not optimized or historically validated statistics.

## Level geometry

For the on-demand Auto Setup and Assisted modes, entry is the current best ask
plus the existing 0.2% allowance, rounded UP to one valid tick. This is a maximum
buy price after a confirmed setup, not a forecast of the future optimal low.
Execution uses the existing LIMIT IOC lifecycle, which can expire with zero fill.

Let E be the entry cap, A be ATR14, and L the minimum low of the last 12 closed
15m candles. The proposed stop, rounded DOWN, is:

`S = min(L − 0.25 A, E − 1.5 A, 0.99 E)`

The 1% term preserves the current strategy's minimum stop distance; it is not a
replacement for volatility or support. Reject non-positive stops and distances
outside the existing 1%–6% range. Do not tighten the stop to force a trade to fit.

R = E − S. Initial target projections are E + 2R and E + 3R. Search the last 96
closed candles for strict local pivot highs with two already-closed bars on each
side. Cap TP1 before the nearest overhead resistance minus 0.1 ATR; cap TP2 before
the next suitable overhead resistance, otherwise retain its 3R projection. Both
are rounded down. If nearby resistance prevents ordered targets and at least
1.5R gross reward, skip. R projections in open space are not statistically
predicted future prices. Assisted targets cannot exceed these model ceilings.

With the verified account's full reserved fee rate f, also require:

`(TP1 − E − f(TP1 + E)) / (E − S + f(E + S)) >= 1.5`

Also reject when the displayed full-target scenario, after quantity-step rounding
and full entry/exit fee reserves, is non-positive. Coarse quantity steps and the
small budget must not hide that loss even when per-unit reward/risk looks good.

This is an additional conservative screening ratio, not a guarantee that actual
fees/slippage/loss will match the estimate. BNB discount savings are not assumed.

## Execution constraints and changing observations

All existing account identity, journal, open/unresolved position, balance,
liquidity/spread/depth, fee, exchange precision, minimum notional, quantity and OCO
capacity checks remain. Fixed normal risk policy retains 10M USDT minimum 24h
volume, 1% of available USDT risk, configured position cap including reserve (default
12 USDT), configured active count (default one), and mandatory protection for each
trade and 3 USDT daily loss threshold. Algorithm modes cannot invoke manual
exceptions; unsupported exchange minimums cause an explained skip. The 24h move
must also remain within the scanner's -15% / +25% pump/dump boundaries.
V1.3 adds owner-confirmed `/setup` caps and settings-revision approval binding.
Different pairs can coexist only with verified, fully covered inventory or empty
entry watches. Other watches reserve USDT; all active plans reserve their full
estimated loss against the daily budget. Unknown positions/orders still block.

At final planning and immediately before an approved entry, re-fetch closed
candles and verify eligibility. Preserve the strategy identity, original analysis
metadata, approved stop/targets and quantity/cost/risk ceilings in the journal.
Reject when support/volatility now requires a lower stop or resistance invalidates
targets. Never silently move approved exits. Check live ask against the approved
cap and against the last closed-candle setup (ATR-scaled 1%–2.5% drift bound).
Analysis is limited to 20 minutes of age, including a possible 15-minute watch;
proposals and watches retain their existing, shorter independent deadlines.

The planner also supports an internal user-limit entry calculation, used to test
persistent watch revalidation. It is not an additional Telegram mode. That limit
must be at/below ask and within 2 ATR at creation; this is not a stop-buy breakout
order. New closed-candle stop breaches invalidate such a watch before buying.

## Public references reviewed

| Source | What was checked | Consequence for NEXVANT |
| --- | --- | --- |
| [Binance Spot REST](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md) | Klines, server time, order book, IOC, OCO and OTOCO | Separate closed-bar analysis from live execution; retain existing verified order lifecycle. Native OTOCO pending orders wait for full working-order execution and do not solve the current two-bracket/partial-fill design automatically. |
| [Binance filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md) | PRICE_FILTER, LOT_SIZE, MIN_NOTIONAL/NOTIONAL, market quantities, percent bounds | Filter validity is separate from strategy suitability; never enlarge the configured budget to force two OCO legs. |
| [Freqtrade Telegram](https://github.com/freqtrade/freqtrade/blob/develop/docs/telegram-usage.md) | Status/profit views, command/confirmation controls | Make strategy suggestions, execution authorization and realized results distinct. No Freqtrade commands or trading behavior were copied into this bot. |
| [Freqtrade strategy customization](https://github.com/freqtrade/freqtrade/blob/develop/docs/strategy-customization.md) and [lookahead analysis](https://github.com/freqtrade/freqtrade/blob/develop/docs/lookahead-analysis.md) | Candle-based signals, strategy separation, future-data leakage tests | Exclude unfinished candles and document the need for independent walk-forward validation. |
| [OctoBot public README](https://github.com/Drakkar-Software/OctoBot) | Publicly documented Telegram interface, paper trading and backtesting features | Compare interface/testing practices only. No OctoBot instance was contacted, run or evaluated for profitability. |

These are reviews of public documentation, not endorsements, comparative trading
benchmarks, or an audit of proprietary Telegram bots. No third-party service got
account access. Binance's API specification does not provide a profitable strategy
or certify these parameters.

## Validation and unresolved strategy risk

Synthetic offline SDK simulations verify indicator arithmetic, exclusion of open
candles, bad/stale/missing/future data, bearish regimes, resistance rejection, fees,
risk budget/minimums, metadata binding, final revalidation, protected fills, and
persistent watch invalidation. Existing lifecycle/ownership/crash tests also run.
The mode UI tests prove that Assisted/Auto Setup do not submit orders without a
separate approval and that cancellation clears unfinished user input.
V2 regressions separately verify symbol-downtrend rejection, visible advisory BTC
downtrends, malformed BTC rejection, unchanged remaining risk gates, and a BTC
regime change between display and approval preserving the approved exits.

Read-only `/discover` rankings use the public Spot symbol and 24h ticker APIs.
Active Spot USDT/OCO eligibility and 10M turnover precede ranking, independently
of the chart strategy. High Volume sorts quote turnover; Top Gainers sorts
positive 24h change. The UI reuses existing chart checks and mode selection;
discovery creates no proposal, approval, journal or order. New Listings awaits a
verified source because symbol metadata does not establish official listing age.

No out-of-sample historical backtest, walk-forward profitability study, paper
trading run or live matching-engine validation was performed. Before relying on
this strategy, evaluate a user-supplied historical dataset with no future leakage,
realistic commissions/spreads/slippage, ambiguous candles where stop and target
are both touched (assume stop first), partial fills, delistings and periods of
BTC/altcoin downtrends. Report net expectancy, drawdown, losing streak, turnover,
trade rejection rate and sensitivity to nearby parameter choices, then reserve
an untouched evaluation period. Synthetic tests cannot establish these metrics.

V1.6.0 distinguishes tradable inventory from a fraction strictly below the live
`LOT_SIZE` step. Working stops must cover all tradable units; an uncovered whole
lot remains a blocker even if its value is below the order minimum. Once all
orders are terminal and fees, ownership and acquisition costs reconcile, the
below-step fraction and its allocated cost remain in a separate bucket within
the trade journal. Closing the tradable position frees the slot but does not
create a dust sale or realized P&L. Retained acquisition cost reserves normal
risk capacity alongside active-plan risk. No statistical entry/exit formula is
changed by this recovery policy.

Prices can gap between checks, buy and OCO placement are not atomic, targets can
fail to fill, emergency exits can fail, and residuals can block closure when they
do not meet these evidence requirements. Statistical levels are suggestions;
they do not remove the account owner's trading risk.
