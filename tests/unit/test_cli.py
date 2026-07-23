"""Unit tests for the `llmbus-costs` report shell (§11).

cli.py is the I/O half of the cost view (argparse, asyncio.run, SQLite, file
write), so it is outside the mutmut gate but still owes coverage. These drive it
against a real temp store and a real written file — no Iggy, no network, no
provider SDKs, which is itself part of the contract: the report must run on a box
that holds no credentials.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from llmbus.cli import (
    build_parser,
    collect_summary,
    main,
    require_existing_store,
    resolve_store_path,
)
from llmbus.config import ConfigError
from llmbus.schema import Job, Message, Result, Usage
from llmbus.store import Store

_SUBMITTED = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return str(tmp_path / "store.db")


async def _seed(path, *, project="hate-moderator", submitted_at=_SUBMITTED, cost=0.25):
    """Write one finalized, non-zero-cost job so the ledger has something in it."""
    async with Store(path) as store:
        job = Job(
            project=project,
            kind="classify",
            model="gpt-5-mini",
            messages=[Message(role="user", content="hi")],
            submitted_at=submitted_at,
        )
        await store.insert_pending(job)
        await store.finalize(Result(job_id=job.job_id, status="ok", usage=Usage(cost_usd=cost)))


def _seed_sync(path, **kwargs):
    """Seed from a *sync* test.

    `main()` owns its event loop (`asyncio.run`, §14 #17), so every test that
    calls it must be sync — an async test would already hold a running loop and
    `asyncio.run` refuses to nest. That is the real entry-point contract, so the
    tests honour it rather than reaching past `main()` to the coroutine.
    """
    asyncio.run(_seed(path, **kwargs))


def _empty_store(path):
    async def _create():
        async with Store(path):
            pass

    asyncio.run(_create())


# --- argument parsing --------------------------------------------------------


def test_parser_defaults_the_output_next_to_the_caller():
    args = build_parser().parse_args([])
    assert args.output == "llmbus-costs.html"


def test_parser_defaults_the_store_path_to_the_environment():
    assert build_parser().parse_args([]).store_path is None


def test_parser_accepts_both_paths():
    args = build_parser().parse_args(["--store-path", "/a.db", "--output", "/b.html"])
    assert (args.store_path, args.output) == ("/a.db", "/b.html")


# --- store-path resolution ---------------------------------------------------


def test_resolve_prefers_the_explicit_flag_over_the_environment(monkeypatch):
    monkeypatch.setenv("STORE_PATH", "/from/env.db")
    assert resolve_store_path("/explicit.db") == "/explicit.db"


def test_resolve_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("STORE_PATH", "/from/env.db")
    assert resolve_store_path(None) == "/from/env.db"


# --- the missing-store guard -------------------------------------------------


def test_require_existing_store_accepts_a_real_file(tmp_path):
    path = tmp_path / "store.db"
    path.write_text("")
    require_existing_store(str(path))  # does not raise


def test_require_existing_store_refuses_a_path_that_is_not_there(tmp_path):
    with pytest.raises(ConfigError, match="nothing to report on"):
        require_existing_store(str(tmp_path / "absent.db"))


def test_require_existing_store_refuses_a_directory(tmp_path):
    with pytest.raises(ConfigError) as caught:
        require_existing_store(str(tmp_path))
    assert str(tmp_path) in str(caught.value)


def test_require_existing_store_does_not_create_the_file_it_rejects(tmp_path):
    absent = tmp_path / "absent.db"
    with pytest.raises(ConfigError):
        require_existing_store(str(absent))
    assert not absent.exists()


def test_require_existing_store_allows_an_in_memory_store():
    require_existing_store(":memory:")  # does not raise


# --- reading the ledger ------------------------------------------------------


async def test_collect_summary_reduces_a_real_store(tmp_path):
    path = _db(tmp_path)
    await _seed(path, cost=0.25)

    summary = await collect_summary(path)

    assert [total.key for total in summary.by_project] == ["hate-moderator"]
    assert str(summary.grand_total) == "0.25"


async def test_collect_summary_of_a_store_with_no_finalized_jobs_is_empty(tmp_path):
    path = _db(tmp_path)
    async with Store(path):
        pass

    summary = await collect_summary(path)

    assert summary.rows == ()
    assert summary.grand_total == 0


# --- main() ------------------------------------------------------------------


def test_main_writes_a_page_for_a_real_ledger(tmp_path):
    path = _db(tmp_path)
    _seed_sync(path, cost=0.25)
    output = tmp_path / "costs.html"

    code = main(["--store-path", path, "--output", str(output)])

    assert code == 0
    page = output.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert '<div class="hero-value">$0.250000</div>' in page
    assert "hate-moderator" in page


def test_main_reports_what_it_wrote_on_stdout(tmp_path, capsys):
    path = _db(tmp_path)
    _seed_sync(path, cost=0.25)
    output = tmp_path / "costs.html"

    main(["--store-path", path, "--output", str(output)])

    assert "$0.250000 across 1 rows" in capsys.readouterr().out


def test_main_names_the_store_it_read_in_the_page(tmp_path):
    path = _db(tmp_path)
    _seed_sync(path)
    output = tmp_path / "costs.html"

    main(["--store-path", path, "--output", str(output)])

    assert f"<code>{path}</code>" in output.read_text(encoding="utf-8")


def test_main_creates_the_output_directory(tmp_path):
    path = _db(tmp_path)
    _seed_sync(path)
    output = tmp_path / "nested" / "deep" / "costs.html"

    assert main(["--store-path", path, "--output", str(output)]) == 0
    assert output.exists()


# A bare relative `--output` (no parent directory) is deliberately NOT tested by
# chdir-ing into tmp_path: mutmut's trampoline re-resolves its configured
# source_paths against the current working directory, so a test that moves the
# cwd makes the mutation run itself fail. It costs nothing to skip — a bare
# filename's parent is `Path(".")`, which takes the same mkdir call as every
# other path, so it is not a distinct branch to cover.


def test_main_renders_an_empty_store_as_an_explicit_empty_state(tmp_path):
    path = _db(tmp_path)
    _empty_store(path)
    output = tmp_path / "costs.html"

    assert main(["--store-path", path, "--output", str(output)]) == 0
    assert "No spend recorded yet" in output.read_text(encoding="utf-8")


def test_main_refuses_a_missing_store_rather_than_reporting_zero(tmp_path, capsys):
    absent = tmp_path / "absent.db"
    output = tmp_path / "costs.html"

    code = main(["--store-path", str(absent), "--output", str(output)])

    assert code == 2
    assert "nothing to report on" in capsys.readouterr().err
    # The whole point of the guard: no confident $0.00 page, no stray database.
    assert not output.exists()
    assert not absent.exists()


def test_main_refuses_when_no_store_path_is_configured_anywhere(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("llmbus.config.load_dotenv", lambda: None)
    monkeypatch.delenv("STORE_PATH", raising=False)

    code = main(["--output", str(tmp_path / "costs.html")])

    assert code == 2
    assert "missing required setting STORE_PATH" in capsys.readouterr().err
