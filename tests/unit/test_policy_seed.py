"""The model-policy seed: routable models only, and it upserts exactly what it declares.

The seed's whole reason to exist is model parity across the direct->bus flip (§14 #23):
a bused kind (``Job.model=None``) must resolve to the model it runs on today. A row
pointing at a model the registry can't route would defeat that by failing loud at
submit, so the routability check is the load-bearing test here.
"""

import asyncio

from llmbus.policy_seed import MODEL_POLICY_SEED, PolicySeed
from llmbus.providers.base import provider_for
from llmbus.seed_cli import apply_seed, main, unroutable
from llmbus.store import Store


async def _make_store(path: str) -> None:
    async with Store(path):
        pass


async def _policies(path: str) -> dict[tuple[str, str], str]:
    async with Store(path) as store:
        rows = await store.list_model_policies()
    return {(r.project, r.kind): r.model for r in rows}


# --- the seed data itself -------------------------------------------------------------


def test_every_seeded_model_is_routable() -> None:
    """A row pointing at a model the bus can't route fails loud at submit (§14 #23),
    defeating the parity the seed exists to provide — guard it at the source."""
    for row in MODEL_POLICY_SEED:
        provider_for(row.model)  # raises UnknownModelError if not in the registry


def test_unroutable_is_empty_for_the_shipped_seed() -> None:
    assert unroutable() == []


def test_seed_is_exactly_the_three_instagram_pilot_kinds() -> None:
    """Canary: mutmut won't mutate a module-level tuple, so pin its contents here."""
    assert MODEL_POLICY_SEED == (
        PolicySeed("milamber", "instagram.series", "gpt-5-nano"),
        PolicySeed("milamber", "instagram.topic", "gpt-5-nano"),
        PolicySeed("milamber", "instagram.hook", "gpt-5-nano"),
    )


def test_every_seed_key_is_unique() -> None:
    keys = [(r.project, r.kind) for r in MODEL_POLICY_SEED]
    assert len(keys) == len(set(keys))


def test_unroutable_flags_a_bad_model() -> None:
    bad = (PolicySeed("milamber", "x.y", "no-such-model"),)
    assert unroutable(bad) == list(bad)


# --- apply_seed against a real store --------------------------------------------------


async def test_apply_seed_upserts_every_row() -> None:
    async with Store(":memory:") as store:
        applied = await apply_seed(store)
        assert applied == list(MODEL_POLICY_SEED)
        rows = await store.list_model_policies()
        assert {(r.project, r.kind): r.model for r in rows} == {
            ("milamber", "instagram.series"): "gpt-5-nano",
            ("milamber", "instagram.topic"): "gpt-5-nano",
            ("milamber", "instagram.hook"): "gpt-5-nano",
        }


async def test_apply_seed_is_idempotent() -> None:
    async with Store(":memory:") as store:
        await apply_seed(store)
        await apply_seed(store)  # PK (project, kind) upsert — must not duplicate
        rows = await store.list_model_policies()
        assert len(rows) == len(MODEL_POLICY_SEED)


# --- the CLI shell --------------------------------------------------------------------


def test_main_seeds_an_existing_store(tmp_path) -> None:
    path = str(tmp_path / "store.db")
    asyncio.run(_make_store(path))

    rc = main(["--store-path", path])

    assert rc == 0
    assert asyncio.run(_policies(path)) == {
        ("milamber", "instagram.series"): "gpt-5-nano",
        ("milamber", "instagram.topic"): "gpt-5-nano",
        ("milamber", "instagram.hook"): "gpt-5-nano",
    }


def test_main_refuses_a_missing_store(tmp_path, capsys) -> None:
    rc = main(["--store-path", str(tmp_path / "absent.db")])

    assert rc == 2
    assert "no store" in capsys.readouterr().err


def test_main_refuses_an_unroutable_seed(tmp_path, monkeypatch, capsys) -> None:
    path = str(tmp_path / "store.db")
    asyncio.run(_make_store(path))
    monkeypatch.setattr(
        "llmbus.seed_cli.unroutable",
        lambda *a, **k: [PolicySeed("milamber", "x.y", "no-such-model")],
    )

    rc = main(["--store-path", path])

    assert rc == 2
    assert "unroutable" in capsys.readouterr().err
    assert asyncio.run(_policies(path)) == {}  # refused before writing anything
