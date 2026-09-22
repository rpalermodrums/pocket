"""Strict opt-in declarations for local learned pulse hypotheses."""
from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle


class AudioModelFile(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    path: str
    sha256: str


class AudioModelDeclaration(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    adapter: Literal['beat_this_cpu_v1']
    executable: AudioModelFile
    weights: AudioModelFile
    expected_profile: str | None


class InlineAudioPulseModel(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['inline']
    declaration: AudioModelDeclaration
    qualification: Literal['synthetic_cpu_v1']


class InspectedAudioPulseModel(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['inspected']
    model: ArtifactHandle


AudioPulseModel = InlineAudioPulseModel | InspectedAudioPulseModel


class AudioPulseSettings(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    device: Literal['cpu']
    dtype: Literal['float32']
    threads: Literal[1]
    postprocessor: Literal['minimal']
    downmix: Literal['arithmetic_mean']
    resampler: Literal['soxr_hq']
    seed: Literal[0]
