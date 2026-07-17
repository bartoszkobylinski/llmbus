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

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr


def _new_job_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_uuid(value: str) -> str:
    """Canonicalize to a lowercase-hyphenated UUID string; reject non-UUIDs.

    `uuid.UUID` also accepts uppercase / `urn:uuid:` / `{...}` forms, so we
    normalize them — one logical id must not become two store keys (§6).
    Whitespace-padded values still raise (they are not stripped).
    """
    return str(uuid.UUID(value))


# job_id stays a `str` (clean as a SQLite key / URL / dict key) but must parse as
# a UUID — see ARCHITECTURE.md §4/§6. StrictStr so lax bytes→str coercion can't
# smuggle a non-string id past the UUID check.
JobId = Annotated[StrictStr, AfterValidator(_ensure_uuid)]


def _ensure_strict_object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Reject a response schema neither provider's strict mode would accept.

    Both mapped targets — OpenAI `json_schema` with `strict: true` and Anthropic
    `output_config.format` — require the top level to be an object schema with
    `additionalProperties: false` (§14 #10). Checking here fails the job at
    submit time instead of after it was queued (§4 fail-loud). Deeper schema
    validity stays the provider's job; nested objects are not walked.
    """
    if not schema:
        raise ValueError("response_format schema must be a non-empty JSON Schema object")
    if schema.get("type") != "object":
        raise ValueError("response_format schema must declare top-level type 'object'")
    if schema.get("additionalProperties") is not False:
        raise ValueError("response_format schema must set top-level additionalProperties to false")
    return schema


class ResponseFormat(BaseModel):
    """Structured-output request: constrain the completion to a JSON Schema.

    Only the `json_schema` variant exists (§14 #10) — it is the one shape that
    maps natively onto BOTH providers: OpenAI `response_format={"type":
    "json_schema", "json_schema": {name, schema, strict}}` and Anthropic
    `output_config={"format": {"type": "json_schema", "schema": ...}}`. OpenAI's
    loose "JSON mode" (`json_object`) has no Anthropic equivalent and is
    deliberately not in the contract. `name` is required by OpenAI's wire shape
    (its charset rules are enforced by the provider, not here); Anthropic's has
    no name field, so its adapter drops it.

    The field is named `json_schema` in Python because `schema` shadows a
    `BaseModel` attribute; the wire key is `schema` (§4, `by_alias=True`).
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["json_schema"] = "json_schema"
    name: str = Field(min_length=1)
    json_schema: Annotated[dict[str, Any], AfterValidator(_ensure_strict_object_schema)] = Field(
        alias="schema"
    )


class Message(BaseModel):
    """One chat message in a job's prompt."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class JobParams(BaseModel):
    """Model-call parameters passed through to the provider.

    `temperature` is **optional** (unset = let the model use its own default) and
    unbounded here: support and valid ranges differ per model, so the per-provider
    adapter owns that check — the GPT-5 family, for one, rejects any caller-set
    temperature (§7, §14 #9). `max_tokens`, when set, must be positive — invalid at
    every provider.

    `response_format` (structured output) is the `json_schema`-only
    `ResponseFormat` type (§14 #10, reopened 2026-07-17 for the hate-moderator
    pilot): unset means a free-text completion; set, each adapter maps it onto
    its provider's native strict-JSON shape.
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    response_format: ResponseFormat | None = None


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

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

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
