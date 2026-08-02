"""Structured logging.

JSON lines, not prose: a log aggregator can filter on structured fields,
whereas free text has to be parsed with fragile regex.

CRITICAL: nothing in this module ever receives a raw card number, email, or
IP. Log lines outlive databases -- they get shipped to third-party services,
sit in files for years, and are read by people with no database access. A
PAN in a log is a PAN in production forever.
"""

import json
import sys
from datetime import UTC, datetime
from typing import Any

_LEVELS = {"error": 0, "warn": 1, "info": 2, "debug": 3}

# Field names that must never be logged, whatever the caller passes.
_FORBIDDEN = {
    "card_number", "pan", "email", "ip_address", "device_fingerprint",
    "account_id", "password", "entity_hash_salt",
}


def _scrub(fields: dict[str, Any]) -> dict[str, Any]:
    """Replace any forbidden key with a marker.

    A denylist here is a backstop, not the primary control -- the routes
    already avoid passing these. But a backstop that costs one dict
    comprehension is worth having, because the day someone adds a debug log
    in a hurry is the day it matters.
    """
    return {
        k: ("[redacted]" if k.lower() in _FORBIDDEN else v)
        for k, v in fields.items()
    }


class Logger:
    def __init__(self, level: str = "info") -> None:
        self.threshold = _LEVELS.get(level, _LEVELS["info"])

    def _emit(self, level: str, message: str, **fields: Any) -> None:
        if _LEVELS[level] > self.threshold:
            return
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": level,
            "message": message,
            **_scrub(fields),
        }
        sys.stdout.write(json.dumps(payload, default=str) + "\n")
        sys.stdout.flush()

    def error(self, message: str, **f: Any) -> None:
        self._emit("error", message, **f)

    def warn(self, message: str, **f: Any) -> None:
        self._emit("warn", message, **f)

    def info(self, message: str, **f: Any) -> None:
        self._emit("info", message, **f)

    def debug(self, message: str, **f: Any) -> None:
        self._emit("debug", message, **f)


def get_logger() -> Logger:
    from fraud_engine.config import get_settings

    return Logger(get_settings().log_level)
