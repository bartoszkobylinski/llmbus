"""Message contract for the bus (ARCHITECTURE.md §4).

`Job` is what producers place on the `llm-jobs` topic; `Result` is what the
worker writes to the store and (optionally) POSTs to a callback. These models
ARE the public API — any change to their shape needs an ARCHITECTURE.md update
in the same PR.

Iggy's Python SDK has no message headers yet, so metadata (`project`, `model`,
`meta`) rides in the JSON body. Wire JSON is produced with `by_alias=True`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _new_job_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(BaseModel):
    """One chat message in a job's prompt."""

    role: Literal["system", "user", "assistant"]
    content: str


class JobParams(BaseModel):
    """Model-call parameters passed through to the provider."""

    temperature: float = 0.0
    max_tokens: int | None = None
    response_format: str | None = None


class Job(BaseModel):
    """A unit of LLM work placed on the `llm-jobs` topic."""

    job_id: str = Field(default_factory=_new_job_id)
    project: str
    kind: str
    model: str
    messages: list[Message]
    params: JobParams = Field(default_factory=JobParams)
    callback_url: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime = Field(default_factory=_utcnow)


class Usage(BaseModel):
    """Token accounting and derived cost for a completed job.

    Wire aliases `in`/`out` match ARCHITECTURE.md §4 (both are Python keywords,
    hence the aliased fields). Serialize with `model_dump(by_alias=True)`.
    """

    model_config = ConfigDict(populate_by_name=True)

    input_tokens: int = Field(default=0, alias="in")
    output_tokens: int = Field(default=0, alias="out")
    cost_usd: float = 0.0


class Result(BaseModel):
    """Outcome of a job: stored and (optionally) POSTed to the callback."""

    job_id: str
    status: Literal["ok", "error"]
    completion: str | None = None
    usage: Usage = Field(default_factory=Usage)
    provider: str | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
