# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in machine error envelope; legacy messages, exception type and exit codes stay intact.

``pocket.error/v2`` is closed and unchanged. ``pocket.error/v3`` adds one optional
``hint``: a short corrective sentence naming an argument to supply or a call to make
first. A hint comes from an explicit raise site or from its code, never from message
wording, and it never implies a retry outcome, an approval or a native result.
"""
from __future__ import annotations

import inspect

from .errors import PocketError

SCHEMA = "pocket.error/v2"
SCHEMA_V3 = "pocket.error/v3"
FORMATS = ("legacy", "v2", "v3")
CODES = ("invalid_request", "invalid_arguments", "io_error", "idempotency_conflict", "request_not_complete",
         "stale_revision", "source_mismatch", "ambiguous_mapping", "unsupported_profile", "locked_field", "evidence_mismatch")
HINT_MAX_LENGTH = 400
JSON_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    "additionalProperties": False, "required": ["schema", "code", "error", "message"],
    "properties": {"schema": {"const": SCHEMA}, "code": {"enum": list(CODES)},
                   "error": {"type": "string"}, "message": {"type": "string"}}}
JSON_SCHEMA_V3 = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    "additionalProperties": False, "required": ["schema", "code", "error", "message"],
    "properties": {"schema": {"const": SCHEMA_V3}, "code": {"enum": list(CODES)},
                   "error": {"type": "string"}, "message": {"type": "string"},
                   "hint": {"type": "string", "minLength": 1, "maxLength": HINT_MAX_LENGTH,
                            "description": "Optional corrective guidance: an argument to supply or a call to make "
                                           "first. It is not a retry guarantee, an approval or a native outcome."}}}

# Corrective defaults per code. Raise sites may give a more specific hint. The
# unclassified invalid_request bucket has no default: a guess would mislead.
CODE_HINTS = {
    "invalid_arguments": ("Match the provider's input schema: declared argument names only, every required "
                          "argument, and the declared JSON types. MCP tools/list and the provider reference "
                          "show each schema."),
    "io_error": ("Check that each path you named exists, has the expected file or folder type and is accessible "
                 "to the Pocket process. Relative paths resolve from that process's working directory."),
    "idempotency_conflict": ("Use a new request_id for changed inputs, or call request_status to inspect the "
                             "earlier request."),
    "request_not_complete": ("Call request_status with the same store_root and request_id to inspect the "
                             "earlier request. Don't remove its lock."),
    "stale_revision": "Read the current revision, choose from what it contains, and pass that revision's identity.",
    "source_mismatch": ("Pass handles derived from the same exact source or render. Pocket doesn't substitute "
                        "one for another."),
    "ambiguous_mapping": "Name the exact occurrence you mean. Pocket never picks the first match.",
    "unsupported_profile": ("The input is outside this operation's declared profile; capabilities_list and the "
                            "guide state its limits. Don't convert, resample or round the input to fit."),
    "locked_field": "Leave locked fields unchanged. Removing a lock is an authoring decision, not a fix.",
    "evidence_mismatch": ("Pass the store_root of the complete store that wrote these handles. Keep the store "
                          "together; don't edit, rehash or move artifacts one by one."),
}

# Appended by the request journal when a started write fails: a fact it has just recorded.
FAILED_REQUEST_HINT = "This request_id is now recorded as failed, so a corrected call needs a new request_id."


def error_envelope(error: Exception, *, code: str | None = None, version: str = "v2",
                   hint: str | None = None) -> dict:
    """Represent a known error; never infer a retry, approval, or native outcome from its wording.

    ``version="v2"`` returns the unchanged v2 envelope and ignores ``hint``. ``version="v3"``
    adds the explicit ``hint``, else the raise site's hint for its own code, else the code's.
    """
    if code is None:
        code = (error.code if isinstance(error, PocketError) else "io_error" if isinstance(error, OSError)
                else "invalid_arguments" if isinstance(error, (TypeError, ValueError)) else "invalid_request")
    if code not in CODES:
        raise ValueError("Unknown machine error code")
    if version not in ("v2", "v3"):
        raise ValueError("Unknown machine error version")
    envelope = {"schema": SCHEMA if version == "v2" else SCHEMA_V3, "code": code,
                "error": type(error).__name__, "message": str(error)}
    if version == "v3":
        if hint is None and isinstance(error, PocketError) and error.code == code:
            hint = error.hint
        hint = CODE_HINTS.get(code) if hint is None else hint
        if hint is not None:
            envelope["hint"] = hint
    return envelope


def argument_hint(function, arguments: dict, argument: str | None = None) -> str | None:
    """Name the argument fix from the declared signature, never from the exception text.

    ``argument`` is the declared parameter whose value failed strict validation, if any.
    """
    if argument is not None:
        return (f"Make {argument} match the tool's input schema exactly. Values are validated strictly and "
                "never coerced; for example, the string \"5\" is not an integer.")
    parameters = {name: p for name, p in inspect.signature(function).parameters.items()
                  if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    if set(arguments) - set(parameters):
        return "Remove arguments the tool doesn't declare. Its input schema in tools/list lists every accepted name."
    missing = [name for name, p in parameters.items() if p.default is p.empty and name not in arguments]
    if missing:
        return f"Supply the required argument{'s' if len(missing) > 1 else ''} {', '.join(missing)}."
    return None
