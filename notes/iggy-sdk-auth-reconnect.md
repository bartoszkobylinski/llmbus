# Iggy SDK 0.8.0 — manual `login_user` does not survive the SDK's own reconnect

Full record of the 2026-07-16/17 investigation. Kept verbatim (evidence, exact command
output, both repro scripts, both draft messages) because it is the basis of a possible
upstream report to Apache Iggy, and because a summary would lose exactly the details
that make it defensible.

**Our fix is done and does not depend on upstream** — see ARCHITECTURE.md §14 #16 and
the `iggy-connection-string` PR. This file is the *evidence file*.

- Investigated against: `apache-iggy` **0.8.0** (PyPI sdist, sha256
  `f95e48eff23db962a290cbfe587dc7cc1bf2fe707c5462f39554c2741fd33c4e`, matches `uv.lock`,
  i.e. the exact source of the installed `.so`).
- Broker: `iggy-server` 0.8.0 on izabela213, `127.0.0.1:8092`, up since 2026-07-15
  06:43:24, `NRestarts=0`.

---

## 1. The finding, in one paragraph

Iggy authenticates **per TCP session**. A client built as `IggyClient(addr)` followed by
an explicit `await login_user(...)` leaves the SDK's `auto_login` **Disabled**, so
`connect()` never authenticates. Every command goes through the SDK's
`send_raw_with_response`, which on a transient error — **and on `Unauthenticated`
itself** — does `disconnect()` → `connect()` → resend. With `auto_login` Disabled that
reconnect silently comes back on an **unauthenticated** session, because the SDK holds
no credentials to replay. The command then fails `Unauthenticated`, which triggers
another credential-less reconnect, which fails identically. **It cannot self-heal.**
`IggyClient.from_connection_string("iggy+tcp://user:pass@host:port")` sets
`auto_login: Enabled(UsernamePassword)`, so the SDK logs in *inside* `connect()` —
including on every internal reconnect — and recovers transparently.

---

## 2. What is PROVEN (reproduced on demand)

### 2.1 `connect()` authenticates only in the connection-string form

Run on the VPS against the live broker, read-only, **neither client calling
`login_user`**:

```
A  IggyClient(addr)          connect() -> get_stream: RuntimeError: Unauthenticated   <- connect() did NOT authenticate
B  from_connection_string    connect() -> get_stream: OK (found=True)   <- connect() DID authenticate
```

### 2.2 The reconnect loses authentication permanently (the decisive one)

A local TCP proxy between client and broker, severed deliberately. No broker restart,
nothing created or deleted:

```
A manual  IggyClient(addr)+login_user: before cut -> OK
A manual  IggyClient(addr)+login_user: AFTER RECONNECT -> RuntimeError: Unauthenticated
B connstr from_connection_string     : before cut -> OK
B connstr from_connection_string     : AFTER RECONNECT -> OK  (re-authenticated)
```

Same broker, same credentials, same cut. Manual login: dead for good. Connection
string: back instantly.

### 2.3 Source, from the sdist (they can read it themselves)

`core/sdk/src/tcp/tcp_client.rs`, `send_raw_with_response` — the path **every** command
takes:

```rust
async fn send_raw_with_response(&self, code: u32, payload: Bytes) -> Result<Bytes, IggyError> {
    let result = self.send_raw(code, payload.clone()).await;
    if result.is_ok() {
        return result;
    }

    let error = result.unwrap_err();
    if !matches!(
        error,
        IggyError::Disconnected
            | IggyError::EmptyResponse
            | IggyError::Unauthenticated
            | IggyError::StaleClient
            | IggyError::NotConnected
            | IggyError::CannotEstablishConnection
            | IggyError::TcpError
    ) {
        return Err(error);
    }

    if !self.config.reconnection.enabled {
        return Err(IggyError::Disconnected);
    }

    self.disconnect().await?;
    // ... info!("Reconnecting to the server: {} by client: {client_address}...")
    self.connect().await?;
    self.send_raw(code, payload).await
}
```

Note `IggyError::Unauthenticated` is itself in the reconnect-trigger list — that is why
it loops instead of failing once.

`core/sdk/src/tcp/tcp_client.rs`, inside `connect()` — the auto-login branch:

```rust
// Handle auto-login
let should_redirect = match &self.config.auto_login {
    AutoLogin::Disabled => {
        info!("Automatic sign-in is disabled.");
        false
    }
    AutoLogin::Enabled(credentials) => {
        info!("{NAME} client: {client_address} is signing in...");
        self.set_state(ClientState::Authenticating).await;
        match credentials {
            Credentials::UsernamePassword(username, password) => {
                self.login_user(username, password.expose_secret()).await?;
                ...
            }
            ...
        }
        self.handle_leader_redirection().await?
    }
};
```

`core/common/src/types/configuration/auth_config/connection_string.rs:117` — the
connection string is what sets it:

```rust
auto_login: AutoLogin::Enabled(Credentials::UsernamePassword(
```

### 2.4 Defaults that matter (`tcp_client.rs`, asserted by their own test ~line 775)

```rust
assert_eq!(tcp_client_config.heartbeat_interval, IggyDuration::from_str("5s").unwrap());
assert!(tcp_client_config.reconnection.enabled);
assert!(tcp_client_config.reconnection.max_retries.is_none());
assert_eq!(tcp_client_config.reconnection.interval, IggyDuration::from_str("1s").unwrap());
assert_eq!(tcp_client_config.reconnection.reestablish_after, IggyDuration::from_str("5s").unwrap());
```

`reconnection.enabled` is **true by default**, and `reestablish_after` is **5s** —
`connect()` sleeps out the remainder of that window before reconnecting:

```rust
self.set_state(ClientState::Connecting).await;
if let Some(connected_at) = self.connected_at.lock().await.as_ref() {
    let now = IggyTimestamp::now();
    let elapsed = now.as_micros() - connected_at.as_micros();
    let interval = self.config.reconnection.reestablish_after.as_micros();
    ...
    if elapsed < interval {
        let remaining = IggyDuration::from(interval - elapsed);
        info!("Trying to connect to the server in: {remaining}",);
        sleep(remaining.get_duration()).await;
    }
}
```

This is the **exact 5-second gap** observed in production between the successful login
and the crash (login 20:47:06, crash 20:47:11).

### 2.5 Their Python binding is NOT at fault

`foreign/python/src/client.rs` is a thin PyO3 wrapper; `connect` and `login_user` just
delegate to the core Rust client:

```rust
fn connect<'a>(&self, py: Python<'a>) -> PyResult<Bound<'a, PyAny>> {
    let inner = self.inner.clone();
    future_into_py(py, async move {
        inner
            .connect()
            .await
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!("{e:?}")))?;
        Ok(())
    })
}
```

and construction:

```rust
fn new(conn: Option<String>) -> Self {
    let client = IggyClientBuilder::new()
        .with_tcp()
        .with_server_address(conn.unwrap_or("127.0.0.1:8090".to_string()))
        .build()
        .unwrap();
    IggyClient { inner: Arc::new(client) }
}
```

No credentials → `auto_login` Disabled.

### 2.6 Their own Python reference usage is the unsafe form

Every client construction in `foreign/python/tests/`:

| Where | Pattern |
|---|---|
| `conftest.py:55` — the primary session fixture every test uses | `IggyClient(f"{host}:{port}")` + `connect()` + `wait_for_ping()` + `login_user()` — **the manual form** |
| `test_iggy_sdk.py:55` | `from_connection_string` — but it is a test *of the constructor* |
| `test_tls.py:86,168` | `from_connection_string` — TLS **requires** the connection string |
| `test_tls.py:185` | `from_connection_string` — a **negative** test (expects failure) |

`foreign/python/README.md` is 57 lines and contains **no client construction at all**
(no `IggyClient`, no `login`, no `connection_string`). So `conftest.py` is the only
Python reference usage in the repository, and it teaches the unsafe form.

Their fixture verbatim (`foreign/python/tests/conftest.py`):

```python
    host, port = get_server_config()

    # Wait for server to be ready
    wait_for_server(host, port)

    # Create and connect client
    client = IggyClient(f"{host}:{port}")
    await client.connect()

    # Wait for server to be fully ready
    await wait_for_ping(client)

    # Authenticate
    await client.login_user("iggy", "iggy")

    return client
```

Their tests do not catch this because they are short-lived, run against a fresh server,
and never reconnect.

### 2.7 The swallow that hid it

`core/sdk/src/leader_aware.rs:38` — `check_and_redirect_to_leader`:

```rust
    match client.get_cluster_metadata().await {
        Ok(metadata) => {
            debug!(
                "Got cluster metadata: {} nodes, cluster: {}",
                metadata.nodes.len(),
                metadata.name
            );
            process_cluster_metadata(&metadata, current_address, transport)
        }
        Err(e) => {
            warn!(
                "Failed to get cluster metadata: {}, connection will continue on server node {}",
                e, current_address
            );
            Ok(None)
        }
    }
```

Reached from `core/sdk/src/clients/binary_users.rs:95` — the high-level `login_user`:

```rust
    async fn login_user(&self, username: &str, password: &str) -> Result<IdentityInfo, IggyError> {
        let identity = self
            .client
            .read()
            .await
            .login_user(username, password)
            .await?;

        let should_redirect = {
            let client = self.client.read().await;
            match &*client {
                ClientWrapper::Tcp(tcp_client) => tcp_client.handle_leader_redirection().await?,
                ClientWrapper::Quic(quic_client) => quic_client.handle_leader_redirection().await?,
                ClientWrapper::WebSocket(ws_client) => {
                    ws_client.handle_leader_redirection().await?
                }
                _ => false,
            }
        };

        if should_redirect {
            info!("Redirected to leader, reconnecting and re-authenticating");
            self.connect().await?;
            self.login_user(username, password).await
        } else {
            Ok(identity)
        }
    }
```

Consequence: `login_user()` returns `Ok` even when the `get_cluster_metadata` call
inside its own flow was rejected `Unauthenticated`. Presumably deliberate (best-effort
leader detection) — it **hides** the failure but does not cause it.

### 2.8 Production broker log, verbatim (the original symptom)

`journalctl -u iggy-server`, 2026-07-16 20:47:06 — login succeeds on one session while
`cluster.metadata` is rejected on a **different** session:

```
20:47:06.781864 INFO shard-0 server::tcp::tcp_listener: Accepted new TCP connection: 127.0.0.1:36570
20:47:06.781908 INFO shard-0 server::tcp::tcp_listener: Added tcp client with session: client ID: 2080111484, IP address: 127.0.0.1:36570 for IP address: 127.0.0.1:36570
20:47:06.781914 INFO shard-0 server::tcp::tcp_listener: Created new session: client ID: 2080111484, IP address: 127.0.0.1:36570
20:47:06.782471 INFO shard-0 server::tcp::tcp_listener: Accepted new TCP connection: 127.0.0.1:36584
20:47:06.782489 INFO shard-0 server::tcp::tcp_listener: Added tcp client with session: client ID: 2080309522, IP address: 127.0.0.1:36584 for IP address: 127.0.0.1:36584
20:47:06.782493 INFO shard-0 server::tcp::tcp_listener: Created new session: client ID: 2080309522, IP address: 127.0.0.1:36584
20:47:06.782697 INFO shard-0 trace_login_user{iggy_user_id=4294967295 iggy_client_id=2080111484}: server::binary::handlers::users::login_user_handler: Logging in user: iggy ...
20:47:06.868434 INFO shard-0 trace_login_user{iggy_user_id=4294967295 iggy_client_id=2080111484}: server::binary::handlers::users::login_user_handler: Logged in user: iggy with ID: 0.
20:47:06.868637 ERROR shard-0 trace_get_cluster_metadata{iggy_user_id=4294967295 iggy_client_id=2080309522}: server::shard: SHARD - unauthenticated access attempt, session: client ID: 2080309522, IP address: 127.0.0.1:36584
20:47:06.868647 ERROR shard-0 server::tcp::connection_handler: Command with code 12 (cluster.metadata) was not handled successfully, session: client ID: 2080309522, IP address: 127.0.0.1:36584, error: Unauthenticated.
20:47:06.868861 INFO shard-0 server::shard::system::clients: Deleted tcp client with ID: 2080111484 for IP address: 127.0.0.1:36570
20:47:06.868965 INFO shard-0 server::tcp::tcp_listener: Successfully closed for client 2080111484, address 127.0.0.1:36570
20:47:06.868976 INFO shard-0 server::shard::system::clients: Deleted tcp client with ID: 2080309522 for IP address: 127.0.0.1:36584
20:47:06.868995 INFO shard-0 server::tcp::tcp_listener: Successfully closed for client 2080309522, address 127.0.0.1:36584
```

Five seconds later — the crash (`reestablish_after` = 5s):

```
20:47:11.788004 INFO shard-0 server::tcp::tcp_listener: Accepted new TCP connection: 127.0.0.1:36938
20:47:11.788058 INFO shard-0 server::tcp::tcp_listener: Added tcp client with session: client ID: 2830571172, IP address: 127.0.0.1:36938 for IP address: 127.0.0.1:36938
20:47:11.788064 INFO shard-0 server::tcp::tcp_listener: Created new session: client ID: 2830571172, IP address: 127.0.0.1:36938
20:47:11.788122 ERROR shard-0 trace_get_cluster_metadata{iggy_user_id=4294967295 iggy_client_id=2830571172}: server::shard: SHARD - unauthenticated access attempt, session: client ID: 2830571172, IP address: 127.0.0.1:36938
20:47:11.788133 ERROR shard-0 server::tcp::connection_handler: Command with code 12 (cluster.metadata) was not handled successfully, session: client ID: 2830571172, IP address: 127.0.0.1:36938, error: Unauthenticated.
```

The healthy start that followed (one session, login on it, worker came up):

```
20:47:17.875107 INFO shard-0 server::tcp::tcp_listener: Accepted new TCP connection: 127.0.0.1:42918
20:47:17.875147 INFO shard-0 server::tcp::tcp_listener: Added tcp client with session: client ID: 2399690369, ...
20:47:17.876060 INFO shard-0 trace_login_user{... iggy_client_id=2399690369}: Logging in user: iggy ...
20:47:17.965363 INFO shard-0 trace_login_user{... iggy_client_id=2399690369}: Logged in user: iggy with ID: 0.
```

Our worker's traceback (`journalctl -u llmbus-worker`, same moment):

```
Jul 16 20:47:11 izabela213 python[19875]: Traceback (most recent call last):
Jul 16 20:47:11 izabela213 python[19875]:   File "/home/bartek/Projects/llmbus/src/llmbus/worker.py", line 249, in run_worker
Jul 16 20:47:11 izabela213 python[19875]:     await ensure_topology(client, topology)
Jul 16 20:47:11 izabela213 python[19875]:   File "/home/bartek/Projects/llmbus/src/llmbus/worker.py", line 166, in ensure_topology
Jul 16 20:47:11 izabela213 python[19875]:     if await client.get_stream(topology.stream) is None:
Jul 16 20:47:11 izabela213 python[19875]: RuntimeError: Unauthenticated
Jul 16 20:47:19 izabela213 python[19887]: INFO:llmbus.worker:worker consuming llmbus/llm-jobs as group llm-workers
```

Frequency before the fix: **3 handshake-class crashes across 8 worker starts** (1
`Disconnected`, 2 `Unauthenticated`) — roughly 37%. Every one self-healed on the systemd
restart, so the production impact was cosmetic (a traceback per restart) rather than an
outage.

---

## 3. What is NOT proven — do not claim it

- **Why production sometimes opened two TCP sessions**, and why the 20:47:11 session
  issued `cluster.metadata` with no login on it at all. The proxy repro (2.2) proves the
  *behaviour* without needing this, but the exact interleaving in our prod logs was never
  demonstrated. **Do not assert a mechanism for it.**
- **That the `cluster.metadata` call inside `login_user` caused the outage.** This was an
  intermediate (wrong) theory. It is a real code path (2.7) and it hid the failure, but
  causation was never shown.
- **What caused the very first disconnect** on our box. Unknown. The broker never
  restarted (`NRestarts=0`).
- Whether the reconnect behaviour is a *bug* or *working as intended* — that is the
  maintainers' call. A fair answer from them is "manual login is unsupported, use the
  connection string."

## 4. Wrong turns taken during this investigation (recorded so they are not repeated)

1. **"It's an SDK bug in the Rust core"** — asserted on the strength of the
   `cluster.metadata` call + the leader-check swallow. Wrong: the swallow *hides* the
   problem, it does not cause it.
2. **"Their own tests use `iggy+tcp://user:pass@host:port`, so we held it wrong"** —
   false. Their primary fixture (`conftest.py:55`) uses the manual form. The claim came
   from a grep hit whose context (a constructor test, TLS tests, a negative test) was not
   checked. This false claim briefly reached CLAUDE.md, ARCHITECTURE.md §14 #16 and a
   commit message before being retracted.
3. **"A silent reconnect happens on send"** — nearly discarded after seeing `send_raw`
   return `NotConnected` without reconnecting. The reconnect lives one level up, in
   `send_raw_with_response`. Both are true; the wrapper is the one that matters.

## 5. What is worth telling upstream

Two items, neither blocking us:

1. **The footgun:** manual `login_user` + the SDK's automatic reconnect silently loses
   authentication, unrecoverably — and that is the pattern their own `conftest.py`
   teaches, with no counter-example in the Python README. Either the reconnect should
   re-authenticate, or the manual form should fail loudly.
2. **The diagnosability:** `leader_aware.rs` swallows a failed `get_cluster_metadata`, so
   `login_user()` returns `Ok` on a session that just answered `Unauthenticated`.

Contribution route per CLAUDE.md: a **separate clone** of `github.com/apache/iggy`, dir
`foreign/python/` — not this repo.

---

## 6. Repro scripts (verbatim, as run)

Both were run on the VPS against the live broker and then deleted from `/tmp` there. They
read `.env` for the address and credentials. They are **read-only**: `get_stream` only,
no stream is created or deleted. `deploy/smoke.py` is the sanctioned live-prod exception
(§9b); these follow the same spirit.

### 6.1 `proof.py` — does `connect()` authenticate?

```python
"""Read-only proof: does connect() authenticate? Creates/deletes nothing.

A: IggyClient(addr)            -> connect() -> get_stream()  [NO login_user call]
B: from_connection_string(...) -> connect() -> get_stream()  [NO login_user call]

If B succeeds and A fails Unauthenticated, then connect() authenticates ONLY in the
connection-string form -> our manual-login client is unauthenticated after every
internal reconnect (send_raw_with_response -> disconnect -> connect -> retry).
"""
import asyncio, os
from urllib.parse import quote
from dotenv import load_dotenv
from apache_iggy import IggyClient

load_dotenv("/home/bartek/Projects/llmbus/.env")
ADDR = os.environ["IGGY_ADDRESS"]
USER = os.environ["IGGY_USERNAME"]
PW   = os.environ["IGGY_PASSWORD"]

async def main():
    # --- A: what we do today (manual login), but WITHOUT calling login_user ---
    a = IggyClient(ADDR)
    await a.connect()
    try:
        await a.get_stream("llmbus")
        print("A  IggyClient(addr)          connect() -> get_stream: OK   <- connect() DID authenticate")
    except Exception as e:
        print(f"A  IggyClient(addr)          connect() -> get_stream: {type(e).__name__}: {e}   <- connect() did NOT authenticate")

    # --- B: connection string (auto_login), also WITHOUT calling login_user ---
    conn = f"iggy+tcp://{quote(USER, safe='')}:{quote(PW, safe='')}@{ADDR}"
    b = IggyClient.from_connection_string(conn)
    await b.connect()
    try:
        s = await b.get_stream("llmbus")
        print(f"B  from_connection_string    connect() -> get_stream: OK (found={s is not None})   <- connect() DID authenticate")
    except Exception as e:
        print(f"B  from_connection_string    connect() -> get_stream: {type(e).__name__}: {e}")

asyncio.run(main())
```

Run as: `RUST_LOG=iggy=info .venv/bin/python -u /tmp/proof.py`

Output:

```
A  IggyClient(addr)          connect() -> get_stream: RuntimeError: Unauthenticated   <- connect() did NOT authenticate
B  from_connection_string    connect() -> get_stream: OK (found=True)   <- connect() DID authenticate
```

### 6.2 `reconnect_proof.py` — does the reconnect re-authenticate? (the decisive one)

```python
"""Does the SDK's internal reconnect re-authenticate? Read-only (get_streams only).

A local TCP proxy sits between the client and the broker so we can sever the
connection deterministically -- no broker restart, nothing created or deleted.

  client -> 127.0.0.1:9099 (proxy) -> 127.0.0.1:8092 (broker)

For each client style: connect, (login if manual), get_streams -> cut the TCP
connection -> get_streams again. The second call is what send_raw_with_response
retries after its disconnect()->connect() reconnect.
"""
import asyncio, os
from urllib.parse import quote
from dotenv import load_dotenv
from apache_iggy import IggyClient

load_dotenv("/home/bartek/Projects/llmbus/.env")
UP_HOST, UP_PORT = os.environ["IGGY_ADDRESS"].split(":")
USER = os.environ["IGGY_USERNAME"]
PW   = os.environ["IGGY_PASSWORD"]
PROXY = ("127.0.0.1", 9099)

live = []          # active proxied socket pairs

async def _pipe(r, w):
    try:
        while (data := await r.read(65536)):
            w.write(data); await w.drain()
    except Exception:
        pass
    finally:
        try: w.close()
        except Exception: pass

async def _handle(cr, cw):
    try:
        ur, uw = await asyncio.open_connection(UP_HOST, int(UP_PORT))
    except Exception:
        cw.close(); return
    live.append((cw, uw))
    await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw), return_exceptions=True)

def cut():
    """Sever every proxied connection, as a network blip would."""
    n = 0
    for cw, uw in live:
        for w in (cw, uw):
            try: w.close(); n += 1
            except Exception: pass
    live.clear()
    return n

async def probe(label, client, needs_login):
    await client.connect()
    if needs_login:
        await client.login_user(USER, PW)
    try:
        await client.get_stream("llmbus")
        print(f"{label}: before cut -> OK", flush=True)
    except Exception as e:
        print(f"{label}: before cut -> {type(e).__name__}: {e}", flush=True); return
    cut()
    await asyncio.sleep(0.5)
    try:
        await asyncio.wait_for(client.get_stream("llmbus"), timeout=30)
        print(f"{label}: AFTER RECONNECT -> OK  (re-authenticated)", flush=True)
    except Exception as e:
        print(f"{label}: AFTER RECONNECT -> {type(e).__name__}: {e}", flush=True)

async def main():
    server = await asyncio.start_server(_handle, *PROXY)
    async with server:
        addr = f"{PROXY[0]}:{PROXY[1]}"
        await probe("A manual  IggyClient(addr)+login_user", IggyClient(addr), True)
        conn = f"iggy+tcp://{quote(USER, safe='')}:{quote(PW, safe='')}@{addr}"
        await probe("B connstr from_connection_string     ",
                    IggyClient.from_connection_string(conn), False)
        cut()
        server.close()
    os._exit(0)  # background SDK heartbeat tasks would otherwise keep us alive

asyncio.run(main())
```

Run as: `timeout 150 .venv/bin/python -u /tmp/reconnect_proof.py`
(`-u` matters: without it the output is buffered and lost if the script is killed.)

Output:

```
A manual  IggyClient(addr)+login_user: before cut -> OK
A manual  IggyClient(addr)+login_user: AFTER RECONNECT -> RuntimeError: Unauthenticated
B connstr from_connection_string     : before cut -> OK
B connstr from_connection_string     : AFTER RECONNECT -> OK  (re-authenticated)
```

Gotchas if re-running: `get_streams()` is **not** exposed in the Python binding (use
`get_stream(name)`); the script must `os._exit(0)` because the SDK's background heartbeat
task keeps the loop alive.

---

## 7. Draft message to the maintainer — technical (Polish)

The Iggy maintainer is Polish; this version is for a direct, private message.

```
Cześć! Wpadliśmy na 0.8.0 (wiązanie pythonowe) na zachowanie, które chyba warto,
żebyś zobaczył — nie blokuje nas, obeszliśmy je, ale wygląda na pułapkę.

Klient zbudowany jako IggyClient(adres) + jawne await login_user(...) TRACI
uwierzytelnienie bezpowrotnie po tym, jak sam sterownik wznowi połączenie.

Dlaczego (z waszego źródła, tcp_client.rs):
- send_raw_with_response ponawia komendę m.in. po błędzie Unauthenticated —
  robi disconnect() -> connect() -> wysyła jeszcze raz,
- ale connect() loguje użytkownika tylko wtedy, gdy auto_login jest włączone;
  przy wyłączonym wypisuje "Automatic sign-in is disabled" i pomija logowanie,
- więc ponowiona komenda leci już na sesji nieuwierzytelnionej. A ponieważ
  uprawnienia w Iggy są przypisane do sesji, kolejne wywołania też dostają
  Unauthenticated — i to się nie naprawi samo: Unauthenticated wywołuje kolejne
  wznowienie bez poświadczeń, które kończy się identycznie. W kółko.

Odtworzenie (u nas powtarzalne): wpuść klienta przez zwykłego pośrednika TCP,
zrób connect + login_user, wywołaj get_stream (działa), potem zerwij gniazdo
i wywołaj get_stream jeszcze raz:

  IggyClient(adres) + login_user   -> przed zerwaniem: OK, po wznowieniu: Unauthenticated
  from_connection_string(...)      -> przed zerwaniem: OK, po wznowieniu: OK

Ten sam broker, te same poświadczenia, to samo zerwanie. Wariant z poświadczeniami
w adresie (iggy+tcp://user:haslo@host:port) wstaje bez problemu, bo ustawia
auto_login, więc sterownik loguje się w środku connect() — także przy każdym
własnym wznowieniu.

Dwie rzeczy, które może warto rozważyć:

1) foreign/python/tests/conftest.py (linia 55) — wasz jedyny punkt odniesienia,
   jak zbudować klienta w Pythonie (README nie pokazuje żadnego) — używa właśnie
   wariantu ręcznego, czyli tego niebezpiecznego. Wasze testy tego nie wyłapią,
   bo są krótkie, na świeżym serwerze i nigdy nie wznawiają połączenia. My
   skopiowaliśmy ten wzorzec i zapłaciliśmy za to wieczorem debugowania na
   produkcji.

2) leader_aware.rs połyka błąd get_cluster_metadata (warn! + Ok(None)) —
   wołane z login_user (binary_users.rs). Efekt: login_user() zwraca Ok nawet
   wtedy, gdy wywołanie w jego własnym przepływie dostało Unauthenticated.
   Domyślam się, że to celowe (wykrywanie lidera "na ile się da") i samo w sobie
   niczego nie psuje — ale to właśnie ono sprawiło, że u nas wyglądało to tak, że
   logowanie się udaje, a dopiero następna komenda pada. Stąd wieczór zamiast
   dziesięciu minut.

Czyli albo wznowienie powinno się ponownie logować, albo wariant ręczny powinien
głośno protestować — ale to już wasza decyzja projektowa. Jeśli się przyda, mogę
założyć zgłoszenie razem ze skryptem do odtworzenia. Nas to nie blokuje,
przeszliśmy na poświadczenia w adresie.
```

## 8. Draft message to the maintainer — plain English, non-technical

```
Hey! Ran into something on 0.8.0 (the Python one) that I figured you'd want to
know about. It's not blocking us, we've worked around it — but it cost me an
evening and I doubt we'll be the last ones to hit it.

Short version: if you create the client with just an address and then log in
yourself, that login doesn't survive the client reconnecting. And it reconnects
on its own, quietly, without telling you. After that every call comes back
"Unauthenticated" — and it never recovers, because each failure triggers another
reconnect that still has no credentials to log back in with. It just loops.

If you put the credentials in the connection string instead, it's all fine — the
client logs itself back in every time it reconnects.

We checked the two side by side: same server, same credentials, and we cut the
connection underneath both of them. The manual one dies for good. The connection
string one is back up instantly like nothing happened.

The reason I'm mentioning it rather than just moving on: your Python test setup
builds the client the manual way, and the readme doesn't show any other example —
so that's the version people copy, and it's the fragile one. Your own tests would
never catch it, because they're short and nothing ever reconnects during them.

The other thing that made it genuinely hard to spot: the login itself succeeds.
It's the *next* call that blows up. So it looks like auth is fine and something
else is broken, which sends you off in completely the wrong direction. (There's a
spot inside the login that quietly ignores a failure — I'm guessing on purpose,
but that's what hid it from us.)

Could well be working as intended from where you sit — your call entirely. If
it's useful I can write it up properly with a script that reproduces it in a few
seconds.
```

## 9. If he pushes back

The fair answer to *"manual login isn't supported, use the connection string"* is:
**then `conftest.py` probably shouldn't model it.** That is an argument about their
documentation, not their design — easy to accept, and it costs nobody face. Do not argue
the design point; we have no standing there and our fix does not depend on the outcome.
