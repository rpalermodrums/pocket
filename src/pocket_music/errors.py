# SPDX-License-Identifier: AGPL-3.0-only
class PocketError(ValueError):
    """An invalid request, retaining its legacy message with an optional stable code.

    ``hint`` is an optional corrective sentence for the opt-in v3 error envelope.
    It never changes the legacy message, the exception type or the code.
    """

    def __init__(self, message, *, code="invalid_request", hint=None):
        super().__init__(message)
        self.code = code
        self.hint = hint
