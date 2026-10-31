# Harden Spot lifecycle, recovery, risk limits and Telegram approvals

Partial fills, uncertain exchange responses and interrupted protection placement could leave assets untracked or expose unrelated holdings. The bot also used OCO methods absent from its installed SDK. This change pins python-binance 1.0.37, journals request identities before submission, reconciles execution-based ownership, namespaces state by account/environment, and blocks new entries while evidence remains unresolved.

Entries now use price-capped IOC limits and single-use Telegram approvals with quantity, cost and risk ceilings. The automatic policy enforces Spot only, 10M USDT minimum volume, 1% planned risk, 12 USDT maximum position, one open trade and a 3 USDT daily loss threshold. A later user-authorized manual risk override bypasses liquidity/strategy, 1% sizing and the daily-loss gate, while retaining the 12 USDT cap, one active trade, exchange rules, OCO protection and bounded explicit approval. Incompatible two-OCO minimums are explained and skipped. Status, cancellation, manual entry and 15-minute rechecks are covered by offline regressions.

Validation: 202 tests passed (three additional Windows-only launcher cases are skipped locally) with no failures/skips/expected failures and three unsuppressed upstream deprecation warnings. The simulator exercises the actual installed SDK interface with sockets/HTTP blocked, including 22 journal-write crash checkpoints. Baseline coverage details and subsequent regression results are recorded in TEST_RESULTS.md. Bandit reports no findings; Ruff, formatting, pip dependency checks and diff checks pass.

See TEST_RESULTS.md for the regression mapping and SECURITY_REVIEW.md for the separate second review pass and unresolved risks. No real or testnet integration, production credential access, Telegram messaging or real trading occurred. The second manual review was performed by the implementing agent, with independent automated static analysis; an independent human review remains advisable.

Live acceptance is not established. Stops and kill operations cannot guarantee execution during outages/gaps; buy-to-OCO and cancel-to-sell are not atomic. Unknown outcomes, missing fee valuation, unsupported exchange rules/assets, legacy state and unsellable residuals deliberately block new entries. Dedicated persistent storage and exclusive account coordination are required.

Windows follow-up: replace direct fcntl use with pinned portalocker locks, retain
non-inheritable handles for their full protected lifetimes, release locks on
forceful process death, and make journal synchronization platform-aware. Eleven
new portable regression cases cover competing processes and abnormal termination.
A Windows/Linux CI matrix runs the complete offline suite; native Windows results
remain pending. Linux directory fsync is retained; equal Windows power-loss
durability is not claimed.

Candidate/fee follow-up: console and optional Telegram summaries identify each
candidate and proposal refusal/cooldown/delivery outcome. ALLOW_BNB_FEES enables
actual BNB commission accounting using persisted fill-time BNBUSDT reference
prices; the account-reported discount applies only to standard fees. Full fee
reserves and actual USDT fallback are retained. Reference valuation is not an
exact claim about Binance's internal conversion, and BNBUSDT's fee-on-base case
remains blocked. Missing valuation cannot delay protection or liquidation.

Entry-result follow-up: approval acknowledgements no longer imply execution.
Zero-fill IOC expiry, safe rejection metadata, partial-fill reconciliation,
verified remaining OCO coverage and pre-submission approval changes have distinct
notifications. `/status` retains the latest saved entry result and known order ID
after closure. No pricing change, automatic buy retry or live execution was added.

Telegram entry redesign: manual now offers Enter now with three typed exit levels,
a clearly labeled preset requiring no typing, and Wait at my price with four
levels. Immediate entries freeze an ask-based price ceiling and displayed bounds;
local 15-minute watches attempt one IOC only when triggered, cancel on observed
stop breach or interruption, and consume the persistent active slot. `/cancel`
cancels only untriggered watches; existing order recovery is preserved. A gap
regression also fixes known-unsent OCO validation failures blocking safe recovery.
Validation: 242 passed, 3 native Windows skips; lint/format/Bandit/dependency checks
pass. No live trades/account access. See README and SECURITY_REVIEW.md for polling,
gap, non-atomic protection and offline-test limitations.

Candidate visibility follow-up: blocked/cooldown scan candidates retain Entry,
SL, TP1 and TP2 as explicit previews. SHOW PR or /showpr retrieves the latest
process-local proposal/candidate snapshot without approval buttons or new orders.
Historical snapshots cannot reactivate canceled approvals, and the active-trade
limit is unchanged. Validation: 252 offline passes, 3 native Windows skips.

Presentation follow-up: compact shared branding (“Concept & Developed by Fazil”),
display version and Git URL; blue ticker and distinct status markers because
Telegram cannot render custom text colors. Plain labels retain the full meaning.
No execution-policy changes. Full offline suite remains 252 passes / 3 Windows skips.

Compact Telegram decision cards now show total estimated profit at both targets
and initial-stop loss, while full assumptions remain behind a read-only Details
button. Live mode, manual risk exceptions and hard limits stay visible. /help and
/start document all supported commands and button flows, including the fact that
/cancel does not close active positions. Validation: 260 offline passes, 3 native
Windows skips. Fee-reserved estimates do not promise live profit or stop losses.

Verified exit notifications now distinguish partial exits from fully reconciled
trade closure, with net USDT and percent of actual entry cost after commissions.
A persistent notification outbox retries failed Telegram delivery, including after
restart, independently of exchange execution. BNB reference valuation is explicit;
missing evidence does not produce a fabricated final result. Delivery is at least
once, so an acceptance/acknowledgement crash can duplicate the same trade reference.
