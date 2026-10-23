"""Logging setup with a filter that keeps secrets out of log output."""

import logging
import re
import sys

from app.core.config import Settings

REDACTED = "***"

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[^\s'\"]+"),
    re.compile(r"(?i)((?:access_token|refresh_token|client_secret|client_id|code|password)=)[^&\s'\"]+"),
    re.compile(r"(?i)(\"(?:access_token|refresh_token|client_secret|password)\"\s*:\s*\")[^\"]+"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+"),
]


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        # Longest first so a secret containing another one is fully masked.
        self._secrets = sorted({s for s in (secrets or []) if len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        if record.exc_info:
            # Render the traceback now so it goes through redaction too.
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretRedactingFilter(settings.secret_values()))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    # httpx logs full request URLs at INFO; keep it quiet.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
