# Harden Spot lifecycles and add approved algorithmic trade setups

Partial/uncertain orders and interrupted OCO placement could lose track of acquired
coins or expose unrelated holdings. This change pins and verifies python-binance
1.0.37 interfaces, journals identities before requests, reconciles fills and fees,
isolates account/environment state, and preserves confirmed ownership through
restart, cancellation and emergency recovery. Cross-platform account locks prevent
duplicate instances. Entries use price-capped IOC orders and immutable single-use
Telegram approvals; two OCOs protect confirmed inventory. Uncertain outcomes are
never blindly retried, and unresolved inventory blocks new buys.

Policy remains Spot only, 10M USDT minimum volume, 1% automatic risk, 12 USDT maximum
position, one active/unresolved trade and 3 USDT daily loss threshold. Explicit
manual approvals retain the owner-authorized strategy/liquidity and account-risk
exceptions; hard caps, exchange validity, ownership and protection remain required.
Trades that cannot fit two valid OCO quantities are explained and skipped.

Telegram offers **Manual** (all four prices), **Assisted** (bot entry/SL, user
profit targets), and **Auto Setup** (all algorithm-generated levels). Every mode
still requires approval. The new on-demand ruleset uses closed candles, 1h trend,
BTC regime, ATR/RSI/volume and confirmed resistance; it validates fee-reserved risk
and rejects stale market conditions rather than moving approved exits. It cannot
use manual risk exceptions. The fixed preset is removed; the background scanner
strategy remains unchanged. No trained AI or profitability claim is introduced.

Compact proposals/Details, candidate previews, SHOW PR, complete help, startup-only
identity and verified partial/final net result notifications improve visibility.
Persistent result delivery retries do not submit exchange orders. BNB fees retain
explicit fill-time USDT reference valuation; missing fee evidence blocks final P&L,
not protection/liquidation. Pending approval cancellation never removes existing
OCOs. Active closing remains available through the existing confirmed CLI path.

Validation: **345 passed, 3 native Windows skips**, three upstream deprecation
warnings; Ruff/format, Bandit, pip dependency and whitespace checks pass. Tests use
actual SDK transport simulations with sockets/HTTP forbidden.
See TEST_RESULTS.md for exact totals, SECURITY_REVIEW.md for the implementation
review and ALGORITHM_DESIGN.md for formulas, public Binance/Freqtrade/OctoBot
references and strategy-validation limits. No production credentials, Binance
integration, real trades or third-party bot account connections were used. Native
Windows CI/user results and an independent human review remain separate checks.

The ruleset has no historical out-of-sample profitability or paper/live validation.
Buy plus protection is non-atomic; outages, gaps, slippage, dust and concurrent
account activity remain risks. No merge into main is performed.

Flat-position recovery diagnostics now distinguish zero bot inventory from pending
execution/commission accounting. Fixed allow-listed reason codes expose missing
BNB references, history truncation/incompleteness or reconciliation mismatches
without serializing private exception data. No journal reset, fee assumption,
valuation-window widening or bypass of the daily-loss gate is introduced.


The confirmed BNB-reference blocker now has a bounded historical-candle fallback
when the one-second aggregate window is genuinely empty. It persists/revalidates
the containing closed minute's OHLCV and uses its high to value actual BNB fees.
P&L explicitly identifies this conservative market reference; Binance's private
conversion is not claimed. Unknown, malformed, truncated or missing evidence still
blocks, while proven flat trades can close without another exchange order.

Scanner-generated targets now compile against the original reference entry and
rounded stop with upward tick rounding, preserving the intended 1.5R/2.5R without
chasing a higher quote. Pullback resistance ceilings and approved/manual exits
remain fixed. Distinct stop-distance and reward/risk diagnostics show the actual
checked prices and required TP1; genuinely insufficient reward still blocks.
