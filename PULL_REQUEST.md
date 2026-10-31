# Harden Spot lifecycle, recovery, risk limits and Telegram approvals

Partial fills, uncertain exchange responses and interrupted protection placement could leave assets untracked or expose unrelated holdings. The bot also used OCO methods absent from its installed SDK. This change pins python-binance 1.0.37, journals request identities before submission, reconciles execution-based ownership, namespaces state by account/environment, and blocks new entries while evidence remains unresolved.

Entries now use price-capped IOC limits and single-use Telegram approvals with quantity, cost and risk ceilings. The automatic policy enforces Spot only, 10M USDT minimum volume, 1% planned risk, 12 USDT maximum position, one open trade and a 3 USDT daily loss threshold. A later user-authorized manual risk override bypasses liquidity/strategy, 1% sizing and the daily-loss gate, while retaining the 12 USDT cap, one active trade, exchange rules, OCO protection and bounded explicit approval. Incompatible two-OCO minimums are explained and skipped. Status, cancellation, manual entry and 15-minute rechecks are covered by offline regressions.

Validation: 195 tests passed (three additional Windows-only launcher cases are skipped locally) with no failures/skips/expected failures and three unsuppressed upstream deprecation warnings. The simulator exercises the actual installed SDK interface with sockets/HTTP blocked, including 22 journal-write crash checkpoints. Baseline coverage details and subsequent regression results are recorded in TEST_RESULTS.md. Bandit reports no findings; Ruff, formatting, pip dependency checks and diff checks pass.

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
