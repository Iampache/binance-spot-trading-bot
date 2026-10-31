# Changelog

Release notes describe code changes. Git commits provide the exact history.
Account settings, keys and trade journals remain local and are ignored by Git.

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
