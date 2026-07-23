"""Unit tests for the cost-ledger HTTP server (§11).

Two kinds of test here. The fan-out over bind addresses uses an injected server
factory, so the multi-host logic is verified without opening real sockets. The
request/response behaviour uses one real `ThreadingHTTPServer` on an ephemeral
loopback port — no external network, no Iggy, no provider SDKs, and nothing that
survives the test.
"""

import asyncio
import socket
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from llmbus.config import CostsBind
from llmbus.schema import Job, Message, Result, Usage
from llmbus.server import (
    _MAX_FORM_BYTES,
    build_parser,
    build_servers,
    main,
    make_handler,
    parse_content_length,
    parse_policy_form,
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


def test_build_servers_closes_open_sockets_when_a_later_bind_fails():
    opened = []

    def fail_second_bind(address, handler):
        if opened:
            raise OSError("second address is unavailable")
        server = _FakeServer(address, handler)
        opened.append(server)
        return server

    with pytest.raises(OSError, match="second address is unavailable"):
        build_servers(
            "/x.db",
            CostsBind(("127.0.0.1", "100.124.41.86"), 8093),
            factory=fail_second_bind,
        )

    assert opened[0].closed == 1


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


def test_resolve_bind_deduplicates_explicit_hosts(monkeypatch):
    monkeypatch.delenv("COSTS_BIND_HOSTS", raising=False)

    assert resolve_bind(["127.0.0.1", "127.0.0.1"], 8093) == CostsBind(
        ("127.0.0.1",),
        8093,
    )


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


@pytest.mark.parametrize("host", ["0.0.0.0", ""])
def test_main_refuses_an_explicit_unsafe_host(host, tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "store.db")
    _seed(path)
    monkeypatch.setattr("llmbus.config.load_dotenv", lambda: None)
    monkeypatch.setattr("llmbus.server.ThreadingHTTPServer", _FakeServer)

    code = main(["--store-path", path, "--host", host])

    assert code == 2
    assert "llmbus-costs-serve:" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "-1", "70000"])
def test_main_refuses_an_explicit_unbindable_port(port, tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "store.db")
    _seed(path)
    monkeypatch.setattr("llmbus.config.load_dotenv", lambda: None)
    monkeypatch.setattr("llmbus.server.ThreadingHTTPServer", _FakeServer)

    code = main(["--store-path", path, "--port", port])

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


# --- the policy page: form parsing (§14 #23) ---------------------------------


def test_parse_policy_form_reads_all_three_fields():
    assert parse_policy_form("project=milamber&kind=language.chat&model=gpt-5.4") == (
        "milamber",
        "language.chat",
        "gpt-5.4",
    )


def test_parse_policy_form_strips_surrounding_whitespace():
    assert parse_policy_form("project=+milamber+&kind=+a+&model=gpt-5.4") == (
        "milamber",
        "a",
        "gpt-5.4",
    )


@pytest.mark.parametrize("missing", ["project", "kind", "model"])
def test_parse_policy_form_requires_every_field(missing):
    fields = {"project": "milamber", "kind": "a", "model": "gpt-5.4"}
    del fields[missing]
    body = "&".join(f"{k}={v}" for k, v in fields.items())

    with pytest.raises(ValueError, match=f"missing required field '{missing}'"):
        parse_policy_form(body)


@pytest.mark.parametrize("blank", ["", "+", "%20"])
def test_parse_policy_form_treats_a_blank_kind_as_missing(blank):
    # A blank kind would create a policy row no job can ever match.
    with pytest.raises(ValueError, match="kind"):
        parse_policy_form(f"project=milamber&kind={blank}&model=gpt-5.4")


def test_parse_policy_form_refuses_a_model_the_bus_does_not_route():
    # The form only offers registered models, but a form is a client-side
    # construct and this is a write endpoint: anyone authenticated can post
    # whatever they like.
    with pytest.raises(ValueError, match="not registered with the bus"):
        parse_policy_form("project=milamber&kind=a&model=gpt-9000")


def test_parse_policy_form_ignores_unknown_extra_fields():
    assert parse_policy_form("project=a&kind=b&model=gpt-5.4&surprise=1")[0] == "a"


# --- the policy page: live HTTP ----------------------------------------------


_SECRET = "s3cret"


def _auth_header(password=_SECRET):
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"x:{password}".encode()).decode()}


@pytest.fixture
def policy_server(tmp_path):
    """A real server WITH a secret configured, on an ephemeral loopback port."""
    from http.server import ThreadingHTTPServer

    path = str(tmp_path / "store.db")
    _seed(path)

    async def _policy():
        async with Store(path) as store:
            await store.set_model_policy("milamber", "language.chat", "gpt-5.5")

    asyncio.run(_policy())
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(path, _SECRET))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", path
    finally:
        server.shutdown()
        server.server_close()


def _request(url, *, method="GET", headers=None, data=None):
    request = urllib.request.Request(
        url, method=method, headers=headers or {}, data=data.encode() if data else None
    )
    return urllib.request.urlopen(request)


def _raw_policy_request(base, headers, body=b""):
    """Send wire-level requests urllib correctly refuses to construct."""
    host, port = base.removeprefix("http://").rsplit(":", 1)
    request = (
        f"POST /policy HTTP/1.1\r\nHost: {host}:{port}\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        + "Connection: close\r\n\r\n"
    ).encode() + body
    with socket.create_connection((host, int(port)), timeout=1) as client:
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        return client.recv(4096)


def test_the_cost_page_still_needs_no_credentials(policy_server):
    base, _ = policy_server
    with _request(f"{base}/") as response:
        assert response.status == 200


def test_the_policy_page_demands_credentials(policy_server):
    base, _ = policy_server
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(f"{base}/policy")

    assert caught.value.code == 401
    assert caught.value.headers["WWW-Authenticate"] == 'Basic realm="llmbus policy"'


def test_a_wrong_secret_is_refused(policy_server):
    base, _ = policy_server
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(f"{base}/policy", headers=_auth_header("wrong"))

    assert caught.value.code == 401


def test_the_right_secret_shows_the_policy(policy_server):
    base, _ = policy_server
    with _request(f"{base}/policy", headers=_auth_header()) as response:
        body = response.read().decode()

    assert response.status == 200
    assert "language.chat" in body
    assert '<option value="gpt-5.5" selected>' in body


def test_posting_a_policy_writes_it_and_reports_back(policy_server):
    base, path = policy_server

    with _request(
        f"{base}/policy",
        method="POST",
        headers=_auth_header(),
        data="project=milamber&kind=language.chat&model=gpt-5-nano",
    ) as response:
        body = response.read().decode()

    assert response.status == 200
    assert "milamber/language.chat now runs on gpt-5-nano" in body

    async def _read():
        async with Store(path) as store:
            return await store.model_policy("milamber", "language.chat")

    assert asyncio.run(_read()).model == "gpt-5-nano"


def test_concurrent_policy_posts_commit_each_distinct_pair(policy_server):
    # The handler creates a Store per request on a ThreadingHTTPServer thread;
    # exercise those separate SQLite connections rather than a mocked store.
    base, path = policy_server

    def post(index):
        data = f"project=project-{index}&kind=language.chat&model=gpt-5-nano"
        with _request(
            f"{base}/policy", method="POST", headers=_auth_header(), data=data
        ) as response:
            return response.status

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post, range(8)))

    assert statuses == [200] * 8

    async def _read():
        async with Store(path) as store:
            return await store.list_model_policies()

    policies = asyncio.run(_read())
    assert {(policy.project, policy.kind, policy.model) for policy in policies} >= {
        (f"project-{index}", "language.chat", "gpt-5-nano") for index in range(8)
    }


def test_posting_without_credentials_changes_nothing(policy_server):
    base, path = policy_server
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(
            f"{base}/policy",
            method="POST",
            data="project=milamber&kind=language.chat&model=gpt-5-nano",
        )

    assert caught.value.code == 401

    async def _read():
        async with Store(path) as store:
            return await store.model_policy("milamber", "language.chat")

    assert asyncio.run(_read()).model == "gpt-5.5"


def test_a_cross_origin_post_is_refused(policy_server):
    # Browsers re-send cached Basic credentials, so authentication alone would
    # not stop another origin driving this endpoint.
    base, _ = policy_server
    headers = _auth_header() | {"Origin": "http://evil.example"}

    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(
            f"{base}/policy",
            method="POST",
            headers=headers,
            data="project=a&kind=b&model=gpt-5.4",
        )

    assert caught.value.code == 403


def test_a_bad_model_is_a_400_that_re_renders_the_page(policy_server):
    base, _ = policy_server
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(
            f"{base}/policy",
            method="POST",
            headers=_auth_header(),
            data="project=a&kind=b&model=gpt-9000",
        )

    assert caught.value.code == 400
    assert "not registered with the bus" in caught.value.read().decode()


def test_an_oversized_form_is_refused_before_being_read(policy_server):
    base, _ = policy_server
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(
            f"{base}/policy",
            method="POST",
            headers=_auth_header(),
            data="project=a&kind=b&model=gpt-5.4&pad=" + "x" * 5000,
        )

    assert caught.value.code == 413


@pytest.mark.parametrize("content_length", ["-1", "not-a-number"])
def test_malformed_content_length_is_a_400_and_never_writes_a_policy(policy_server, content_length):
    # Content-Length is untrusted input. Negative values must not become
    # rfile.read(-1) (read until EOF), and an invalid integer must not abort the
    # handler without an HTTP response.
    base, path = policy_server
    response = _raw_policy_request(
        base,
        _auth_header() | {"Content-Length": content_length},
        b"project=attacker&kind=language.chat&model=gpt-5-nano",
    )

    assert response.startswith(b"HTTP/1.1 400")

    async def _read():
        async with Store(path) as store:
            return await store.model_policy("attacker", "language.chat")

    assert asyncio.run(_read()) is None


def test_a_body_shorter_than_its_content_length_is_a_400_and_never_writes(policy_server):
    # EOF before the declared length is malformed framing, not permission to
    # parse and commit a partial body. A client can otherwise claim one more
    # byte than it sends and still make a valid form take effect.
    base, path = policy_server
    body = b"project=attacker&kind=language.chat&model=gpt-5-nano"
    response = _raw_policy_request(
        base,
        _auth_header() | {"Content-Length": str(len(body) + 1)},
        body,
    )

    assert response.startswith(b"HTTP/1.1 400")

    async def _read():
        async with Store(path) as store:
            return await store.model_policy("attacker", "language.chat")

    assert asyncio.run(_read()) is None


def test_posting_to_an_unknown_path_is_a_404(policy_server):
    base, _ = policy_server
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(f"{base}/nope", method="POST", headers=_auth_header(), data="x=1")

    assert caught.value.code == 404


# --- with no secret configured, the write surface does not exist -------------


@pytest.fixture
def unsecured_server(tmp_path):
    from http.server import ThreadingHTTPServer

    path = str(tmp_path / "store.db")
    _seed(path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_without_a_secret_the_policy_page_is_off_not_open(unsecured_server):
    # The fail-safe: upgrading without setting COSTS_AUTH_SECRET must not hand
    # anyone on the tailnet a way to change which model every project runs on.
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(f"{unsecured_server}/policy")

    assert caught.value.code == 503
    assert "COSTS_AUTH_SECRET" in caught.value.read().decode()


def test_without_a_secret_a_post_is_also_refused(unsecured_server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(
            f"{unsecured_server}/policy",
            method="POST",
            data="project=a&kind=b&model=gpt-5.4",
        )

    assert caught.value.code == 503


def test_without_a_secret_the_cost_page_is_unaffected(unsecured_server):
    with _request(f"{unsecured_server}/") as response:
        assert response.status == 200


# --- Content-Length is attacker-controlled input (§14 #23) -------------------


def test_absent_content_length_is_a_body_less_post_not_an_error():
    assert parse_content_length(None) == 0


def test_a_declared_length_is_returned():
    assert parse_content_length("42") == 42


def test_zero_is_a_legal_length():
    assert parse_content_length("0") == 0


@pytest.mark.parametrize("raw", ["-1", "-4096"])
def test_a_negative_length_is_refused(raw):
    # The dangerous one: rfile.read(-1) means "read until EOF", so a negative
    # value would BOTH skip the size cap and stream unbounded data into memory.
    with pytest.raises(ValueError, match="negative Content-Length"):
        parse_content_length(raw)


@pytest.mark.parametrize("raw", ["not-a-number", "", "1.5", "0x10", "4 096"])
def test_a_non_numeric_length_raises_rather_than_escaping_the_handler(raw):
    # An uncaught ValueError kills the serving thread and the client gets no
    # HTTP response at all.
    with pytest.raises(ValueError):
        parse_content_length(raw)


def test_a_form_exactly_at_the_cap_is_accepted(policy_server):
    # Pins the boundary as <=, not <: a body of exactly _MAX_FORM_BYTES is legal.
    base, path = policy_server
    prefix = "project=edge&kind=k&model=gpt-5-nano&pad="
    body = prefix + "x" * (_MAX_FORM_BYTES - len(prefix))
    assert len(body) == _MAX_FORM_BYTES

    with _request(f"{base}/policy", method="POST", headers=_auth_header(), data=body) as response:
        assert response.status == 200

    async def _read():
        async with Store(path) as store:
            return await store.model_policy("edge", "k")

    assert asyncio.run(_read()).model == "gpt-5-nano"


def test_one_byte_over_the_cap_is_refused(policy_server):
    base, path = policy_server
    prefix = "project=over&kind=k&model=gpt-5-nano&pad="
    body = prefix + "x" * (_MAX_FORM_BYTES - len(prefix) + 1)

    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(f"{base}/policy", method="POST", headers=_auth_header(), data=body)

    assert caught.value.code == 413

    async def _read():
        async with Store(path) as store:
            return await store.model_policy("over", "k")

    assert asyncio.run(_read()) is None
