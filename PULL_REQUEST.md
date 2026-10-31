# NEXVANT: recoverable Spot positions with live performance, portfolio risk and diagnostics

Position details previously lacked net performance on remaining inventory and an
aggregate view of cash/risk commitments. V1.8.0 adds on-request position valuations
and owner-only `/portfolio` through the dashboard and Telegram Menu. Position
details/Refresh show time held, actual remaining acquisition cost, reference value,
estimated net unrealized P&L, actual realized result, their combined estimate and
distance to original SL/TP levels. Partial sales allocate cost once; price crossing
never invents a fill. Fully sold records awaiting closure have a flat display.

Portfolio shows bot holdings/per-position results, whole-account free/locked USDT,
watch cash reservations, trade slots/cap, full active planned-risk reservation,
retained dust/cost, UTC realized loss, remaining normal budget, 1% sizing reference
and hypothetical loss on remaining holdings at original stops. Risk reservations
retain the existing conservative full-plan policy after TP1. Incomplete stop
coverage and negative Manual-override budgets remain visible. External holdings
are excluded from bot valuation; closed dust is not treated as an active position
or invented sale. Missing metrics or concurrent lifecycle/settings changes withhold
affected totals rather than reporting a misleading subtotal.

Detached views use the verified refresh/fee helpers with `persist=False`; execution
and recovery retain default durable persistence, ownership checks and approvals.
Historical BNB fees use fill-time references, future sell fees use a fresh current
conservative reserve, and absent evidence is not replaced by zero/stale plan fees.
Reports cannot submit/cancel/reprice orders, write journals, import manual holdings
or change settings. No journal migration, dependency or background polling is added.

Running threads and generic reconciliation warnings previously gave no clear
view of successful monitoring versus repeated failures. V1.7.0 adds owner-only
`/health` through the dashboard and Telegram Menu. It reports current account/open
order reads, last successful check, per-trade monitor progress/success/errors,
scanner status, version/environment, uptime, local limits, saved coverage, funds,
retained dust and account-wide versus normal-risk entry gates. Full trade slots
can coexist with healthy operation; missing evidence never becomes a fresh OK.

Health diagnosis uses reads and process-local observations only, without journal
writes, trading-lock waits, order changes, automatic recovery or new background
polling. Authenticated identity and saved order ownership are checked; lifecycle
changes during the read are flagged for refresh. Saved quantities remain labeled
as saved. Alive but delayed workers differ from stopped workers. Prior success
survives a later failure within the process, and restart clears observations
without changing durable state. Every buy still requires its existing checks and
approval; `/health` does not adopt manual Binance orders or authorize trading.

A reported PEPE trade had 1,723,228.05 coins remaining and working stops for
1,723,228. Base commission left a 0.05 fraction below the exchange's one-coin
quantity step. Recovery attempted to sell the fraction, rounded to zero, and
repeated a generic ValueError while blocking new entries. V1.6.0 preserves the
stops when every tradable unit is covered and reports the exact unsellable tail.
The reported PEPE/UNI state recovers without an exchange mutation or a UNI change.

After tradable units close, the retained fraction and its allocated acquisition
cost are recorded in the same atomic trade journal. Terminal execution evidence,
valued fees, physical ownership above the baseline and replayable accounting are
required. The trade slot is released; actual-sale P&L excludes the unsold cost,
which remains reserved against normal risk capacity. Status, position details,
results and daily P&L disclose retained assets. Whole-lot/notional failures,
unknown orders and missing evidence remain unresolved. Later same-pair entries
preserve old dust in their baseline. Closure classification survives journal
write failure without leaving half-classified records or duplicating sales.

Recovery alerts identify the pair/trade and use fixed reason codes for known
local failures without exposing raw exception text. No automatic dust sale,
conversion, top-up or false zero is introduced. Preserve runtime journals and
keep v1.6.0 or newer after dust closures; earlier versions reject closed records
with positive remaining inventory. External disposal is not invented as a sale.

The branch also includes durable buy/OCO intents, evidence-based restart
recovery, capped IOC entries, fee accounting, account/environment isolation and
single-use Telegram approval. Unknown writes are reconciled before another
submission. Owner-only `/setup` persists confirmed parallel-trade and investment
caps; cash, active risk, order capacity and exact OCO ownership constrain entry.
Defaults remain one trade and 12 USDT. `/position` adds read-only details and
expiring, exact-record Exit Trade confirmation with durable per-OCO unwind.
Watches have a distinct cancellation. `/pnl` uses completed trades' final sell
day in Dubai time; UTC daily-risk accounting remains separate. Cooperative Ctrl+C
announces saving and joins workers before releasing the instance lock.

NEXVANT identity, original artwork, `/start` dashboard, owner-scoped Telegram Menu,
cached profile setup, linked tickers, public discovery and three proposal modes
reuse the same authorization flows. BTC weakness is advisory by explicit owner
choice; the coin's strategy checks remain. New Listings awaits a verified source.
Normal policy retains Spot USDT, 10M turnover, 1% sizing and the 3 USDT daily-risk
budget. Manual risk exceptions remain explicit; every new buy requires approval.

Validation: **627 passed, 3 native Windows skips**, three upstream warnings.
Ruff lint/format, Bandit, dependency and whitespace checks pass. Thirty-six new
financial-report cases plus dashboard coverage independently verify cash-flow/
cost allocation, partial exits, fees, unknown data, reservations, ownership,
concurrency, day boundaries, negative budgets and read-only behavior. Thirty-two health
cases plus dashboard coverage verify read-only diagnosis, real worker loops,
stale/missing/error states, account/local/order failures, concurrent state changes,
entry gates, dust, authorization and restart history. Nineteen dust regressions
cover the exact PEPE/UNI replay, retained cost, same-pair ownership, whole-lot/
unknown/fee/balance blockers, safe diagnostics, partial IOC fills and atomic-write
failures. The suite forbids sockets/HTTP and uses synthetic SDK
transport data. No production credentials, live Binance, Telegram service or real
orders were used. Windows-specific behavior remains a native verification step.

Source versions and changes are recorded in CHANGELOG.md and Git history;
private settings/journals stay local. See TEST_RESULTS.md, SECURITY_REVIEW.md and
ALGORITHM_DESIGN.md for evidence and limits. Buy/protection and cancel/sell are
non-atomic; outages, gaps and residual exposure remain possible. No profitability
or zero-defect claim is made. No merge into main.
