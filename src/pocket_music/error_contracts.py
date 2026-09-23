"""Opt-in machine error envelope; legacy messages, exception type and exit codes stay intact."""
from __future__ import annotations

from .errors import PocketError

SCHEMA = "pocket.error/v2"
CODES = ("invalid_request", "invalid_arguments", "io_error", "idempotency_conflict", "request_not_complete",
         "stale_revision", "source_mismatch", "ambiguous_mapping", "unsupported_profile", "locked_field", "evidence_mismatch")
JSON_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    "additionalProperties": False, "required": ["schema", "code", "error", "message"],
    "properties": {"schema": {"const": SCHEMA}, "code": {"enum": list(CODES)},
                   "error": {"type": "string"}, "message": {"type": "string"}}}


def error_envelope(error: Exception, *, code: str | None = None) -> dict:
    """Represent a known error; never infer a retry, approval, or native outcome from its wording."""
    if code is None:
        code = (error.code if isinstance(error, PocketError) else "io_error" if isinstance(error, OSError)
                else "invalid_arguments" if isinstance(error, (TypeError, ValueError)) else "invalid_request")
    if code not in CODES:
        raise ValueError("Unknown machine error code")
    return {"schema": SCHEMA, "code": code, "error": type(error).__name__, "message": str(error)}
