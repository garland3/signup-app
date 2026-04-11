"""Audit logging to stdout.

Emits structured lines for security-relevant events (key create/update/
delete/block/unblock, logins, logouts, auth failures). Goes through the
standard logging module so it plays nice with uvicorn's handlers and can
be redirected by the operator.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_logger = logging.getLogger("signup_app.audit")
_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    # Only attach a handler if nothing else has (avoid double logging when
    # uvicorn has already wired up the root logger).
    if not _logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    _configured = True


def audit(event: str, **fields: Any) -> None:
    """Emit a single JSON line to stdout describing an audit event."""
    _configure()
    payload: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
    }
    payload.update(fields)
    try:
        line = json.dumps(payload, default=str, sort_keys=True)
    except Exception:
        line = f'{{"event": "{event}", "error": "audit_serialize_failed"}}'
    _logger.info(line)
