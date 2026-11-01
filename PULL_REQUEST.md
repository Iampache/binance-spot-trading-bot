# Single-target or Two-target exit plans (NEXVANT v1.11.0)

A two-OCO-only plan requires enough funding and exchange capacity for two exit
portions, even when the user wants one full-position take profit. Add a per-plan
choice while preserving normal entry checks and explicit final approval.

## Behavior

- Before mode selection, named buttons or replies select **1 = Single target** or
  **2 = Two targets**. Both occupy one trade slot.
- Manual collects ENTRY/SL/TP or ENTRY/SL/TP1/TP2. Assisted asks for one or two
  targets. Single-target Auto Setup uses the existing first-target calculation.
- Funding, fee-reserved quantity and symbol/exchange order capacity reflect the
  selected structure. Single target protects all tradable bot inventory with one
  OCO. Two targets retains the split OCOs and common original stop.
- Proposals display structure, estimated exit quantities, selected levels and
  profit/loss estimates. Single-target views have no phantom TP2 or second sale.
- Change targets retires the old proposal and opens a fresh selection/mode flow;
  it never edits exchange orders and always requires a new approval.
- Account/settings/generation/message/expiry checks guard selections and slow
  analysis/delivery. Approved entry watches and setup observations retain their
  selected structure. Cancellation cannot restore a pending approval.
- Automatic scans remain Two-target by default, with Change targets on delivered
  proposals. Rejected background candidates remain quiet.

## Compatibility and scope

Legacy records without target metadata retain Two targets. Existing filled trades
are not converted or repriced. New metadata binds structure/levels to approval;
unknown styles or mismatched protective lists fail closed. Actual execution,
commissions, retained dust, confirmed exits, restart recovery and P&L work for
either structure. Preserve `.env` and all `runtime_state`; no migration/reset or
new dependency is required. Use v1.11.0 or newer after Single-target records exist.

Normal liquidity, 1% sizing, 3 USDT risk budget, fee-aware algorithmic RR, exchange
filters, ownership and final approval remain. Manual retains its explicit existing
exceptions. This change does not introduce trailing stops or automatic breakeven.

## Validation

- **789 passed, 3 native Windows skips** locally; three upstream deprecation warnings.
- Ruff lint/format, Bandit, dependency consistency and whitespace checks pass.
- **77 new cases** cover full TP/SL accounting, actual fee dust, one-list funding
  and capacity, partial fills, timeouts/rejection/restart, confirmed exits,
  fourteen journal crash boundaries, approvals/metadata, all UI modes and stale
  authorization races. Existing UI tests add explicit Two-target selection while
  retaining numerical/ownership/execution assertions.
- Native Windows matrix results are tracked by GitHub Actions on this PR.
- Tests use synthetic SDK transport and forbid sockets/HTTP; no production
  credentials, live Binance orders or Telegram service were used. Functional
  verification does not prove profitability, live reliability or loss limits.
