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
