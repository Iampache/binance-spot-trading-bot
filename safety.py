"""Credential-safe diagnostics and immutable deployment limits."""

import logging
import re

MIN_QUOTE_VOLUME = 10_000_000
RISK_PCT = 1
MAX_POSITION_USDT = 12
MAX_OPEN_TRADES = 1
MAX_DAILY_LOSS_USD = 3


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
