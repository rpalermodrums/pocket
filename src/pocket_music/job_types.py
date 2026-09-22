"""Store-relative identity for an owned local analysis job."""
from typing import Literal

from typing_extensions import TypedDict


class JobHandle(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    schema: Literal['pocket.job-handle/v1']
    job_id: str
