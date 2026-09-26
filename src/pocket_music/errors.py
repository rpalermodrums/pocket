# SPDX-License-Identifier: AGPL-3.0-only
# The pocket.error/v3 schema bounds a hint to 1-400 characters; enforce it where hints are made.
HINT_MAX_LENGTH = 400


def valid_hint(hint) -> bool:
    return isinstance(hint, str) and 1 <= len(hint) <= HINT_MAX_LENGTH


class PocketError(ValueError):
    """An invalid request, retaining its legacy message with an optional stable code.

    ``hint`` is an optional corrective sentence for the opt-in v3 error envelope.
    It never changes the legacy message, the exception type or the code.
    """

    def __init__(self, message, *, code="invalid_request", hint=None):
        if hint is not None and not valid_hint(hint):
            raise ValueError(f"PocketError hint must be a 1-{HINT_MAX_LENGTH} character string")
        super().__init__(message)
        self.code = code
        self.hint = hint
