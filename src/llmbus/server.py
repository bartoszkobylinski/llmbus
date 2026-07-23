"""`llmbus-costs-serve` — the cost ledger over HTTP on the tailnet (§11).

Same page as `llmbus-costs`, re-rendered on every request instead of written once
to a file. It exists so milamber's projects module can point at it: that module is
a *registry, not a proxy* — it stores a port, shows a card "online" by opening a
TCP connection to `127.0.0.1:<port>`, and links to `http://<the host you are
browsing>:<port>`. Nothing there forwards traffic, so this process has to be
listening on the tailnet interface itself.

**Why it binds more than one address.** The health check wants loopback; the
browser wants the tailnet address. Binding `0.0.0.0` satisfies both but also puts
per-project spend on the box's public interface with no authentication, and
binding only the tailnet address (the `capcycle-web` pattern) leaves the card
permanently showing a false "Offline". So the server opens one socket per host in
`COSTS_BIND_HOSTS` — loopback *and* tailnet, neither public — which needs no
nginx vhost and so stays clear of the 443/tailscaled ordering traps in the
runbook. The tailnet address is config, never a literal here (§10).

No web framework: the standard library's threading HTTP server is enough for a
read-only page refreshed by one person, and pulling FastAPI/uvicorn into a *bus*
for this would be exactly the scope creep §1 exists to prevent. `asyncio.run` per
request is safe because each request is served on its own thread, which has no
running loop — the same bridge-at-your-own-edge shape as `cli.py` (§14 #17).

The page has no authentication. That is deliberate and is why the default bind is
loopback: on this box the network boundary *is* the access control (the same
Phase-0 stance the runbook records for `capcycle-web`). Do not widen the bind to
`0.0.0.0` without adding auth first.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseServer
from urllib.parse import parse_qs

from llmbus.cli import collect_summary, require_existing_store, resolve_store_path
from llmbus.config import (
    ConfigError,
    CostsBind,
    load_costs_bind,
    load_costs_secret,
    validate_costs_hosts,
    validate_costs_port,
)
from llmbus.dashboard import render_dashboard
from llmbus.policy_page import render_policy_page
from llmbus.providers.base import CAPABILITIES
from llmbus.store import ModelPolicy, Store
from llmbus.webauth import BASIC_REALM, basic_auth_ok, origin_allowed

# Only the ledger itself is served. No static assets, no other routes: the page
# inlines its own CSS and pulls nothing, so anything else is a 404 by design.
_PAGE_PATHS = frozenset({"/", "/index.html"})
_POLICY_PATH = "/policy"
# A policy form is a handful of short fields. Anything larger is not a form, and
# reading it into memory would be the only interesting thing an unauthenticated
# caller could make this process do.
_MAX_FORM_BYTES = 4096

ServerFactory = Callable[[tuple[str, int], type[BaseHTTPRequestHandler]], ThreadingHTTPServer]


def render_page(store_path: str) -> str:
    """Read the ledger and render it, stamped with the moment it was read."""
    summary = asyncio.run(collect_summary(store_path))
    return render_dashboard(summary, datetime.now(UTC), store_path)


def render_policy_view(store_path: str, notice: str | None = None) -> str:
    """Read the policy table and render it, re-reading on every request."""
    return render_policy_page(asyncio.run(read_policies(store_path)), datetime.now(UTC), notice)


async def read_policies(store_path: str) -> list[ModelPolicy]:
    """Every policy row, for the policy page."""
    async with Store(store_path) as store:
        return await store.list_model_policies()


async def write_policy(store_path: str, project: str, kind: str, model: str) -> None:
    """Point `(project, kind)` at `model`."""
    async with Store(store_path) as store:
        await store.set_model_policy(project, kind, model)


def parse_policy_form(body: str) -> tuple[str, str, str]:
    """Pull `project`, `kind` and `model` out of a form body, or raise `ValueError`.

    Pure, so the parsing rules are testable without a socket. Values are stripped
    and all three are required: a blank `kind` would create a row no job can match,
    and a blank `model` would write an empty string into the policy the producer
    then resolves — both failures that surface far from here.

    The model is checked against the registry rather than trusted. The form only
    offers registered models, but a form is a client-side construct and this is a
    write endpoint: anyone authenticated can post arbitrary fields.
    """
    fields = parse_qs(body, keep_blank_values=True)
    values = []
    for name in ("project", "kind", "model"):
        raw = fields.get(name, [""])[0].strip()
        if not raw:
            raise ValueError(f"missing required field {name!r}")
        values.append(raw)
    project, kind, model = values
    if model not in CAPABILITIES:
        raise ValueError(f"model {model!r} is not registered with the bus")
    return project, kind, model


def make_handler(store_path: str, secret: str | None = None) -> type[BaseHTTPRequestHandler]:
    """A request handler bound to one store. Re-reads on every GET.

    Built by a factory rather than configured through a class attribute so two
    servers (loopback and tailnet) can share one handler class without a mutable
    global, and so tests can point a handler at a temp store.
    """

    class _CostHandler(BaseHTTPRequestHandler):
        server_version = "llmbus-costs"
        protocol_version = "HTTP/1.1"

        def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            # Spend and policy both change under the reader; a cached page would
            # quietly show a stale total, or a model that is no longer in force.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _guard(self) -> bool:
            """True when the caller may use the policy page; else it has replied.

            Order matters. An unconfigured secret answers 503 *before* asking for
            credentials, so an operator who never set `COSTS_AUTH_SECRET` is told
            the page is off rather than being prompted for a password that cannot
            exist.
            """
            if secret is None:
                self.send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "policy page disabled: COSTS_AUTH_SECRET is not set",
                )
                return False
            if not basic_auth_ok(self.headers.get("Authorization"), secret):
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("WWW-Authenticate", f'Basic realm="{BASIC_REALM}"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            if self.path in _PAGE_PATHS:
                self._send_html(render_page(store_path))
                return
            if self.path == _POLICY_PATH:
                if self._guard():
                    self._send_html(render_policy_view(store_path))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "no such page")

        def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            if self.path != _POLICY_PATH:
                self.send_error(HTTPStatus.NOT_FOUND, "no such page")
                return
            if not self._guard():
                return
            # Browsers cache Basic credentials and re-send them, so a page on
            # another origin could otherwise drive this endpoint (webauth.py).
            if not origin_allowed(self.headers.get("Origin"), self.headers.get("Host")):
                self.send_error(HTTPStatus.FORBIDDEN, "cross-origin write refused")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_FORM_BYTES:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "form too large")
                return
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                project, kind, model = parse_policy_form(raw)
            except ValueError as error:
                self._send_html(
                    render_policy_view(store_path, notice=str(error)),
                    HTTPStatus.BAD_REQUEST,
                )
                return
            asyncio.run(write_policy(store_path, project, kind, model))
            self._send_html(
                render_policy_view(store_path, notice=f"{project}/{kind} now runs on {model}")
            )

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # journalctl already timestamps; keep one line per request, on stderr.
            print(f"llmbus-costs: {self.address_string()} {format % args}", file=sys.stderr)

    return _CostHandler


def build_servers(
    store_path: str,
    bind: CostsBind,
    factory: ServerFactory | None = None,
) -> list[ThreadingHTTPServer]:
    """One listening server per configured host, all sharing a handler.

    `factory` is injected so the fan-out is testable without binding real
    sockets — the same seam `config.build_providers` uses for the SDK clients.
    It defaults to `None` and resolves to `ThreadingHTTPServer` *inside* the
    call, not as a default argument: a default is bound once at definition time,
    so `ThreadingHTTPServer` would already be captured and a test that patches
    the module attribute would silently open a real socket instead.
    """
    server_factory = factory or ThreadingHTTPServer
    handler = make_handler(store_path, load_costs_secret())
    opened: list[ThreadingHTTPServer] = []
    try:
        for host in bind.hosts:
            opened.append(server_factory((host, bind.port), handler))
    except OSError:
        # A partial bind must not leak the sockets it did open. Failing on the
        # second address (tailscale0 not up yet — the documented cold-boot case)
        # would otherwise leave the first listening on a half-started service and
        # hold the port, so systemd's restart hits EADDRINUSE and never recovers.
        for server in opened:
            server.server_close()
        raise
    return opened


def serve_forever(servers: Sequence[BaseServer]) -> None:
    """Run every server until interrupted, then shut them all down."""
    threads = [
        threading.Thread(target=server.serve_forever, name=f"llmbus-costs-{index}", daemon=True)
        for index, server in enumerate(servers)
    ]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:  # pragma: no cover - interactive path only
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def build_parser() -> argparse.ArgumentParser:
    """CLI surface. Every default is overridable; no address is baked in (§10)."""
    parser = argparse.ArgumentParser(
        prog="llmbus-costs-serve",
        description="Serve the llmbus cost ledger over HTTP (read-only, no auth).",
    )
    parser.add_argument(
        "--store-path",
        default=None,
        help="SQLite results store to read (default: STORE_PATH from .env/environment).",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=None,
        help="Address to bind; repeatable (default: COSTS_BIND_HOSTS, else 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind (default: COSTS_PORT, else 8093).",
    )
    return parser


def resolve_bind(hosts: Sequence[str] | None, port: int | None) -> CostsBind:
    """CLI flags win over the environment, per field, like `resolve_store_path`.

    Flags go through the *same* validation as `.env` rather than straight into a
    socket: an explicit `--host 0.0.0.0` (or `--host ""`, which the socket layer
    reads as the same wildcard) would otherwise publish the unauthenticated page
    on the public interface, and an explicit `--port 70000` would skip the range
    check and fail deep in `bind()` instead of at the boundary.
    """
    configured = load_costs_bind()
    return CostsBind(
        hosts=validate_costs_hosts(hosts) if hosts else configured.hosts,
        port=validate_costs_port(port) if port is not None else configured.port,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `llmbus-costs-serve`. Blocks until killed."""
    args = build_parser().parse_args(argv)
    try:
        store_path = resolve_store_path(args.store_path)
        require_existing_store(store_path)
        bind = resolve_bind(args.host, args.port)
    except ConfigError as error:
        print(f"llmbus-costs-serve: {error}", file=sys.stderr)
        return 2
    servers = build_servers(store_path, bind)
    listening = ", ".join(f"{host}:{bind.port}" for host in bind.hosts)
    print(f"llmbus-costs-serve: serving {store_path} on {listening}", file=sys.stderr)
    serve_forever(servers)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
