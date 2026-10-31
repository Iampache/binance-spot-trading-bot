# Binance Spot Trading Bot by IamPache

A Python-based Binance Spot Trading Assistant with Telegram integration.

## Algorithmic proposal modes (v1.1.0)

Send `/check COIN` → **3 - Trading options**:

| Mode | You provide | Bot provides | Risk policy |
| --- | --- | --- | --- |
| Manual | Entry, SL, TP1, TP2 | Validation and bounded execution | Explicit manual override |
| Assisted | TP1 and TP2 | Statistical entry cap and SL | Normal risk limits |
| Auto Setup | Approval after reviewing the proposal | Statistical entry cap, SL, TP1, TP2 | Normal risk limits |

Every mode requires a fresh Telegram approval. “EXIT” means the stop-loss trigger;
profit-taking exits are TP1 and TP2. There is no autonomous buying and no AI model.
The former fixed -3%/+5%/+8% preset has been replaced with data-based proposals.
The existing background scanner's strategy is retained. These new algorithm modes
are on-demand and use closed-candle trend/volatility/volume/structure checks.
They can reject a setup rather than invent levels. See [ALGORITHM_DESIGN.md](ALGORITHM_DESIGN.md)
for exact formulas, reviewed Binance/bot references, tests and unverified strategy
performance. Later development-history sections describe previous UI versions.

## Features

* Automatic market scanning every 15 minutes
* EMA, RSI, ATR and volume analysis
* Telegram trade proposals with Approve/Reject buttons
* On-demand coin analysis using /check
* Manual entry, stop-loss and take-profit levels
* OCO order management
* Risk management and trading limits

## Security

Never upload API keys, Telegram tokens, or your .env file to GitHub.

## Development

Codex is used to develop, review and test new features. Live trades must never be executed during development or automated testing.


## Hardened development workflow

Use Python 3.12 on Linux or Windows 11. Account locks use pinned `portalocker==3.2.0`
(OS file locks on POSIX and Win32 locks on Windows). Install and validate offline
on Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -c requirements.lock
PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q
.venv/bin/ruff check *.py tests
.venv/bin/bandit -q *.py
```

On Windows 11, use PowerShell (no production credentials are needed for tests):

```powershell
py -3.12 -m venv .venv
$env:PYYAML_FORCE_LIBYAML = "0"
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c requirements.lock
$env:PYTHON_DOTENV_DISABLED = "1"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

For **Command Prompt**, set the same build option with
`set PYYAML_FORCE_LIBYAML=0` before installing development dependencies. PyYAML
6.0.3 (required by Bandit) has no Python 3.15 wheel. This supported option builds
its pure-Python implementation without Microsoft C++ Build Tools. It does not
disable dependency checks, TLS verification, or any trading safety control.
Only development tooling needs PyYAML; runtime dependencies already install normally.

The Windows dependency `pywin32` is installed automatically through portalocker;
the constraints snapshot pins version 312 on Windows only. Version 311 lacks
Python 3.15 wheels and can cause a misleading dependency-conflict error on that
interpreter. The version 312 wheel set includes CPython 3.12–3.15 on x86, x64 and
ARM64. Native Python 3.15 execution is covered by the Windows CI job, not by the
local Linux validation. Upgrade dependencies with the
commands above rather than copying a Linux virtual environment onto Windows.

`requirements.lock` records the exact packages used for validation; it is a constraints
snapshot, not a hash-verified artifact lock. The Binance SDK is pinned to 1.0.37.
Tests instantiate the actual SDK with synthetic credentials, intercept its API
transport, and block sockets and Requests HTTP calls. Do not inject production
credentials into test processes. `PYTHON_DOTENV_DISABLED=1` prevents loading `.env`.

The deployment policy lives in `safety.py`. Environment variables cannot silently
raise these limits:

| Setting | Automatic / scanner plan | Explicit manual override |
| --- | --- | --- |
| Exchange | Binance Spot only | Binance Spot only |
| Minimum 24-hour quote volume | 10,000,000 USDT | Bypassed; low volume displayed as a warning |
| Stop-distance and TP1 reward/risk | 1%-6% and at least 1.5R | User levels; strategy thresholds bypassed |
| Spread and nearby order-book depth | Enforced | Thresholds bypassed; warnings displayed |
| Planned trade risk | Up to 1% of available USDT, including reserved fees | Not limited to 1%; estimated stop loss shown before approval |
| Maximum position | 12 USDT including reserved buy commission | Same 12 USDT cap |
| Maximum active bot trades | One, including uncertain orders and residuals | Same one-trade limit |
| Daily loss threshold | 3 USDT, based on net realized bot P&L for the UTC day | Entry gate bypassed after explicit manual approval |
| Authorization | Fresh, single-use Telegram approval | Fresh, single-use **MANUAL RISK OVERRIDE** approval |

The original request specified 10 USDT; the user subsequently changed the position
cap to **12 USDT**. The user later explicitly authorized manual liquidity/strategy
and 1%/daily-loss overrides. Automatic policy remains unchanged. The entry point
and selected policy are persisted in each new trade's plan and approved bounds.

Choose **3 - Trading options → Manual** from a `/check` card, provide all four prices,
and review the labeled override proposal. Selecting Manual or submitting levels
does not itself place a buy. Trend/setup warnings do not block a manual plan.
The bot uses the available balance up to the 12 USDT position cap for manual
sizing instead of reducing size to the automatic risk budget. Automatic-only 5%
balance reserve and extra minimum-notional padding are also omitted for manual
plans; actual exchange minimums and commission-adjusted protective quantities
are still checked. Losses from manual
trades are still accounted for and therefore affect subsequent automatic entries.

Manual override is not permission to ignore exchange validity or lose ownership
tracking. Finite positive levels in valid order, entry-price drift bounds, symbol
support, commissions, exchange quantity/price/notional restrictions, account
identity, valid journals, sufficient funds, OCO protection and approval ceilings
remain mandatory. If two valid protective OCO legs cannot fit within 12 USDT,
the trade is still skipped with an explanation. The bot never rounds quantity up
to satisfy a minimum. Manual trades can exceed 1% risk and the automatic daily
threshold; stop-market slippage can also exceed the displayed estimated loss.

Entries use **price-capped LIMIT IOC orders**, rather than uncapped market buys, to
bound entry price and quantity. Partial/expired entry fills are recorded, reconciled,
and unwound conservatively. An IOC may expire without buying anything. Protective
exits remain two exchange-linked OCOs with `LIMIT_MAKER` targets and `STOP_LOSS`
market triggers. Original stops remain after TP1; there is no automatic breakeven
cancel/replace. `/check`, manual levels, and 15-minute rechecks remain available.

## Runtime and recovery contract

No production setup or integration was performed as part of this change. Future
explicitly authorized runtime operation requires separately configured Binance and
Telegram credentials. Never put credentials in Git, issue text, logs, or test files.
`USE_TESTNET` defaults to `true`; production mutations additionally require
`ALLOW_LIVE_TRADING=true`. The application verifies the authenticated account's
numeric UID and `SPOT` account type; it refuses to guess an identity if unavailable.

Records are stored under `runtime_state/<testnet|live>/<account UID>/trades/`.
Preserve this directory on durable storage. Every write intention and client ID is
saved with file synchronization and atomic replacement before submission.
POSIX additionally synchronizes the containing directory. Python does not support
directory fsync on Windows: process-crash recovery is supported, but equal
power-loss durability across filesystems/platforms is not guaranteed. On Windows,
atomic replacement retries known access/sharing/lock errors (WinError 5, 32, 33)
up to eight attempts with less than one second of total backoff. Persistent
failures still propagate: the bot never truncates the old journal or submits a
request whose intention could not be saved. This handles transient file access
contention; it does not override permanent permissions. Account operation
locks serialize entries, monitors, status reconciliation and kill operations on
this filesystem. The process lock is released automatically after process death;
never delete the lock file to unlock a running process. The live handle is held
until the entire protected scope ends, and is closed even if explicit unlocking
fails. Instance locks reject competitors immediately; operation locks wait for
other processes and retain reentrant thread serialization. These locks do not coordinate
separate machines or independent copies of the state directory.

Legacy `state/` records are **not automatically migrated or ignored**: their
presence blocks initialization until a separate manual reconciliation establishes
ownership and account/environment identity. Do not delete journals to unblock a
trade. Do not migrate an unresolved journal simply by renaming its directory.

Recovery never creates a new buy. It queries persisted IDs, verifies symbol, side,
quantity, order/list identity, protective levels and execution evidence, and resumes
monitoring confirmed protection. Confirmed uncovered bot inventory may be unwound.
Unknown acceptance, missing fills, invalid records and unconfirmed exits block
new entries. Writes with unknown acceptance are not blindly retried, even if a
subsequent lookup temporarily reports “not found.” Monitoring continues with
bounded backoff after transient failures; manual action may still be necessary.

Inventory calculations subtract verified sell fills and commissions from verified
buy fills. Emergency exits also preserve the entire pre-entry base-asset balance,
so unrelated holdings are not used to make an exit meet minimums. Use a dedicated
Spot account/subaccount with no concurrent manual orders, transfers, withdrawals
or other bots: external balance activity cannot be coordinated atomically.

`/status` reports available USDT, active journal states, remaining bot quantities,
and verified stop coverage (or explicitly UNKNOWN). `/cancel` invalidates pending
proposals, manual entry, untriggered entry watches and scheduled rechecks; it does **not** cancel submitted
orders or protective exits. Previously approved requests already in execution
retain their durable recovery obligations.

During separately authorized runtime operation, these CLI commands use the same
account operation lock and can run while Telegram is active:

```sh
.venv/bin/python exit_manager.py status
.venv/bin/python exit_manager.py kill --symbol FOOUSDT --confirm CLOSE_FOOUSDT
```

These are **authenticated runtime commands, not development smoke tests**. A kill
reconciles ownership, journals cancellation, then unwinds each confirmed released
leg before removing another stop. Cancel-and-sell cannot be atomic; a timeout,
market halt, missing evidence or exchange minimum can leave residual exposure.

Actual fill commissions and execution timestamps determine the journal's realized
P&L. Base/USDT commissions are accounted directly. Optional BNB fee accounting
records the actual BNB charged and values it using the last BNBUSDT aggregate trade
at or before the fill (no more than one second earlier). That exchange reference
is persisted with the fill; it is a defined USDT-equivalent valuation, **not a
claim to reproduce Binance's private fee-conversion price exactly**. Missing or
invalid reference data blocks completion/new entries, but does not delay placing
protective OCOs or unwinding confirmed inventory. Unsupported other fee assets
remain unresolved instead of being treated as zero. Dust below lot/notional
minimums and histories reaching the 1,000-fill response limit still block rather
than silently truncating ownership or accounting evidence.

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the second review and remaining
risks, and [TEST_RESULTS.md](TEST_RESULTS.md) for the regression mapping and results.


## Binance timestamp error -1021

A successful Windows/domain NTP resync does not prove the host clock agrees with
Binance. Before authenticated access, the client samples the public Spot server
time and sets the SDK's timestamp offset using the lowest-round-trip-time of
three samples. The offset is cached for up to 30 seconds and invalidated on a
wall-clock jump. This changes neither Windows time nor Binance's recvWindow.
Samples taking over one second, malformed timestamps and clock jumps during
sampling fail closed rather than guessing an offset.

Account and order-status reads can resynchronize and retry once on -1021.
Mutating requests are never automatically retried by this mechanism. An explicit
timestamp rejection of a buy/OCO is journaled as rejected, so recovery cannot
silently resubmit it; any later buy still needs a fresh approval. An unavailable
public time service blocks authenticated initialization. No API key rotation is
needed solely to correct a timestamp error. Persistent failures still require
network/latency investigation; do not bypass timestamp checks or enlarge the
request window to hide them.

## Double-click LIVE launcher on Windows

After installing dependencies and configuring this checkout's local `.env`,
double-click **Start_Live_Bot.bat** in the project folder. Explorer opens it in
Command Prompt. It switches to its own directory (including paths with spaces),
activates `.venv\Scripts\activate.bat`, clears `PYTHON_DOTENV_DISABLED`, sets
`USE_TESTNET=false`, `ALLOW_LIVE_TRADING=true`, and `AUTO_SCAN=true`, then starts
`telegram_assistant.py` with unbuffered output. No keys/tokens are embedded or
printed, and the script does not install packages, pull Git changes or merge.

This is explicitly a real-money launcher. Telegram approval is still required for
new buys; startup recovery can manage existing journaled positions. Keep the
window open while running and use Ctrl+C to stop. Existing exchange orders remain
at Binance. The window pauses after normal exit or startup failure so the result
can be read. Missing `.venv`, bot file, or `.env` produces an actionable error
instead of falling back to a different Python installation or checkout.

Stop the old instance before launching. The account process lock still prevents
duplicate instances. For desktop access, create a shortcut pointing to this batch
file; do not move the batch file away from its checkout.


## Candidate visibility and BNB Spot fees

Add these non-secret settings to this checkout's existing `.env` and restart:

```dotenv
ALLOW_BNB_FEES=true
SCAN_CANDIDATE_NOTIFICATIONS=true
```

`ALLOW_BNB_FEES` defaults to false and enables the accounting support described
above. It does not change Binance account settings or buy BNB. Keep the Binance
Spot BNB fee-payment option enabled and sufficient BNB in the account if you want
the exchange to apply its discount. The application reads the account/symbol
commission endpoint: there is no hard-coded 25% discount or user-entered fee rate.
Only standard commission receives the returned multiplier; tax/special components
are not discounted. Sizing reserves the full undiscounted entry/stop taker and
target maker rates so a BNB shortfall can fall back safely. Actual execution
commissions, not the advertised estimate, determine journal accounting.

A mixed BNB/USDT fallback across fills is supported. The active trade's BNB opt-in
is journaled, so historical fee accounting can finish after restart even if the
local flag later changes. Trading **BNBUSDT itself while BNB discounts are
active remains blocked** pending a dedicated reserve for fees charged in the same
base asset as the position. The bot will explain that edge case rather than risk
selling unrelated BNB or incorrectly declaring inventory flat.

`SCAN_CANDIDATE_NOTIFICATIONS` defaults to true. Every scan names all candidate
symbols in the console and reports each outcome: proposal sent, cooldown,
validation refusal, API failure or Telegram delivery failure. Nonempty scans also
send Telegram summaries with these details; set the flag to false for console-only
summaries. Only successfully registered proposals start their alert cooldown.
A candidate is not an order, and a proposal still requires your approval. No
symbol can be recovered from an old count-only log line without other records.


## Approval and actual execution outcomes

Telegram approval authorizes an entry attempt; it does not establish a fill.
Entries are price-capped LIMIT IOC orders. An accepted IOC can expire immediately
with executed quantity zero if it cannot execute within its limit. No asset is
acquired in that case, no protective exits are needed for that attempt, and the
bot does not automatically retry the buy or change its price cap.

The proposal acknowledgement now explicitly says execution is not yet confirmed.
Subsequent messages distinguish NO BUY FILLED (including EXPIRED/REJECTED and
executed quantity), partial-entry reconciliation, filled entries with verified
remaining stop coverage, already-executed exits and incomplete coverage. Final
approval-bound failures before submission are explicitly reported as NOT SUBMITTED.

`/status` includes the latest saved entry's buy status, executed quantity, journal
state and known Binance order ID even when there are no active positions. This
is the latest journaled attempt, not necessarily the latest clicked proposal:
validation failure before journaling may not create a new record. Use the order
ID to compare with Binance history. Older rejected records may lack an API error
code; new rejections persist only sanitized numeric API/HTTP metadata.

A successful protection notification is a verified snapshot, not a guarantee that
orders cannot subsequently fill, be canceled externally, or become unavailable.
Unknown outcomes are never reported as zero fill merely because the balance looks
unchanged. Confirmed zero-fill notifications do not offer an automatic retry.


## Previous entry UI (superseded by v1.1.0 modes)

Send `/check PARTI`, then choose **3 - Manual override**:

- **Enter now:** type only `STOP TP1 TP2`. The proposal uses the current best ask
  plus 0.2%, rounded up to one valid tick, as the **maximum buy price**. Approval
  freezes that cap and the displayed maximum quantity, cost and estimated loss.
  A higher ask requires a fresh proposal; lower prices cannot increase quantity.
- **Now preset: SL -3%, TP +5%/+8%:** no level typing. Levels are based on a fresh
  ask. These are optional manual levels, not a scanner recommendation. Review the
  resulting prices, warnings and quantity, then explicitly approve or reject.
- **Wait at my price:** type `ENTRY STOP TP1 TP2`. After approval the bot watches
  locally for up to 15 minutes, polling roughly every 2 seconds plus API latency.
  It submits one price-capped IOC when the ask reaches your entry and observed
  bid/last remain above the stop. It places **no resting Binance buy** while waiting.

For example, with PARTI at 0.0485 and levels
`0.0480 0.0475 0.0490 0.0500`, Wait watches for ask <= 0.0480.
An observed drop directly to 0.0465 cancels without buying. A stop breach between
polls or after the last preflight check can still be missed. The watch therefore
cannot guarantee that no fill ever occurs below the stop; confirmed fills retain
existing OCO validation, emergency exit and durable recovery. Market exits can
slip, fail or leave untradeable dust.

`/status` identifies an armed watch; `/cancel` cancels it under the account lock.
Restarting the bot or a failed monitoring read cancels an **untriggered** watch,
because price movements during the interruption are unknown. Fresh approval is
required. Submitted or uncertain orders are always reconciled, never canceled
locally or retried as a new buy. Watches consume the one-active-trade slot. A
zero-fill IOC is terminal and requires a new proposal; entries are never chased.
The original automatic scanner entry behavior is retained.

The 12 USDT cap including reserved fees, two valid protective OCO quantities and
exchange minimums remain mandatory. Entry mode does not add exceptions to the
already displayed manual risk override. A proposal expires after 5 minutes;
an approved watch has its separate fixed 15-minute lifetime, which is not renewed.

## View scanner candidates while a position is active

Scanner summaries include candidate Entry, SL, TP1 and TP2 reference levels even
when a position, cooldown or another validation prevents an executable proposal.
These are labeled previews, with the blocking reason; they cannot bypass the
one-active/unresolved-trade limit or substitute for exchange-rounded order levels.

Send **SHOW PR** (case insensitive) or **/showpr** to see the most recent proposal
or candidate snapshot saved during this bot process. It includes the saved UTC
time and is read-only: no order, approval button or renewed authorization is
created. An empty scan preserves the previous snapshot. Restart clears the
snapshot; `/check COIN` or a new scan supplies a fresh one. `/cancel` can invalidate
a proposal while its historical snapshot remains viewable. Prices may be stale;
use `/check COIN` and fresh approval when you can open another position.

## Presentation and version

The scanner and assistant share a compact identity block: application version
`1.0.0` (the first explicitly tracked display version), “Concept & Developed by
Fazil,” and the GitHub repository URL. This display version is maintained in
`branding.py`; it is not a claim that a Git release/tag has been published.
Startup output, proposals, checks, scanner summaries and saved previews use this
identity. Telegram uses a blue ticker marker and a separate status marker
(amber for waiting/cooldown, red for blocked/canceled). Text labels always explain
the state. Telegram does not support arbitrary text colors. Markers are visual
cues, not safety ratings or permission to trade. Order logic is unchanged.

The assistant starts with a compact `TRBT | The Trading Bot for Binance Spot`
sequence: scanner module readiness, verified exchange initialization, instance
lock, recovery pass, then Telegram readiness. Each success line follows the
corresponding operation; no artificial loading delays are added. Scanner module
readiness does not mean the first market scan is finished, and a completed
recovery pass can still report unresolved records requiring continued monitoring.

## Compact proposal cards and help

The main Telegram proposal shows ticker, manual/automated type, entry mode, maximum
buy price, SL, TP1/TP2, size/cost, estimated total profit at both targets and
estimated loss at the initial stop. It retains the live-money label, manual risk
exceptions, hard limits, original-stop behavior and slippage warning. **Details**
opens the full original assumptions, scanner warnings, BNB fee information and
specific overridden checks. Viewing Details never consumes or renews approval and
never submits an order; expired/canceled proposals cannot be revived this way.

The profit estimate credits only the two fee-reserved exit quantities at their
target prices, reserves full exit fees and subtracts the entire entry cost with
its fee reserve. This conservative scenario ignores leftover/dust proceeds and
BNB discount savings. It is not realized P&L or a guaranteed profit; actual fills,
fees, partial exits and slippage differ. The stop estimate describes the initial
full-position stop scenario; TP1 followed by a stop has a different outcome.

`/help` and `/start` list every supported command: `/check` (including direct
coin arguments and `/check_ticker` / `/check-ticker` aliases), `/status`, `/showpr`
/ `SHOW PR`, `/cancel`, `/help` and `/start`. They also explain proposal buttons
and manual-entry modes. `/cancel` does not close active positions or cancel OCOs.

## Exit results in Telegram

Verified sells produce a short result message. A partial exit reports realized
profit/loss so far and remaining bot quantity; **TRADE CLOSED** is sent only after
all bot inventory is zero, all orders terminal, and execution/commission accounting
is verified. Net P&L is shown in USDT and as a percentage of **actual total entry
cost including entry fees**, not account balance or the proposal's estimate.
Targets, stops and emergency closes use actual fills. Partial BNB results can stay
pending valuation; final BNB results identify the existing fill-time USDT reference
method, not an exact Binance internal conversion.

Notifications are saved before delivery, with failed Telegram deliveries retried
about every 30 seconds while the assistant runs and after restart. A completed
trade stays closed even if Telegram is unavailable. Delivery is at least once:
a crash after Telegram accepts a message but before its acknowledgement is saved
can produce a duplicate with the same trade reference. Newer execution snapshots
supersede undelivered older ones. Reporting never retries or creates an order.

Candidate Telegram alerts now use one compact card: “Potential Candidate Detected,”
ticker, current status, rejection/cooldown reason, and Entry/SL/TP1/TP2 on separate
lines. They retain the live/testnet label, preview-only notice and market warning,
with `TRBT v1.0.0 | Fazil` as a short footer. The large branding/Git header and
repeated candidate/status headings are omitted from these cards. Full identity
remains in startup output and proposal Details. SHOW PR uses the same compact
candidate snapshot. Console diagnostics and execution policy are unchanged.

## Startup-only identity and execution narration

The assistant's startup welcome contains the full branding, Git URL and live/testnet
mode notice. Routine proposals, Details, check cards, candidate alerts, /status and
exit reports omit repeated identity/mode headers. Console startup still identifies
the environment. The standalone alert-only scanner also keeps branding in its
startup announcement rather than every candidate. Existing Telegram history is
not edited; these formats apply to new messages after updating and restarting.

Exit reports identify verified Take Profit-1, Take Profit-2 or stop-loss fills,
and distinguish partial fills from completed order execution. Confirmed recovery
market sells are never relabeled as target hits. New recovery intentions save
whether they were an explicit position close or an exit of unprotected inventory;
the latter is reported as an emergency exit. Older records without that reason
retain a generic recovery-sale label where the cause is unknown. The old generic
“Emergency exit reconciled” Telegram alert is removed in favor of the verified
result report, preserving pending fee valuation and remaining-position information.
Pending, undelivered result messages adopt the new format on retry.

Startup wording now uses **Live Spot** or **Spot Testnet** as the environment name,
including the welcome, Windows window title and console initialization. The
“REAL MONEY” / “fake money” wording is retired. The welcome reports TRBT online,
scan cadence, required order approval and the primary commands. Launcher/startup
messages distinguish module readiness, verified exchange connection, account lock,
position reconciliation and Telegram availability. No connection, scan, order or
configuration logic was changed; these are presentation changes only.

The approved startup status is now shared by the Telegram welcome and final
console readiness message:

```text
Connected to Binance Spot
Market scanning: Active
Trade execution: Requires Telegram approval
```

Testnet explicitly says “Binance Spot Testnet.” With automatic scanning off, the
status instead says “Disabled — use /check.” Connection text is reached only
after exchange initialization; these labels do not change trade authorization.

The manual-entry screen now starts with a choice rather than asking for four
numbers immediately: **Use current price**, **Wait for my price**, or **Use preset
levels (-3%, +5%, +8%)**. After selecting a custom mode, a short prompt explains
STOP and TP1/TP2 and requests only the three or four prices that mode needs, with
an explicitly illustrative format example. Four-number direct entry remains
compatible with the existing watch flow. Presets remain human-approved example
levels, not a trading recommendation. Final manual-risk disclosures and explicit
approval are preserved; buttons alone never authorize a purchase.
