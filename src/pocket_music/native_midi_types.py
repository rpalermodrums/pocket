# SPDX-License-Identifier: AGPL-3.0-only
"""Strict transport inputs for the separate native MIDI adapter."""
from typing import Literal, NotRequired

from typing_extensions import TypedDict


class NativeReadTarget(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    location: Literal["arrangement"]
    track_index: int
    clip_index: int


class NativeSavedBinding(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    saved_als: str
    expected_sha256: str
    track_id: str
    clip_id: str


class NativeRequestReference(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    session_nonce: str
    request_key: str


class NativeSessionAcknowledgment(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    actor: str
    actor_kind: Literal["human", "agent"]
    observed_at: str
    exclusive_native_session: Literal[True]
    no_ui_edits_after_native_read: Literal[True]
    note: NotRequired[str]


class NativeTime(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    n: int
    d: int


class NativeInsertNote(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    pitch: int
    onset_qn: NativeTime
    duration_qn: NativeTime
    velocity: int
    release_velocity: int


class NativeInsertEdit(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["insert_empty"]
    notes: list[NativeInsertNote]


class NativeVelocityEdit(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["set_velocity"]
    note_id: int
    velocity: int
    insertion_terminal: "ArtifactHandle"


from .artifact_store import ArtifactHandle

NativeMidiWriteEdit = NativeInsertEdit | NativeVelocityEdit
