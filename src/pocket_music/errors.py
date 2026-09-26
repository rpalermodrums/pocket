# SPDX-License-Identifier: AGPL-3.0-only
class PocketError(ValueError):
    """An invalid request, retaining its legacy message with an optional stable code."""

    def __init__(self, message, *, code="invalid_request"):
        super().__init__(message)
        self.code = code
