import logging
import re
from collections.abc import MutableMapping
from typing import Any

_AUTH_PATTERN = re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?[^\s,'\"}]+")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN_FIELD_PATTERN = re.compile(r"(?i)(token|secret|password)(['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+")


def redact_secrets(value: str) -> str:
    value = _AUTH_PATTERN.sub(r"\1[REDACTED]", value)
    value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    return _TOKEN_FIELD_PATTERN.sub(r"\1\2[REDACTED]", value)


def _redact_log_argument(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    return value


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(str(record.msg))
        if record.args:
            if isinstance(record.args, MutableMapping):
                record.args = {
                    key: _redact_log_argument(value) for key, value in record.args.items()
                }
            else:
                record.args = tuple(_redact_log_argument(value) for value in record.args)
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def safe_log_context(**values: Any) -> dict[str, Any]:
    forbidden = {"token", "authorization", "encrypted_donut_token"}
    return {key: value for key, value in values.items() if key.lower() not in forbidden}
