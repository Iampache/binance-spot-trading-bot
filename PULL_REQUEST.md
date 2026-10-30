# Harden Spot lifecycle, recovery, risk limits and Telegram approvals

Partial fills, uncertain exchange responses and interrupted protection placement could leave assets untracked or expose unrelated holdings. The bot also used OCO methods absent from its installed SDK. This change pins python-binance 1.0.37, journals request identities before submission, reconciles execution-based ownership, namespaces state by account/environment, and blocks new entries while evidence remains unresolved.

Entries now use price-capped IOC limits and single-use Telegram approvals with quantity, cost and risk ceilings. The user-requested policy is enforced: Spot only, 10M USDT minimum volume, 1% planned risk, 12 USDT maximum position, one open trade and a 3 USDT daily loss threshold. Incompatible two-OCO minimums are explained and skipped. Status, cancellation, manual entry and 15-minute rechecks are covered by offline regressions.

Validation: 102 tests passed with no failures/skips/expected failures and four unsuppressed upstream deprecation warnings. The simulator exercises the actual installed SDK interface with sockets/HTTP blocked, including 22 journal-write crash checkpoints. Overall coverage is 70%; the exit engine is 87%. Bandit reports no findings; Ruff, formatting, pip dependency checks and diff checks pass.

See TEST_RESULTS.md for the regression mapping and SECURITY_REVIEW.md for the separate second review pass and unresolved risks. No real or testnet integration, production credential access, Telegram messaging or real trading occurred. The second manual review was performed by the implementing agent, with independent automated static analysis; an independent human review remains advisable.

Live acceptance is not established. Stops and kill operations cannot guarantee execution during outages/gaps; buy-to-OCO and cancel-to-sell are not atomic. Unknown outcomes, third-asset fee accounting, unsupported exchange rules, legacy state and unsellable residuals deliberately block new entries. Dedicated persistent storage and exclusive account coordination are required.
