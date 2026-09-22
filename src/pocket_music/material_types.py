"""Versioned transport records for standalone musical material capabilities."""
from __future__ import annotations

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle

__all__ = [
    "ArtifactHandle",
    "CCStepBinding",
    "EditOperation",
    "MaterialEditOperation",
    "MaterialLocks",
    "MaterialRecord",
    "MaterialSelection",
    "MaterialSource",
    "PatternBrief",
    "Position",
    "Rational",
    "SeedMap",
]


class EmbeddedCCStepBinding(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    curve_id: str
    controller: Literal[1, 11]
    same_tick_order: Literal['before_existing']


class ExternalCCStepBinding(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    curve: ArtifactHandle
    clip_id: str
    controller: Literal[1, 11]
    same_tick_order: Literal['before_existing']


CCStepBinding = EmbeddedCCStepBinding | ExternalCCStepBinding


class Rational(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    n: int
    d: int


class Position(Rational):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    space: Literal['clip_qn', 'phrase_qn', 'arrangement_qn']


class MaterialSource(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal['smf', 'live_clip', 'material']
    path: NotRequired[str]
    expected_sha256: NotRequired[str]
    thread_handle: NotRequired[dict]
    clip_id: NotRequired[str]
    material: NotRequired[MaterialRecord]


class MaterialSelection(TypedDict, total=False):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    note_ids: list[str]
    voice_ids: list[str]
    clip_ids: list[str]
    span_qn: list[Rational | int]
    time_space: Literal['clip_qn']
    material_revision: str
    selection_sha256: str


class PatternBrief(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    role: str
    pitch: int
    cell_qn: Rational | int
    cell: list[Rational | int]
    length_qn: Rational | int
    enter_qn: NotRequired[Rational | int]
    exit_qn: NotRequired[Rational | int]
    gate_qn: NotRequired[Rational | int]
    velocities: NotRequired[list[int]]
    variation_qn: NotRequired[Rational | int]
    channel: NotRequired[int]
    intentions: NotRequired[list[str]]


class SeedMap(TypedDict, total=False):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    structure: int
    timing: int
    velocity: int


class EditOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['shift', 'transpose', 'repitch', 'velocity', 'release_velocity', 'resize', 'thin']
    delta_qn: NotRequired[Rational | int]
    semitones: NotRequired[int]
    pitch: NotRequired[int]
    value: NotRequired[int]
    duration_qn: NotRequired[Rational | int]
    every: NotRequired[int]
    offset: NotRequired[int]


class DeleteOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['delete']
    controller_timeline: Literal['preserve_existing']


class DuplicateOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['duplicate']
    delta_qn: Rational | int
    controller_timeline: Literal['preserve_existing']


class GridQuantizeOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['grid_quantize']
    grid_qn: Rational | int
    phase_qn: Rational | int
    strength: Rational | int
    threshold_qn: Rational | int
    ties: Literal['earlier', 'later']
    note_off: Literal['follow_onset']
    controller_timeline: Literal['preserve_existing']
    time_space: Literal['clip_qn']


class VelocityMappingEntry(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    from_value: int
    to_value: int


class VelocityMapOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['velocity_map']
    field: Literal['velocity', 'release_velocity']
    mapping: list[VelocityMappingEntry]
    unmapped: Literal['preserve', 'reject']


class MaterialLocks(TypedDict, total=False):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    outside_selection: Literal['all']
    selected_fields: list[str]


class PitchRecord(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    midi_note: int
    cents_offset: int | float
    tuning_ref: str


class VelocityRecord(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    value: int
    domain: Literal['midi1_7bit']


class LiteralNoteSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    onset_qn: Rational | int
    duration_qn: Rational | int
    pitch: PitchRecord
    velocity: VelocityRecord
    release_velocity: VelocityRecord
    channel: int
    mute: bool
    voice_id: str
    role_ref: str | None


class AddOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['add']
    clip_id: str
    notes: list[LiteralNoteSpec]
    controller_timeline: Literal['preserve_existing']
    time_space: Literal['clip_qn']


class SplitOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['split']
    offsets_qn: list[Rational | int]
    controller_timeline: Literal['preserve_existing']
    time_space: Literal['note_relative_qn']
    articulation: Literal['retrigger']


class MergeOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['merge']
    controller_timeline: Literal['preserve_existing']
    time_space: Literal['clip_qn']
    articulation: Literal['remove_retriggers']


class GrooveAnchor(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    nominal_qn: Rational | int
    offset_qn: Rational | int


class GrooveOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['groove']
    cycle_qn: Rational | int
    anchors: list[GrooveAnchor]
    phase_qn: Rational | int
    strength: Rational | int
    threshold_qn: Rational | int
    ties: Literal['earlier', 'later']
    note_off: Literal['follow_onset']
    controller_timeline: Literal['preserve_existing']
    time_space: Literal['clip_qn']


class NotePitchDestination(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    note_id: str
    pitch: PitchRecord


class HarmonicHypothesis(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    label: str
    actor: str
    actor_kind: Literal['human', 'agent']
    statement: str
    uncertainty: list[str]


class RevoiceOperation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal['revoice']
    destinations: list[NotePitchDestination]
    hypothesis: HarmonicHypothesis
    controller_timeline: Literal['preserve_existing']
    pitch_expression: Literal['preserve_relative']


MaterialEditOperation = (
    EditOperation | DeleteOperation | DuplicateOperation | GridQuantizeOperation | VelocityMapOperation
    | AddOperation | SplitOperation | MergeOperation | GrooveOperation | RevoiceOperation
)


class NoteRecord(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    id: str
    voice_id: str
    role_ref: str | None
    onset: Position
    duration_qn: Rational
    pitch: PitchRecord
    velocity: VelocityRecord
    release_velocity: VelocityRecord
    channel: int
    mute: bool
    expression_refs: list[str]
    source_binding: dict | None
    derived_from: list[str]


class ClipRecord(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    id: str
    track_id: str
    origin: Position
    length_qn: Rational
    loop: Literal[False]
    note_ids: list[str]
    event_ids: list[str]
    curve_ids: list[str]


class WireEvent(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    id: str
    time: Position
    order: int
    message_type: str
    is_meta: bool
    bytes: list[int]
    source_tick: NotRequired[int]
    track_index: NotRequired[int]


class MaterialRecord(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    schema: Literal['pocket.material/v1']
    material_id: str
    revision_sha256: str
    parent_revision: str | None
    sources: list[dict]
    tracks: list[dict]
    clips: list[ClipRecord]
    notes: list[NoteRecord]
    events: list[WireEvent]
    curves: list[dict]
    tempo_map_ref: ArtifactHandle | None
    meter_map_ref: ArtifactHandle | None
    coverage: dict
    provenance: dict
