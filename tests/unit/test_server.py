"""Unit tests for the cost-ledger HTTP server (§11).

Two kinds of test here. The fan-out over bind addresses uses an injected server
factory, so the multi-host logic is verified without opening real sockets. The
request/response behaviour uses one real `ThreadingHTTPServer` on an ephemeral
loopback port — no external network, no Iggy, no provider SDKs, and nothing that
survives the test.
"""

import asyncio
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from llmbus.config import CostsBind
from llmbus.schema import Job, Message, Result, Usage
from llmbus.server import (
    build_parser,
    build_servers,
    main,
    make_handler,
    render_page,
    resolve_bind,
    serve_forever,
)
from llmbus.store import Store

_SUBMITTED = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _seed(path, *, project="hate-moderator", cost=0.25):
    async def _write():
        async with Store(path) as store:
            job = Job(
                project=project,
                kind="classify",
                model="gpt-5-mini",
                messages=[Message(role="user", content="hi")],
                submitted_at=_SUBMITTED,
            )
            await store.insert_pending(job)
            await store.finalize(Result(job_id=job.job_id, status="ok", usage=Usage(cost_usd=cost)))

    asyncio.run(_write())


class _FakeServer:
    """Stands in for ThreadingHTTPServer: records where it would have listened."""

    def __init__(self, address, handler):
        self.server_address = address
        self.handler = handler
        self.shutdown_calls = 0
        self.closed = 0

    def serve_forever(self):
        return None

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.closed += 1


# --- binding: one socket per host -------------------------------------------


def test_build_servers_opens_one_server_per_host():
    servers = build_servers(
        "/x.db", CostsBind(("127.0.0.1", "100.124.41.86"), 8093), factory=_FakeServer
    )
    assert [server.server_address for server in servers] == [
        ("127.0.0.1", 8093),
        ("100.124.41.86", 8093),
    ]


def test_build_servers_gives_every_host_the_same_port():
    servers = build_servers("/x.db", CostsBind(("a", "b", "c"), 9999), factory=_FakeServer)
    assert {server.server_address[1] for server in servers} == {9999}


def test_build_servers_shares_one_handler_class_across_hosts():
    servers = build_servers("/x.db", CostsBind(("a", "b"), 1), factory=_FakeServer)
    assert servers[0].handler is servers[1].handler


def test_build_servers_with_a_single_host_opens_one_socket():
    servers = build_servers("/x.db", CostsBind(("127.0.0.1",), 8093), factory=_FakeServer)
    assert len(servers) == 1


# --- shutdown ---------------------------------------------------------------


def test_serve_forever_shuts_down_and_closes_every_server():
    servers = [_FakeServer(("127.0.0.1", 1), None), _FakeServer(("127.0.0.1", 2), None)]
    serve_forever(servers)
    assert [(s.shutdown_calls, s.closed) for s in servers] == [(1, 1), (1, 1)]


# --- rendering --------------------------------------------------------------


def test_render_page_reads_the_store_on_every_call(tmp_path):
    path = str(tmp_path / "store.db")
    _seed(path, cost=0.25)

    assert '<div class="hero-value">$0.250000</div>' in render_page(path)


def test_render_page_reflects_new_spend_without_a_restart(tmp_path):
    # The whole reason the page is served rather than generated once.
    path = str(tmp_path / "store.db")
    _seed(path, cost=0.25)
    assert "$0.250000" in render_page(path)

    _seed(path, project="beziarnia", cost=0.75)
    assert '<div class="hero-value">$1.000000</div>' in render_page(path)


# --- a real request over loopback -------------------------------------------


@pytest.fixture
def live_server(tmp_path):
    """A real server on an ephemeral loopback port, torn down after the test."""
    from http.server import ThreadingHTTPServer

    path = str(tmp_path / "store.db")
    _seed(path, cost=0.25)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_get_root_returns_the_dashboard(live_server):
    with urllib.request.urlopen(f"{live_server}/") as response:
        body = response.read().decode("utf-8")

    assert response.status == 200
    assert body.startswith("<!doctype html>")
    assert '<div class="hero-value">$0.250000</div>' in body


def test_get_root_declares_html_and_utf8(live_server):
    with urllib.request.urlopen(f"{live_server}/") as response:
        assert response.headers["Content-Type"] == "text/html; charset=utf-8"


def test_the_page_is_never_cached(live_server):
    # A cached ledger would show yesterday's total to someone checking today's.
    with urllib.request.urlopen(f"{live_server}/") as response:
        assert response.headers["Cache-Control"] == "no-store"


def test_index_html_serves_the_same_page(live_server):
    with urllib.request.urlopen(f"{live_server}/index.html") as response:
        assert response.status == 200


def test_any_other_path_is_a_404(live_server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"{live_server}/../etc/passwd")

    assert caught.value.code == 404


def test_a_tcp_connect_to_the_port_succeeds_which_is_milambers_health_check(live_server):
    # milamber's projects module calls it "online" purely on socket.create_connection
    # to 127.0.0.1:<port> (api/routers/projects.py). Binding loopback is what makes
    # the card's green dot true rather than decorative.
    import socket

    port = int(live_server.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass


# --- argument parsing and precedence ----------------------------------------


def test_parser_defaults_everything_to_the_environment():
    args = build_parser().parse_args([])
    assert (args.store_path, args.host, args.port) == (None, None, None)


def test_parser_collects_repeated_host_flags():
    args = build_parser().parse_args(["--host", "127.0.0.1", "--host", "100.124.41.86"])
    assert args.host == ["127.0.0.1", "100.124.41.86"]


def test_resolve_bind_prefers_explicit_flags(monkeypatch):
    monkeypatch.setenv("COSTS_BIND_HOSTS", "10.0.0.1")
    monkeypatch.setenv("COSTS_PORT", "9000")

    assert resolve_bind(["127.0.0.1"], 8093) == CostsBind(("127.0.0.1",), 8093)


def test_resolve_bind_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("COSTS_BIND_HOSTS", "127.0.0.1,100.124.41.86")
    monkeypatch.setenv("COSTS_PORT", "8093")

    assert resolve_bind(None, None) == CostsBind(("127.0.0.1", "100.124.41.86"), 8093)


def test_resolve_bind_mixes_a_flag_port_with_environment_hosts(monkeypatch):
    monkeypatch.setenv("COSTS_BIND_HOSTS", "127.0.0.1,100.124.41.86")
    monkeypatch.delenv("COSTS_PORT", raising=False)

    assert resolve_bind(None, 8500) == CostsBind(("127.0.0.1", "100.124.41.86"), 8500)


# --- main() -----------------------------------------------------------------


def test_main_refuses_a_missing_store_rather_than_serving_zero(tmp_path, capsys):
    code = main(["--store-path", str(tmp_path / "absent.db")])

    assert code == 2
    assert "nothing to report on" in capsys.readouterr().err


def test_main_refuses_an_unbindable_port(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "store.db")
    _seed(path)
    monkeypatch.setattr("llmbus.config.load_dotenv", lambda: None)
    monkeypatch.setenv("COSTS_PORT", "70000")

    code = main(["--store-path", path])

    assert code == 2
    assert "must be a valid port" in capsys.readouterr().err


def test_main_serves_then_returns_zero(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "store.db")
    _seed(path)
    monkeypatch.setattr("llmbus.config.load_dotenv", lambda: None)
    monkeypatch.setenv("COSTS_BIND_HOSTS", "127.0.0.1")
    monkeypatch.setenv("COSTS_PORT", "8093")
    monkeypatch.setattr("llmbus.server.ThreadingHTTPServer", _FakeServer)

    assert main(["--store-path", path]) == 0
    assert "serving" in capsys.readouterr().err


def test_main_announces_every_address_it_listens_on(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "store.db")
    _seed(path)
    monkeypatch.setattr("llmbus.config.load_dotenv", lambda: None)
    monkeypatch.setattr("llmbus.server.ThreadingHTTPServer", _FakeServer)

    main(["--store-path", path, "--host", "127.0.0.1", "--host", "100.124.41.86", "--port", "8093"])

    err = capsys.readouterr().err
    assert "127.0.0.1:8093, 100.124.41.86:8093" in err


def test_config_error_from_the_store_path_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("llmbus.config.load_dotenv", lambda: None)
    monkeypatch.delenv("STORE_PATH", raising=False)

    assert main([]) == 2
    assert "missing required setting STORE_PATH" in capsys.readouterr().err
