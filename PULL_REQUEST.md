# NEXVANT: recoverable Spot positions, approved controls and fractional-dust accounting

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

Validation: **557 passed, 3 native Windows skips**, three upstream warnings.
Ruff lint/format, Bandit, dependency and whitespace checks pass. Nineteen new
regressions cover the exact PEPE/UNI replay, retained cost, same-pair ownership,
whole-lot/unknown/fee/balance blockers, safe diagnostics, partial IOC fills and
atomic-write failures. The suite forbids sockets/HTTP and uses synthetic SDK
transport data. No production credentials, live Binance, Telegram service or real
orders were used. Windows-specific behavior remains a native verification step.

Source versions and changes are recorded in CHANGELOG.md and Git history;
private settings/journals stay local. See TEST_RESULTS.md, SECURITY_REVIEW.md and
ALGORITHM_DESIGN.md for evidence and limits. Buy/protection and cancel/sell are
non-atomic; outages, gaps and residual exposure remain possible. No profitability
or zero-defect claim is made. No merge into main.
