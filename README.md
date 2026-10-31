# Binance Spot Trading Bot by IamPache

A Python-based Binance Spot Trading Assistant with Telegram integration.

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

| Setting | Required value |
| --- | --- |
| Exchange | Binance Spot only |
| Minimum 24-hour quote volume | 10,000,000 USDT, including manual entries |
| Planned trade risk | Up to 1% of available USDT, including reserved commissions |
| Maximum position | 12 USDT including reserved buy commission |
| Maximum open bot trades | One, including uncertain orders and residual holdings |
| Daily loss threshold | 3 USDT, based on net realized bot P&L for the UTC day |
| Entry authorization | Fresh, single-use Telegram approval |

The original request specified 10 USDT; the user subsequently changed the position
cap to **12 USDT**. No other requested limit was raised. If the remaining risk
budget or the 12 USDT cap cannot support both OCO legs after fees and rounding,
the trade is skipped with an explanation. The bot never rounds quantity up to
satisfy a minimum. Stop-market losses can exceed planned risk.

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
proposals, manual entry and scheduled rechecks; it does **not** cancel submitted
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

Exact base/USDT commissions and exchange execution timestamps determine realized
P&L. BNB-discount configurations are rejected before entry because this version
cannot establish an exact USDT cost basis for third-asset fees. Unexpected
third-asset fees quarantine the record; they are never treated as zero. Dust below
lot/notional minimums remains explicitly tracked and blocks new entries until
separately reconciled. Histories reaching the 1,000-fill response limit also block
rather than silently truncating accounting.

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the second review and remaining
risks, and [TEST_RESULTS.md](TEST_RESULTS.md) for the regression mapping and results.
