# Offline validation results

Environment: Linux, Python 3.12.14, python-binance 1.0.37. The original 13 scenarios
were retained and migrated from an invented exchange interface to the actual SDK
transport simulator; the old unconditional fake `binance` module was removed.
Policy assertions now enforce the requested 12 USDT / 1% / one trade / 3 USDT limits.
Tests for changed behavior assert verified closure or durable uncertainty rather
than assuming an exception is the only correct recovery outcome.

## Results

- **102 passed; 0 failed; 0 skipped; 0 expected failures.**
- Four upstream SDK/websocket deprecation warnings; none suppressed.
- Coverage: 70% overall; exit engine 87%, trade planning/execution 81%, SDK adapter
  90%, safety helpers 93%, scanner 48%, Telegram assistant 45%.
- Bandit 1.9.4: zero reported findings across application modules.
- Ruff lint and formatting, `pip check`, and `git diff --check`: passed.
- JUnit report generated at `/tmp/binance-security-tests.xml` for the final run.

## Commands

```sh
PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q \
  --cov=entry_scanner --cov=exchange --cov=exit_manager --cov=safety \
  --cov=telegram_assistant --cov=trade_assistant --cov-report=term \
  --junitxml=/tmp/binance-security-tests.xml
.venv/bin/python -m pip check
.venv/bin/ruff check *.py tests
.venv/bin/ruff format --check *.py tests
.venv/bin/bandit -q *.py
git diff --check
```

The suite blocks socket connections and Requests network operations. SDK instances
use synthetic test-only credentials and `ping=False`. All application state lives
under pytest temporary directories. No production account, credential, trade or
Telegram message is used. Exchange actions in assertions refer only to the simulator.

## Regression mapping

| Original finding | Regression evidence |
| --- | --- |
| 1. SDK mismatch | `test_01_actual_sdk_contract`, IOC transport contract |
| 2. Unprotected incomplete buy | `test_02_all_buy_outcomes_accounted` across partial, expired, rejected, new and accepted-timeout states |
| 3. Unrelated holdings sold | `test_03_*`, nonbot withdrawal, base-buy-fee residual, TP1 then kill |
| 4. Restart gaps | `test_06_*`, 22 journal-write crash checkpoints, stale monitor, concurrent approvals, crash-released/nested locks |
| 5. Emergency uncertainty | `test_04_*`, `test_05_*`, unconfirmed and partially filled emergency exit tests |
| 6. Account/environment confusion | Account namespace, UID rotation, wrong OCO identity, forged protective level tests |
| 7. Daily loss omissions | `test_07_*`, missing closed accounting/fills, actual commission math, execution-day assignment, prospective budget |
| 8. Post-buy minimum failures | `test_08_*`, fee/rounding, zero-disabled market steps, dynamic averaging windows, capacity and volume tests |
| 9. Halted monitoring | `test_09_*`, more than three errors followed by recovery, partial/canceled exit evidence |
| 10. Telegram / disclosure | `test_10_*`, manual check/trading/recheck, in-flight cancellation, replay/expiry/authentication, bounds and synthetic credential redaction |

Additional controls cover IOC price ceilings, shutdown entry refusal, managed
worker cleanup, CLI kill availability during Telegram operation, and liquidation
of one released OCO leg before canceling another. Scanner checks cover indicators,
closed candles, insufficient/stale/malformed history and the fixed volume floor.

## Interpretation

Passing results demonstrate the simulated invariants, not real matching-engine
acceptance. Coverage remains incomplete, particularly scanner strategy branches
and the complete Telegram polling loop. Four upstream deprecation warnings from
the SDK/websocket integration remain; none are suppressed. There is no skipped or
expected-failure test used to hide a failing safety check. The simulator does not
implement every Binance API condition. See SECURITY_REVIEW.md for unresolved risks.


## Windows compatibility follow-up

The complete suite now has **113 passing tests** on the local Linux/Python 3.12
execution environment, with three upstream deprecation warnings. The new locking
module has 100% statement coverage locally; overall coverage remains 70%. Lint,
formatting, dependency consistency, Bandit, and diff checks passed. The final JUnit
report is `/tmp/binance-windows-compatibility-tests.xml`. The SDK's unused
websocket event-loop creation is replaced with an inert object in REST test
fixtures: Windows asyncio can otherwise create loopback sockets during construction,
which conflicts with the intentionally unchanged socket/HTTP network guard.

Eleven cross-platform regression cases cover imports without Unix-only application
modules; journal replacement when `O_DIRECTORY` is absent; file synchronization
before replacement; propagation of real IO failures; simultaneous subprocess
instance prevention; operation-lock blocking for the entire scope; non-inheritable
handles; lock release after forced process death for both lock types; failure-path
handle cleanup; and the exact blocking/nonblocking lock flags. Existing nested,
concurrent-approval, crash-recovery and kill-while-running tests remain enabled.
Subprocess probes use a synthetic account context, disable dotenv loading, whitelist
only non-secret platform bootstrap variables and forbid network connections.

The Windows pywin32 311 wheel for CPython 3.12/AMD64 was verified available by a
TLS-verified package download; it was not executed on Linux. A GitHub Actions matrix
runs the full suite and lint/format checks on `windows-latest` and `ubuntu-latest`.
Native Windows results remain pending CI execution; the local Linux result must not
be interpreted as a completed native Windows run. No live integration was performed.


## Windows dependency-resolution correction

The original `pywin32==311` Windows-only constraint did not provide CPython 3.15
wheels. It was replaced with `pywin32==312`, which remains compatible with
portalocker's `pywin32>=226` requirement and publishes wheels for CPython 3.15 as
well as the original Python 3.12 target. Verification uses official PyPI metadata
and TLS-verified cross-platform wheel downloads without executing Windows code.
Windows CI now covers both Python 3.12 and 3.15; Linux CI remains on 3.12. Native
Windows results must be checked in CI; this dependency correction does not claim
that Python 3.15 has been executed in the Linux development workspace.

After this correction, the complete local suite passes (113 tests, three upstream
warnings), with lint, formatting, Bandit and dependency checks passing. The Python
3.15 Windows CI installation step is the dependency-resolution regression check: it
would fail with the original 311 pin before the test suite starts.


## Compiler-free Windows development installation

PyYAML 6.0.3 has no CPython 3.15 wheel. The development installation therefore
attempted to compile its optional extension, failing on Windows without Visual
C++ Build Tools. PyYAML's supported `PYYAML_FORCE_LIBYAML=0` option builds the
pure-Python implementation instead. It is now configured in CI and documented
for PowerShell and Command Prompt. Runtime requirements are unchanged.

The option was verified against the official checksum-verified PyYAML source and
used to build `pyyaml-6.0.3-py3-none-any.whl` with normal pip build isolation and
TLS verification. That wheel was installed into the development environment to
run the complete offline suite and Bandit with `yaml.__with_libyaml__ == False`.
Native Windows CI results remain separate from this local Linux verification.


## Windows journal replacement denial

A native Windows user run completed 112 tests successfully and failed
`test_04_every_submission_has_durable_ids` with `PermissionError: [WinError 5]`
from `os.replace`. The output establishes a denied replacement but does not
identify the holder or distinguish transient contention from permanent ACL issues.

The journal now retries only Windows errors 5 (access denied), 32 (sharing
violation), and 33 (lock violation). Eight attempts reuse the same already-flushed
temporary file, with 0.975 seconds total backoff. The old file is never truncated,
deleted or replaced through a non-atomic fallback. Exhaustion propagates failure;
other errors and POSIX failures are not retried.

Ten added regression cases inject the native failure codes, verify successful
recovery and unchanged old bytes while denied, bound persistent failures, ensure
no buy is submitted if its intention cannot be saved, and exercise full
buy/OCO/kill journaling with transient denial on every replacement. The original
durable-ID test and its assertions are unchanged. All **123 tests pass locally**
with three upstream warnings; lint, formatting, Bandit, dependency and whitespace
checks pass. Native Windows rerun is required to confirm the user's actual case.
No real trades, production credentials or external exchange connections are used.


## Binance server-clock synchronization

The user reported live read-only status failures with -1021 after Windows domain
NTP resynchronization. No live account was accessed to investigate. Startup now
uses the public Spot time endpoint and the pinned SDK's `timestamp_offset` to
correct signing time; account and order-status reads permit one forced resync on
-1021. recvWindow, operating-system time and trade limits remain unchanged.

Seventeen added offline regressions verify ahead/behind clocks with the actual
SDK signer, latency-corrected sample selection, cache expiry and domain-clock
jumps, malformed/slow/unavailable time responses, signed-read blocking on sync
failure, bounded read retries, no retry for other API errors, and strict exclusion
of buy/sell/cancel methods from the read-retry helper. A timestamp-rejected buy is
persisted as rejected and never automatically resubmitted. The complete local
suite now has **145 passing tests** with three upstream warnings. Native Windows
live-account behavior remains unverified until the user retries read-only status.


## Explicit manual-risk override

The user explicitly authorized a manual profile that bypasses volume, spread/
depth, trend/setup, reward/risk, stop-distance, 1% sizing and the daily-loss gate.
Automatic strategy and risk gates remain unchanged. The 12 USDT total position
cap, one active/unresolved trade, identity/ownership, exchange validity, complete
commission evidence, protective OCOs and bounded fresh approval remain mandatory.
Automatic 5% balance/notional buffers are not exchange requirements and are
omitted for manual plans, while actual exchange limits and fee reserves remain.

The GUI marks Manual as an override and displays the exceptions and measured
violations before approval. The explicit flag is preserved through proposal
refresh, final pre-buy revalidation and journaled approval bounds. The startup
banner separates automatic limits from the manual exception. A manual label or
nonboolean flag cannot activate the profile, and automatic approval cannot be
silently escalated to manual.

Twenty-six additional offline cases cover the screenshot's low-volume rejection,
strategy stops/R:R, spread/depth, a 33 USDT balance, exceeded daily losses, full
manual execution revalidation, mandatory exchange minimums, insufficient fee
valuation, invalid/nonfinite data, active-position gating, approval expiry/mode
binding, disclosure, manual UI entry and quantity/cost/risk ceilings. Manual
losses are verified to remain in accounting and block later automatic entries.
The old low-volume test still asserts rejection without the explicit override;
its name was clarified rather than weakening its assertion.

The complete local suite passes **171 tests** with three upstream warnings.
Lint, formatting, Bandit, dependency and whitespace checks pass. All exchange
operations were simulated with network blocking; no live trade was executed.
Native Windows validation of this new manual profile remains pending CI/user
verification. Allowing human overrides increases accepted trading risk and is
not evidence that low-liquidity execution or losses are controlled.

## Windows double-click LIVE launcher

`Start_Live_Bot.bat` starts from its own checkout, activates its Windows virtual
environment, restores dotenv loading, explicitly selects LIVE/auto-scan mode and
uses that environment's Python. It preserves the bot's exit code and keeps the
console open for failures. Git attributes enforce CRLF checkout for batch files.

Three native Windows CMD tests use a fresh temporary virtual environment and a
harmless replacement `telegram_assistant.py`; they never import or launch the
real bot. They verify directory-with-spaces handling from a different working
directory, exact effective mode variables, dotenv activation, interpreter and
virtual-environment selection, exit-code preservation and missing-environment/
configuration failures. Child processes receive only non-secret OS bootstrap
variables and synthetic mode settings, with no production credentials.

Local result: **171 passed, 3 skipped**, with three upstream warnings. These three
skips are explicitly Windows-only CMD execution tests, unsupported on the Linux
workspace. They run in the existing Windows CI matrix; native results remain
unverified here. Lint, formatting, Bandit, dependency and whitespace checks pass.


## Candidate summaries and opt-in BNB commission support

The complete local suite now reports **195 passed, 3 skipped**, with three upstream
warnings. The three skips remain native Windows CMD launcher tests. Lint,
formatting, Bandit, dependency and whitespace checks pass. No production
credentials, live Binance account, real trade or Telegram messaging was used.

New cases cover opt-in gating; account-reported 25% standard-fee reduction;
undiscounted fallback reserves; undiscounted tax/special fees; higher maker target
rates; actual charged BNB without double-discounting; BNB exhaustion and mixed
USDT fallback; historical fill-time prices rather than reconciliation prices;
missing/invalid/truncated/duplicate conversion history; durable references across
restart; corrupted closed evidence; partial-buy liquidation despite missing fee
valuation; and the explicitly unsupported BNBUSDT inventory edge case.

Candidate tests cover one candidate with zero proposals, per-symbol exception
isolation, cooldown explanations, real delivery result reporting, console-only
configuration, bounded multi-message summaries and credential-safe diagnostics.
The simulator uses the actual SDK adapter and realistic BNB quantity/balance
changes. BNB valuation is a documented reference-price convention, not a claim of
exact agreement with Binance's internal conversion. The implementation uses the
public commission FAQ as the rate-semantics reference:
https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md


## Explicit entry and IOC zero-fill outcomes

The user confirmed a PARTIUSDT LIMIT IOC buy expired with executed quantity zero.
The old UI acknowledged approval but did not report that terminal zero-fill
outcome, while `/status` only reported no active positions. This was a reporting
gap; it did not establish a filled position or justify an automatic buy retry.

Seven new offline regressions cover zero-fill expiry with unchanged balances,
no OCO placement and no retry; persisted/reported sanitized rejection codes;
protection messages sent only after actual list/coverage verification; partial
entry liquidation distinguished from no fill; unknown buys never reported as
zero fill; final approval-bound failures reported before submission; and exits
already completed before a protection notification. Status retains the latest
saved entry result when the journal is closed. Pricing, approval bounds and
no-blind-retry policy are unchanged.

Final local result: **202 passed, 3 skipped**, with three upstream warnings. Native
Windows launcher skips remain unchanged. Lint, formatting, Bandit, dependency and
whitespace checks pass. No live account was accessed and no real trade was
executed by the implementing agent. The native live expiry evidence came from
the user's provided history/status, not an agent-side account connection.

## Telegram now/watch entry redesign

Added 40 offline regressions through the actual python-binance 1.0.37 transport
interface. They cover ask-based capped immediate entry, no quantity increase on
price improvement, watch triggering/expiry/cancellation, restart and read-failure
cancellation, stop gaps before validation and after execution, partial/expired/
rejected/unknown/accepted-timeout buys, 12 crash checkpoints, immutable watch
authorization, exchange minimums, no unrelated liquidation, persistent slot
blocking, and Telegram three-number/preset flows requiring explicit approval.

A post-fill gap regression exposed local OCO validation failures being classified
as uncertain submissions. A dedicated pre-transport exception now marks only
those known-unsent requests rejected, allowing ownership-limited recovery;
transport exceptions retain the original no-blind-retry reconciliation rule.

Local Linux Python 3.12 result: **242 passed, 3 skipped**, three upstream
websockets deprecation warnings. The three native Windows launcher checks remain
skipped on Linux. Ruff lint/format, Bandit, pip check and diff whitespace checks
pass. No production credentials, live Binance connections or real orders were
used. Tests do not establish production matching-engine behavior or native
Windows success; Windows CI/user verification remains necessary.

## Candidate previews and SHOW PR

Ten additional regressions verify that active-trade blocks and cooldowns retain
all four scanner levels without new orders/approvals, invalid levels are explicit,
SHOW PR and /showpr are authorized read-only commands, canceled proposals cannot
be reactivated by viewing, empty scans retain the last snapshot, and a fresh
process reports no saved preview. The cache is intentionally process-local and
contains no executable authorization. Existing message splitting and notification
preferences remain enforced.

Local Linux result: **252 passed, 3 native Windows skips**, three upstream warnings.
Ruff, formatting, Bandit, dependency and whitespace checks pass. No live account
or real trades were used. The previous user-reported Windows run (245 passed)
predates these ten new tests; this update still requires Windows verification.

## Compact product identity and status markers

Presentation-only update adds a shared display version, Fazil attribution and
repository URL, with restrained ticker/status markers in Telegram. No order,
approval, risk or account logic changed. Existing full suite: **252 passed,
3 native Windows skips**, three upstream warnings. Ruff/format, Bandit, dependency
and whitespace checks pass. No live account access or trades occurred.

Startup presentation follow-up adds a five-step TRBT initialization display.
Completion messages follow exchange verification, lock acquisition, recovery and
Telegram validation; scanner readiness is distinguished from scan completion.
No fake delay or additional exchange request was added. Full suite remains
**252 passed, 3 Windows-only skips**, with three upstream warnings.

## Compact approvals, fee-reserved profit estimate and complete help

Eight additional offline checks cover conservative total-target profit (all entry
cost and full reserved exit fees), negative-profit display, Details buttons that
do not consume/renew approval, expired/canceled/unauthorized Details requests,
and complete /help and /start command guides with the /cancel boundary. Existing
BNB/manual-disclosure assertions now verify the full Details disclosure as well
as visible manual exceptions on the compact approval card; no execution checks
or financial limits were weakened.

Full local result: **260 passed, 3 native Windows skips**, three upstream warnings.
Ruff/format, Bandit, dependency and whitespace checks pass. No live accounts or
real trades were used. The new profit display is a conservative forecast, not
verified realized P&L; that accounting remains exchange-execution based.

## Verified exit-result reporting

Eleven additional offline cases cover target/stop net results against simulated
actual USDT movements, percentage denominator based on actual entry cost, partial
then final exits, mixed target/stop outcomes, zero fills, failed delivery/restart
retry, crash after closure before delivery, Telegram failure preserving closure,
missing BNB references withholding final P&L, BNB reference labels and lock-order
safety. A notification worker handles pending closed journals without resubmitting
exchange requests. Result messages are at least once; a delivery/acknowledgement
crash can duplicate a message, identified by the same trade reference.

Final combined local result: **271 passed, 3 native Windows skips**, three upstream
websockets warnings. Ruff/format, Bandit, dependency and whitespace checks pass.
No live Binance connection, production credentials or real trades were used.

Compact candidate-card follow-up: existing visibility regressions now assert the
requested ticker/status/rejection/one-level-per-line Telegram layout, absence of
the large Git header, preserved market warning, no actionable approval while a
trade is active, notification preferences, chunking and SHOW PR behavior. Full
suite remains **271 passed, 3 native Windows skips**, three upstream warnings.
Ruff/format, Bandit and whitespace checks pass. No live account access or trades.

## Startup-only branding and evidence-based exit narration

Six new regressions cover identity/mode text confined to startup, specific TP1/
TP2 event names, emergency recovery not reported as a target fill, explicit close
not described as emergency, partial target execution not described as complete,
and pending pre-update notifications adopting the new rendering. Existing
verified net P&L, closure, retry, account-lock and ownership tests remain enforced.
No production credentials, live exchange access or real trades were used.

Final local result: **277 passed, 3 native Windows skips**, three upstream warnings.
Ruff/format, Bandit, dependency and whitespace checks pass.

Professional startup wording follow-up replaces “LIVE - REAL MONEY” with “Live
Spot” and testnet wording with “Spot Testnet.” The welcome lists environment,
scan cadence, mandatory approval and commands; the Windows launcher and console
use consistent wording. Existing startup assertions verify the new environment
and approval disclosure and absence of the retired terms. All trading flags,
limits, locks and recovery behavior remain unchanged.

Full local suite: **277 passed, 3 native Windows skips**, three upstream warnings.
Updated startup assertions also pass in the focused compact-proposal suite.
Ruff/format, Bandit and whitespace checks pass. No live account access or trades.

Approved startup status follow-up shares the exact three-line connection,
scanning and Telegram-approval wording between welcome and console readiness.
The existing startup regression also checks the auto-scan disabled and explicit
Testnet variants. Full local suite: **277 passed, 3 native Windows skips**, three
upstream warnings. No live account access or trades; trading behavior unchanged.

Manual-entry clarity follow-up replaces the initial four-number instruction with
a choice of current-price, wait-at-price or preset modes. Mode-specific prompts
explain stop-loss and profit targets and show an example format. The existing
fast-flow test now checks the clearer labels and three-price prompt while still
requiring a pending proposal and zero exchange writes before approval. Risk
exceptions, hard limits, watch behavior and execution logic are unchanged.

Full local suite remains **277 passed, 3 native Windows skips**, three upstream
warnings; the updated focused fast-flow regression also passes. Ruff/format,
Bandit and whitespace checks pass. No live account access or real trades.
