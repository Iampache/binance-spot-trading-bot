# Second security review

Scope: all application modules, actual python-binance 1.0.37 method contracts,
offline transport simulation, durable lifecycle transitions, runtime controls and
the regression suite. This is a separate manual review pass by the implementing
agent, plus independent automated static analysis using Bandit. It is **not an
independent human or third-party audit**, and is not certification for live trading.
No Binance account, production credential or Telegram integration was accessed.

## Original findings

| Finding | Implemented control |
| --- | --- |
| 1. Nonexistent SDK methods | Pinned 1.0.37, verified `v3_get_order_list` / `v3_delete_order_list`; real SDK transport tests |
| 2. Incomplete buys lose ownership | Save observed execution quantity before retrieving complete fill/fee evidence; terminal and uncertain outcomes reconciled without another buy |
| 3. Unrelated holdings liquidated | Execution-based remaining inventory, confirmed coverage, and pre-buy nonbot balance floor bound exits |
| 4. Incomplete restart recovery | Durable IDs for every lifecycle step; all active states processed; unknown results quarantined; crash-released filesystem locks |
| 5. Unjournaled/unconfirmed emergency sells | Persist intention before request, reconcile by ID, require terminal/evidenced outcome, prevent ambiguous repeat exits |
| 6. Shared account/environment journals | Binance-verified UID and Spot type; separate environment/account paths; identity checks before submissions and reconciliation |
| 7. Incomplete daily loss accounting | Validated execution evidence, actual supported commissions and fill dates; remaining daily budget caps planned risk |
| 8. Post-buy protection failures | Fee-aware rounded OCO preflight, symbol/exchange capacity and dynamic-price checks; skip incompatible minimums under 12 USDT |
| 9. Monitoring permanently stops | Continued bounded-backoff reconciliation; account partial/canceled fills and safely unwind verified uncovered target remainders |
| 10. Telegram controls / sensitive logs | State/coverage status; cancellation invalidates proposals and rechecks; single-use bounded approvals; credential-safe errors and disabled transport logs |

## Additional problems discovered and addressed in the second pass

- Uncapped market buys could violate displayed cost bounds despite preflight.
  Entries now use quantity- and price-bounded IOC limit orders.
- Stale monitor or status snapshots could overwrite a newer kill/exit journal.
  Every reconciliation reloads current state under the shared account operation
  lock; nested calls use the same lock safely.
- Order cancellation can change client IDs. Persisted cancellation aliases and
  known numeric IDs allow subsequent ownership-checked reconciliation.
- The CLI instance lock made kill unavailable while Telegram was running.
  The CLI now uses the same serialized operation lock as the running engine.
- Canceling every OCO before selling unnecessarily removed all protection at once.
  Each confirmed released leg is unwound before canceling the next list.
- Startup errors could strand non-daemon monitoring workers. Startup/shutdown now
  retain the instance lock until managed workers have finished their transitions.
- An approval could expire during slow validation, or shutdown could admit a new
  buy. Both conditions are checked again immediately before submission.
- Closed journals with missing fills could implicitly reset the daily loss gate.
  Closed-state accounting and inventory evidence are validated during loading.
- Manual entries could bypass the requested volume floor. Entry preflight now
  enforces 10,000,000 USDT quote volume on every path.

Bandit reports no findings. Ruff and formatting checks pass. See TEST_RESULTS.md
for actual execution evidence and coverage limitations; passing checks do not
establish exchange-side correctness.

## Unresolved risks and deployment limits

1. **No live or testnet integration was run.** Method signatures and request
   contracts were exercised through the installed SDK, but simulations cannot
   prove matching-engine behavior, current symbol rules, account permissions,
   rate limits, retention or real API availability. An independently reviewed,
   separately authorized testnet acceptance phase remains necessary before a
   controlled live rollout.
2. **Stops do not cap losses.** Gaps, slippage, illiquidity, trading halts and
   exchange outages can exceed the 1% planned risk and 3 USDT daily threshold.
   The threshold is a pre-entry budget gate, not an exchange-enforced guarantee.
3. **Buy-to-OCO placement is not atomic.** Two OCOs require separate requests. A
   crash or network partition can leave confirmed coins temporarily uncovered.
   Recovery prioritizes ownership and refuses speculative liquidation, which can
   leave exposure pending manual reconciliation.
4. **Cancel-and-sell is not atomic.** Kill and uncovered-target recovery can lose
   connectivity after cancellation. Unknown sell acceptance is never retried
   blindly; holdings may remain exposed until evidence becomes available.
5. **Small positions can be ineligible.** A 12 USDT position cannot satisfy every
   symbol's two-leg minimum. Partial fills, commissions, price falls and precision
   can also produce unsellable residuals. Dust remains tracked and blocks entry;
   there is no automated dust conversion or journal-reset escape hatch.
6. **Third-asset commissions are unsupported.** Known BNB discounts cause a skip.
   Unexpected third-asset fees block accounting until a separate, verified
   valuation/reconciliation workflow is implemented. Fee schedules changing after
   preflight can exceed the reserved commission amount.
7. **Coordination has a local scope.** Dedicated persistent storage and a single
   coordinated account are required. Filesystem locks do not cover another host,
   another state-directory copy, manual trades, transfers or other bots. Complete
   state loss/deletion, local tampering and compromised exchange/Telegram accounts
   cannot be made safe by these local controls.
8. **Recovery may require an operator.** Unknown pre-transmission crashes, exchange
   “not found” responses, missing execution pages, unsupported filters and invalid
   records deliberately block. No automatic inference or manual-edit shortcut is
   supplied to declare these cases safe.
9. **Credential permissions are unverified.** No production credentials were read
   or tested. A future operator must independently verify restricted Spot-only
   permissions, no withdrawal permission, suitable IP restrictions and isolation.
10. **Platform and dependency limits.** Runtime locking requires Linux/POSIX.
    Version constraints improve repeatability but are not artifact hashes or a
    dependency-CVE audit. SDK/websocket deprecation warnings remain upstream.

The code is prepared for review and further controlled acceptance work. It should
not be described as guaranteed safe or live-validated. A reviewer independent of
the implementing agent should examine lifecycle and exchange assumptions before
real funds are enabled.
