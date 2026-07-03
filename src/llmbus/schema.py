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
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _new_job_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_uuid(value: str) -> str:
    """Reject ids that aren't well-formed UUIDs (store-key / dedup integrity, §6)."""
    uuid.UUID(value)
    return value


# job_id stays a `str` (clean as a SQLite key / URL / dict key) but must parse as
# a UUID — see ARCHITECTURE.md §4/§6.
JobId = Annotated[str, AfterValidator(_ensure_uuid)]


class Message(BaseModel):
    """One chat message in a job's prompt."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class JobParams(BaseModel):
    """Model-call parameters passed through to the provider.

    `temperature` is intentionally unbounded here: valid ranges differ per
    provider (OpenAI 0-2, Anthropic 0-1), so the per-provider adapter owns that
    check. `max_tokens`, when set, must be positive — invalid at every provider.
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float = 0.0
    max_tokens: int | None = Field(default=None, gt=0)
    response_format: str | None = None


class Job(BaseModel):
    """A unit of LLM work placed on the `llm-jobs` topic."""

    model_config = ConfigDict(extra="forbid")

    job_id: JobId = Field(default_factory=_new_job_id)
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

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    input_tokens: int = Field(default=0, alias="in")
    output_tokens: int = Field(default=0, alias="out")
    cost_usd: float = 0.0


class Result(BaseModel):
    """Outcome of a job: stored and (optionally) POSTed to the callback."""

    model_config = ConfigDict(extra="forbid")

    job_id: JobId
    status: Literal["ok", "error"]
    completion: str | None = None
    usage: Usage = Field(default_factory=Usage)
    provider: str | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
