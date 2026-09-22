"""Transport contracts for supervised native candidates; no host driver."""
from typing import Literal, NotRequired

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle


class CandidateTime(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    n: int
    d: int


class CandidateContext(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    arrangement_start_qn: CandidateTime
    length_qn: CandidateTime
    tempo_bpm: float
    meter: list[int]


class LayerSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    track_name: str
    instrument_device: Literal["Operator"]


class NativeSaveReport(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    actor: str
    actor_kind: Literal["human", "agent"]
    observed_at: str
    host_version: str
    saved_als_sha256: str
    saved: bool
    reopened: bool
    missing_media: bool
    device_missing: bool
    instrument_recipe_verified: NotRequired[bool]
    evidence: NotRequired[list[ArtifactHandle]]


class NativeReconciliation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    actor: str
    actor_kind: Literal["human", "agent"]
    observed_at: str
    reason: str


class NativeAbandonment(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    actor: str
    actor_kind: Literal["human", "agent"]
    observed_at: str
    reason: str
    all_old_live_max_instances_stopped: Literal[True]
    old_writer_unloaded: Literal[True]
    old_candidate_closed_without_saving: Literal[True]
    no_native_dispatch_in_flight: Literal[True]


class RenderReport(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    actor: str
    actor_kind: Literal["human", "agent"]
    observed_at: str
    candidate_sha256: str
    export_completed: bool
    arrangement_only: bool
    no_missing_media: bool


class RenderSettings(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    sample_rate: int
    channels: int
    start_qn: CandidateTime
    end_qn: CandidateTime
    tempo_bpm: float
    tail_seconds: float
    normalization: Literal[False]


class AttributedDecision(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    action: Literal["keep", "revise", "reject", "no_addition"]
    actor: str
    actor_kind: Literal["human", "agent"]
    reason: str
    feedback: NotRequired[ArtifactHandle]
