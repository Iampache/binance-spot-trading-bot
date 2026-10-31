# Harden Spot recovery and add configurable approved Telegram trading

Interrupted buys, uncertain responses and partial OCO placement could lose track
of acquired coins. This branch journals order identities before requests,
reconciles fills/fees, isolates account/environment state and recovers ownership
after restart. Price-capped IOC entries and two protective OCOs retain single-use
Telegram approval bounds. Uncertain writes are never blindly retried.

V1.3 adds owner-only /setup with reviewed, confirmed maximum active trades and
per-trade investment cap. Defaults remain one trade and 12 USDT. Account-scoped
settings, monotonic revisions and timestamped old/new values are persisted in one
atomic, Git-ignored local file. Changing settings invalidates pending approvals
and untriggered watches; lower limits never close or reprice existing positions.
Plans/approvals bind the settings revision, and locked final checks enforce the
current limits even when a proposal was built before a change.

Different USDT pairs can now be protected concurrently. Duplicate pairs, uncertain
positions, unprotected inventory and unowned/changed exchange orders still block
entries. Other OCOs are refreshed and matched by pair, IDs, side/type, list,
quantity and exit prices. Watch costs reserve free USDT; full active-plan risk is
reserved against the normal daily budget. Current order/list usage counts toward
exchange capacity. New submissions remain serialized, with separate position
monitoring; recovery/close operations retain account locking and per-pair ownership.

/pnl reports fee-adjusted results for fully completed bot trades on the final
sell-fill day in Dubai time, including each trade's full result. Open/partial,
unfilled and pending-accounting records are excluded; invalid accounting shows
unavailability. BNB fees retain evidence-backed reference valuation. UTC fill-day
daily-risk accounting remains separate. Ctrl+C now announces a cooperative stop,
including a Windows console callback independent of the main network wait, then
joins workers before releasing the instance lock. Exchange OCOs remain active.

The branch also includes Manual, Assisted and Auto Setup, public High Volume/Top
Gainers discovery, linked tickers, clear execution blockers, correct Cancel/expiry
keyboards, compact proposals/Details and persistent net-result delivery. New
Listings awaits a verified source. By explicit owner choice, BTC weakness is
advisory in algorithm modes; the coin's own trend/setup checks still apply.

Normal fixed policy remains Spot USDT, 10M turnover, 1% risk sizing and a 3 USDT
daily loss/risk budget. Owner-authorized Manual exceptions remain explicit; they
cannot bypass configured caps, exchange validity, ownership, OCO protection or
final approval. Every new buy still requires approval. Settings are ceilings,
not guaranteed allocation; aggregate normal risk/funds can prevent filling slots.

Validation: **479 passed, 3 native Windows skips**, three upstream deprecation
warnings. Ruff lint/format, Bandit, pip dependencies and whitespace checks pass.
Tests use synthetic transport data with sockets/HTTP forbidden. New tests cover
atomic settings/audit, stale approvals, concurrent pair admission, separate
protection/recovery/closing, cash/risk reservations, exchange capacity, completed
P&L day boundaries and shutdown ordering. Windows console callback logic is mocked
locally; actual Windows console timing remains a native verification step.

CHANGELOG.md now records versions alongside Git history; private account settings
and journals remain local. See TEST_RESULTS.md, SECURITY_REVIEW.md and
ALGORITHM_DESIGN.md. No production credentials, live Binance, Telegram service
or real orders were used. No historical profitability or paper/live matching
validation is claimed. Parallel exposure and removal of the BTC veto are explicit
owner-requested policies, not evidence of better returns. Buy/protection remains
non-atomic; gaps, outages, slippage and dust remain possible. No merge into main.
