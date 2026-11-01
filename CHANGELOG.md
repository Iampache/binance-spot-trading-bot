# Changelog

Release notes describe code changes. Git commits provide the exact history.
Account settings, keys and trade journals remain local and are ignored by Git.

## 1.10.0 — 2026-11-01

- Add owner-requested plain-language rejection cards with numbered known reasons,
  Details, Ignore, quiet 1/3/15-minute setup watches and matching remedial controls.
  Keep automatic scanning proposals-only; add fresh pair navigation to `/scanlog`.
- Observe throughout the selected period and notify only for a fresh validated
  proposal. Bind watches to account, settings revision, generation and deadline;
  cancellation/late delivery cannot register buying authority. Watches are
  read-only and reserve neither funds nor slots.
- Add early and repeated capacity, ownership, accounting, permission and funding
  checks. Calculate two-leg minimum funding from pair rules and fee reserves,
  mark pre-level estimates provisional and retain normal/Manual policy boundaries.
- Ask for 1–10 parallel trades at terminal startup before exchange/Telegram
  connections, defaulting to one. Make Telegram `/setup` changes session-only;
  retain durable baselines/audits and clamp old larger counts without closing
  positions. No ordinary-chat permanent-save or operational Helpdesk is added.
- Invalidate approvals and untriggered watches after confirmed limit changes;
  recheck authenticated account trading permission before a new buy submission.
  Compare approved limit values as well as revisions to reject reused session
  ordinals. Invalid settings block entries while startup recovery still runs.
  Preserve filled positions, journal compatibility and mandatory final approval.
- Add 75 regressions for funding, entry gates, watch lifecycle/delivery races,
  session/persistent isolation, legacy settings and startup order/ceiling.

## 1.9.0 — 2026-11-01

- Reserve automatic scan Telegram messages for validated proposals with review
  controls. Keep routine rejections, cooldowns and no-proposal outcomes in logs;
  remove duplicate candidate summaries after successful proposal delivery.
- Add owner-only `/scanlog` and native Menu access for cached scan diagnostics,
  delivery counts and skip reasons with Dubai timestamps and no approval controls.
- Preserve the last delivered proposal in `/showpr` when later candidates fail;
  retain candidate-preview fallback only before any proposal has been delivered.
- Retire/ignore `SCAN_CANDIDATE_NOTIFICATIONS`, so existing `true` values cannot
  restore background preview spam. Update startup policy narration and docs.
- Preserve lifecycle/protection/recovery notifications, all risk/strategy gates,
  successful-delivery cooldowns and single-use final approval.
- Add 10 regressions and update old notification expectations for the explicitly
  requested policy, retaining technical diagnostics and execution assertions.

## 1.8.0 — 2026-10-31

- Add live performance to position details/Refresh: time held, actual remaining
  cost, reference value, estimated unrealized P&L after current sell-fee reserve,
  realized/combined results and distance to original SL/TP levels.
- Add owner-only `/portfolio`, dashboard and native Menu access for bot exposure,
  free/locked USDT, watch reservations, slots, retained dust and normal risk.
- Allocate actual entry cost once across sold and remaining net units. Preserve
  historical BNB fee references, distinguish estimates and withhold missing data.
- Show full planned risk reservations separately from estimated remaining-stop
  loss; expose incomplete stop coverage and negative Manual-override budgets.
- Detect concurrent local lifecycle/settings changes and withhold mixed totals.
  Keep external holdings out of bot P&L/risk and dust out of realized sales.
- Add detached, non-persisting read options to existing verified refresh/fee
  helpers. Durable execution/recovery still uses the default persistence path.
- Add 36 financial-report regressions plus dashboard navigation coverage.
  Entry rules, approval, order levels, settings and journal format are unchanged.

## 1.7.0 — 2026-10-31

- Add owner-only `/health` plus dashboard and native Telegram Menu access.
- Report authenticated account/open-order read freshness, version/environment,
  uptime, per-trade monitor progress/success/errors and scanner cycle health.
- Distinguish stopped, retrying and delayed workers; retain prior successful
  timestamps when later checks fail, and reset observations on process restart.
- Explain account-wide and normal-risk entry blockers separately. Compare current
  open orders with saved ownership; flag concurrent state changes for refresh.
- Treat full slots and retained dust separately from health faults. Preserve the
  explicit Manual policy and all existing execution/approval requirements.
- Keep diagnosis read-only, without journal writes, order changes, automatic
  recovery, new background exchange polling or waiting on the trading lock.
- Add 32 health regressions and dashboard coverage for the new shortcut.

## 1.6.0 — 2026-10-31

- Fix repeated reconciliation of a base-fee fraction below Binance's quantity
  step: preserve existing OCOs when they cover all exchange-tradable inventory,
  report the tail, and avoid attempting a zero-quantity emergency sale.
- Once tradable inventory closes, retain the proven dust quantity and allocated
  acquisition cost in the same atomic journal, free the trade slot, and report
  the unsold asset separately from realized P&L. Reserve its cost against normal
  portfolio risk capacity; preserve the baseline for subsequent same-pair trades.
- Require terminal order evidence, valued actual fees, physical ownership and
  replayable cost accounting. Keep whole-lot/notional failures unresolved.
- Make closure classification transactional across journal-write failures.
- Add pair/trade identifiers and private-error-safe local validation reason codes
  to recovery alerts. Chunk long `/status` reports.
- Add 19 offline regressions, including the exact reported PEPE/UNI quantities,
  restart and exit recovery, partial buys, accounting and failed persistence.
- Require v1.6.0 or newer after retained-dust closures; preserve runtime journals.

## 1.5.0 — 2026-10-31

- Rename the product to NEXVANT across shared identity, help, scanner, launcher
  and documentation, retaining existing runtime paths and recovery compatibility.
- Add original NEXVANT welcome/banner artwork and a matching profile icon.
- Add the `/start` photo dashboard with owner-authorized shortcuts to discovery,
  coin checking, positions, P&L, setup, status and the full `/help` guide.
- Register an owner-scoped native Telegram command Menu and update the bot's
  display name, descriptions and avatar through verified Bot API methods.
- Cache successful cosmetic setup by bot/chat/content so profile photos are not
  duplicated on every restart; retry failures later and retain text fallbacks.
- Document Telegram/BotFather control of @usernames and native client appearance.
  Trading rules, ownership and approval semantics remain unchanged.

## 1.4.0 — 2026-10-31

- Add `/position` / `/positions`: numbered bot positions with pagination,
  refreshed details, average entry, original SL/TP1/TP2, target status, remaining
  quantity, reference price and observed stop coverage.
- Add Exit Trade → Confirm Exit Trade / Keep Position. Persist exact trade and
  account identity before reusing the existing per-OCO cancel/market-close
  recovery lifecycle. Confirmed exits survive restart and target bot inventory only.
- Bind expiring, single-use controls to their message and immutable trade IDs;
  stale buttons cannot close a new position with the same ticker. Cancel before
  acceptance leaves management in place; accepted exits continue independently.
- Separate Cancel Entry Watch from filled-trade exits. Reject a stale watch
  cancellation after entry starts. Fix CLI/saved-close recovery on unfilled
  watches so a close request can never advance them into a buy.
- Report exit acceptance separately from verified final closure and net P&L.

## 1.3.0 — 2026-10-31

- Add owner-only `/setup`: review and confirm maximum concurrent trades and
  per-trade investment cap, with defaults 1 and 12 USDT. Persist account/environment
  settings and timestamped old/new values in one atomic local file.
- Support protected positions in different USDT pairs concurrently. Validate
  ownership of other bot OCOs, reserve watch funds and open-position risk, enforce
  exchange-wide capacity, and serialize new submissions separately from monitors.
- Bind approval to the settings revision. Settings changes invalidate old
  proposals and untriggered watches; lower limits preserve existing positions.
- Add `/pnl`: fee-adjusted profit/loss for bot trades completed today in Dubai
  time, with counts and explicit exclusions. Keep UTC daily-risk accounting separate.
- Add a flushed Ctrl+C waiting message, Windows console callback, cooperative
  worker shutdown and completion message. Keep exchange protection active.
- Retain fixed normal 1% sizing, 3 USDT daily budget, Spot/USDT and final approval.

## 1.2.0 — 2026-10-31

- Add public High Volume / Top Gainers discovery with Spot links, setup checks
  and the existing three-mode selector. Mark New Listings unavailable pending a
  verified listing source.
- Make BTC weakness advisory in Assisted/Auto Setup, by explicit owner choice;
  retain the coin's setup/risk checks and required closed-candle evidence.
- Report the occupied trade slot before algorithm analysis/manual input.

## 1.1.7 — 2026-10-31

- Retire Cancel/expired/replaced selector keyboards and invalidate backend actions.
- Change candidate heading to Potential Token Detected.

## 1.1.6 — 2026-10-31

- Link Telegram tickers to Binance Spot pages; distinguish same-pair, other-pair,
  watch, pending entry and reconciliation blockers.

## 1.1.5 — 2026-10-31

- Distinguish chart setups from execution eligibility and label reference prices.

## 1.1.4 — 2026-10-31

- Add ticker-only `/scan` as an alias for the single-coin `/check` flow.

## 1.1.3 — 2026-10-31

- Compile scanner reward/risk targets with tick-aware rounding; clarify rejection
  diagnostics while preserving approved exits and resistance limits.

## 1.1.2 — 2026-10-31

- Add validated closed-minute BNB fee reference fallback and explicit reference-valued P&L.

## 1.1.1 — 2026-10-31

- Explain flat-position accounting blockers through fixed, private-error-safe reason codes.

## 1.1.0 — 2026-10-31

- Add Manual, Assisted and Auto Setup with final Telegram approval and closed-candle
  algorithmic level calculation. Document design, public sources and validation limits.

Earlier development includes durable buy/OCO ownership, restart recovery, fee
accounting, Windows locking/launcher support and compact Telegram proposals.
See Git history and the historical sections in TEST_RESULTS.md/SECURITY_REVIEW.md.
