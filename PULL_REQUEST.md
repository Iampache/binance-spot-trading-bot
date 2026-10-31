# Harden Spot recovery and add approved Telegram setups and token discovery

Interrupted buys, uncertain responses and partial OCO placement could lose track
of acquired coins. The branch journals order identities before requests,
reconciles fills and fees, isolates account/environment state, and recovers owned
inventory after restart. Price-capped IOC entries and two protective OCOs retain
single-use Telegram approval bounds. Uncertain outcomes are never blindly retried;
unresolved inventory blocks new buys. Cross-platform locks prevent duplicate
instances, and BNB fees have evidence-backed historical reference valuation.

Telegram now offers Manual (all prices), Assisted (bot entry/SL, user targets),
and Auto Setup (all calculated levels). Closed-candle trend, ATR, RSI, volume and
confirmed resistance inform algorithm levels; they are revalidated before entry
without moving approved exits. Per the owner's explicit choice, v2 treats BTC
weakness as a visible advisory instead of v1's direction veto. The coin's own
trend/setup, net fee-reserved reward/risk and all other normal checks remain.
An occupied trade slot is reported before algorithm analysis or manual input.

The new read-only /discover menu ranks High Volume and Top Gainers using public
Spot metadata and rolling 24h tickers. Up to five active USDT Spot/OCO pairs with
at least 10M turnover are shown with Binance links, fresh Check setup and existing
three-mode Trading options. Extreme moves carry automatic-entry-limit flags.
New Listings is explicitly unavailable until a verified listing source exists;
these filtered rankings do not claim exact Binance dashboard parity. Discovery
never grants approval, accesses an account or creates orders. It can be browsed
while another position is active. Sender/chat checks, tracked message IDs,
expiry and cancel generations protect the ephemeral controls.

Normal policy remains Spot USDT, 10M minimum volume, 1% risk, 12 USDT maximum
position, one active/unresolved trade and a 3 USDT daily loss threshold. Explicit
manual approvals retain owner-authorized strategy/liquidity and account-risk
exceptions; hard caps, exchange rules, ownership and OCO protection remain.
Every new buy still requires final approval. Strategy identity is now
atr-structure-v2, with fresh proposals required after upgrading; existing
protected trades retain their journal and exchange protection.

Compact proposals/Details, linked tickers, pair-specific blockers, correct
Cancel/expiry keyboards, startup-only branding and persistent partial/final net
result notifications improve visibility. Result delivery retries never submit
exchange orders. Pending UI cancellation leaves protective OCOs active.

Validation: **425 passed, 3 native Windows skips**, three upstream deprecation
warnings. Ruff lint/format, Bandit, pip dependencies and whitespace checks pass.
Tests use synthetic data behind the pinned SDK transport, with sockets/HTTP
forbidden. No production credentials, live Binance, Telegram service or real
trades were used. TEST_RESULTS.md records regressions; SECURITY_REVIEW.md records
the implementation review; ALGORITHM_DESIGN.md documents formulas and sources.

The strategy has no out-of-sample profitability or paper/live validation. Buy and
protection remain non-atomic; gaps, outages, slippage and dust remain possible.
Removing the BTC veto is an owner-requested policy change, not evidence of a
better strategy. Native Windows and an independent human review remain separate
checks. No merge into main is performed.
