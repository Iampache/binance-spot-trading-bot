"""Credential-safe diagnostics, default entry caps and fixed risk policy."""

import logging
import re

MIN_QUOTE_VOLUME = 10_000_000
RISK_PCT = 1
MAX_POSITION_USDT = 12
MAX_OPEN_TRADES = 1
MAX_DAILY_LOSS_USD = 3


RECONCILIATION_REASONS = {
    "INVALID_NUMERIC_VALUE": "A numeric value is invalid or non-finite; validation cannot complete.",
    "INVALID_ROUNDING_RULE": "A quantity/price rounding rule is invalid; validation cannot complete.",
    "SPOT_ORDER_SUPPORT_MISSING": "The pair lacks required active USDT Spot/OCO order support.",
    "NOTIONAL_RULE_MISSING": "The exchange minimum-order-value rule is unavailable; validation is pending.",
    "ORDER_QUANTITY_INVALID": "Order quantity or reference price is not positive; validation is pending.",
    "ORDER_LOT_SIZE": "Order quantity does not satisfy the exchange lot size, step or quantity limits.",
    "ORDER_MIN_NOTIONAL": "Order value is below the exchange minimum, including any required entry buffer.",
    "ORDER_MAX_NOTIONAL": "Order value exceeds the exchange maximum; validation is pending.",
    "EXIT_DUST": "Remaining bot quantity is below the exchange quantity step; a market exit cannot be submitted.",
    "BNB_CANDLE_NOT_CLOSED": "Waiting for the BNB fee's containing minute to close; final P&L is pending.",
    "BNB_CANDLE_INVALID": "Historical BNB candle failed validation; final P&L is pending.",
    "BNB_REFERENCE_MISSING": "Historical BNB fee reference unavailable; final P&L is pending.",
    "BNB_REFERENCE_TRUNCATED": "Historical BNB fee response is truncated; final P&L is pending.",
    "BNB_REFERENCE_INVALID": "Historical BNB fee reference failed validation; final P&L is pending.",
    "FEE_ASSET_UNSUPPORTED": "Commission asset needs supported valuation before final P&L.",
    "FEE_VALUATION_PENDING": "Verified commission valuation is missing; final P&L is pending.",
    "EXECUTION_HISTORY_INCOMPLETE": "Exchange execution history is incomplete; reconciliation is pending.",
    "EXECUTION_TOTALS_MISMATCH": "Execution totals do not match the order; reconciliation is pending.",
    "EXECUTION_HISTORY_TRUNCATED": "Execution history requires pagination; reconciliation is pending.",
    "ORDER_STATE_UNSUPPORTED": "Exchange order/list state requires reconciliation.",
    "RECONCILIATION_PENDING": "Exchange execution or fee reconciliation is pending; new entries remain blocked.",
}


def reconciliation_reason(code):
    return RECONCILIATION_REASONS.get(code) if type(code) is str else None


class RecoveryError(RuntimeError):
    """Only a fixed diagnostic code may be rendered; exception text stays private."""

    def __init__(self, message, *, reason_code=None):
        super().__init__(message)
        self.reason_code = reason_code if reconciliation_reason(reason_code) else None


class RecoveryValidationError(ValueError):
    """A local validation failure with an allow-listed public reason.

    Keep ValueError compatibility. Arbitrary SDK/HTTP ValueErrors are deliberately
    not trusted to provide a renderable reason or exception text.
    """

    def __init__(self, message, *, reason_code):
        super().__init__(message)
        self.reason_code = reason_code if reconciliation_reason(reason_code) else None


def recovery_code(error):
    if isinstance(error, (RecoveryError, RecoveryValidationError)):
        code = error.reason_code
        return code if reconciliation_reason(code) else None
    return None


def safe_error(error):
    # Do not stringify arbitrary exchange/HTTP exceptions: request URLs, bodies,
    # headers and signatures may contain credentials. No credential discovery.
    details = []
    code = getattr(error, "code", None)
    status = getattr(error, "status_code", None)
    # Accept numeric metadata only. A string-valued attribute may contain a
    # response body or secret, and bool is not a meaningful numeric error code.
    if type(code) is int:
        details.append(f"API code {code}")
    if type(status) is int and 100 <= status <= 599:
        details.append(f"HTTP {status}")
    metadata = f" ({', '.join(details)})" if details else ""
    reason_code = recovery_code(error)
    if reason_code:
        reason = reconciliation_reason(reason_code)
        if reason:
            return f"{type(error).__name__}{metadata} [{reason_code}]: {reason}"
    return f"{type(error).__name__}{metadata}: operation failed; reconcile exchange state"


def redact(text):
    text = re.sub(
        r"(?i)(?:https?://)?api\.telegram\.org/bot[^\s/]+",
        "[REDACTED TELEGRAM URL]",
        str(text),
    )
    return re.sub(
        r"(?i)(api[_-]?key|api[_-]?secret|token|signature|authorization)"
        r"([\s\"\x27:=]+)[^\s,&\"\x27]+",
        r"\1=[REDACTED]",
        text,
    )


class RedactingFilter(logging.Filter):
    def filter(self, record):
        if record.exc_info:
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def secure_logging():
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    # SDK/transport debug logs can expose signed requests. Do not propagate them.
    for name in ("binance", "urllib3", "requests", "websockets"):
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.propagate = False
        logger.handlers = [logging.NullHandler()]
