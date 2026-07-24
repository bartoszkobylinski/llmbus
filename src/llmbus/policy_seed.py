"""Central model-policy seed — model parity across the direct->bus flip (§14 #23).

Each entry pins a ``(project, kind)`` to the model that kind runs on TODAY, so that
flipping the kind onto the bus (a Job with ``model=None``) resolves to the SAME model
instead of silently swapping it. Values come from the milamber LLM surface map
(``milamber_assistant/analysis/milamber-llm-surface.md``), re-verified against milamber
main @38f9693.

Only kinds that are all of (a) chat, (b) bus-eligible, and (c) already on a model the
bus registry routes belong here — a policy row pointing at an unroutable model fails
loud at submit, not silently (§14 #23). That excludes ``nutrition.estimate`` (gpt-5.2)
and ``training.*`` (gpt-5.4) until those models join ``providers.base.PROVIDERS``; the
routability check in ``seed_cli`` (and its test) enforces the rule so an unroutable row
can never ship. The three instagram kinds are milamber's F1 bus pilot; all three share
one classifier (``instagram/series_classify.py``) on ``SERIES_MODEL`` -> ``gpt-5-nano``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicySeed:
    """One ``(project, kind) -> model`` row to upsert into the model policy."""

    project: str
    kind: str
    model: str


# Model parity for milamber's F1 bus pilot. Keep every model here in
# providers.base.PROVIDERS — the routability test refuses anything else.
MODEL_POLICY_SEED: tuple[PolicySeed, ...] = (
    PolicySeed("milamber", "instagram.series", "gpt-5-nano"),
    PolicySeed("milamber", "instagram.topic", "gpt-5-nano"),
    PolicySeed("milamber", "instagram.hook", "gpt-5-nano"),
)
