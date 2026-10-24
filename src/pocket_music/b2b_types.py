# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded b2b inputs, so MCP and CLI schemas carry the nested enums and refuse unknown fields.

Control values are integers in the adapter's declared unit. One TypedDict per record carries a
``kind`` Literal; the broker enforces which fields belong to which kind. No agent-facing type has
an issuer, token, capability, actor or actor_kind field: issuing policy is not an agent input.
"""

from typing import Literal, NotRequired

from typing_extensions import TypedDict


class B2bSettings(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    notice_ack_timeout_ms: NotRequired[int]
    max_lease_ms: NotRequired[int]
    activity_release_ms: NotRequired[int]
    settle_ms: NotRequired[int]
    invitation_ttl_ms: NotRequired[int]
    trusted_page_timeout_ms: NotRequired[int]
    max_action_ms: NotRequired[int]
    max_evidence_age_ms: NotRequired[int]
    ack_timeout_ms: NotRequired[int]
    uncertainty_ms: NotRequired[int]
    suspend_threshold_ms: NotRequired[int]


class PermissionLimit(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    resource: str
    minimum: int
    maximum: int


class ProactiveClaims(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    allowed: bool
    idle_interval_ms: int
    cooldown_ms: int


class PermissionDraft(TypedDict):
    """A proposed standing permission. Proposing it grants nothing; the person issues it on the page."""
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    resources: list[str]
    action_kinds: list[Literal["set", "ramp", "nudge", "set_state"]]
    limits: NotRequired[list[PermissionLimit]]
    proactive_claims: ProactiveClaims
    max_lease_ms: int
    duration_ms: int
    statement: str


class ClaimBasis(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    kind: Literal["invitation", "standing_permission"]
    invitation_id: NotRequired[str]
    permission_id: NotRequired[str]
    permission_version: NotRequired[int]
    revocation_generation: NotRequired[int]


class B2bAction(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    kind: Literal["set", "ramp", "nudge", "set_state"]
    resource: str
    value: NotRequired[int]
    target: NotRequired[int]
    duration_ms: NotRequired[int]
    delta: NotRequired[int]
    state: NotRequired[str]


class RevokeTarget(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    kind: Literal["grant_lease", "own_proposal", "all_own_authority", "permission"]
    grant_id: NotRequired[str]
    lease_ms: NotRequired[int]
    proposal_id: NotRequired[str]
    permission_id: NotRequired[str]


class EventCursor(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - TypedDict schema metadata
    host_epoch: str
    sequence: int
