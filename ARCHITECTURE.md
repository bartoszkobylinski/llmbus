# llmbus — architektura (v0, draft do dyskusji)

**Data:** 2026-07-03 · **Status:** projekt, przed implementacją.
**Decyzje zamknięte:** pilot = `hate-moderator`; providerzy = OpenAI + Anthropic; backbone = Apache Iggy (Python SDK).

> To jest żywy dokument. Sekcja 15 to lista otwartych decyzji — dopisujemy odpowiedzi w miarę pytań.

---

## 1. Po co to jest
Centralny punkt dla całego ruchu LLM z moich projektów. Zamiast każdy projekt woła OpenAI/Anthropic sam (z własnym retry / rate-limit / liczeniem kosztów), wszystkie wrzucają **zlecenie** na wspólny bus; pula workerów woła model centralnie; wynik wraca do projektu.

**Co rozwiązuje:**
- jeden rate-limit i budżet zamiast N kopii,
- bursty ruch (flood komentarzy, batch newsów) nie dusi web-latency — bufor,
- żaden job nie ginie przy restarcie (trwały log zamiast `BackgroundTasks` w RAM),
- zmiana modelu / providera w jednym miejscu,
- audyt + replay wszystkich promptów.

**Non-goals v1:** skalowanie na wiele workerów, priorytety/fast-lane, streaming odpowiedzi, multi-tenant, milamber (interaktywny → bezpośrednie wywołania).

## 2. Komponenty
1. **`llmbus` (biblioteka-klient / producent)** — importowana w projektach: `submit()`, `await_result()`.
2. **Iggy server** — broker (docker lokalnie na dev).
3. **Topic `llm-jobs`** — trwała kolejka zleceń.
4. **Worker** — proces w consumer group `llm-workers`: rate-limit, retry, provider, koszt.
5. **Results store (SQLite)** — `job_id → wynik`.
6. **Config / `.env`** — klucze, limity, budżety.

## 3. Przepływ end-to-end (model: callback)
Na przykładzie hate-moderatora:

```
IG webhook ─▶ hate-mod: llmbus.submit(...) ─▶ [topic llm-jobs] ─▶ worker ─▶ OpenAI/Anthropic
   │              │  zwraca job_id, 200 OK          (trwałe)         │ rate-limit/retry/koszt
   │              └─ web NIE czeka na model                          ▼
   ▼                                                          results store (SQLite)
/internal/classified  ◀────────── callback POST {job_id, meta, completion} ─────────┘
   │ decyzja hide/nie → hide_comment → zapis
```

Kroki:
1. Webhook IG dostaje komentarz.
2. hate-mod buduje `Job(project="hate-moderator", kind="classify", model=…, messages=[…], callback_url="…/internal/classified", meta={comment_id})` i woła `bus.submit(job)` → dostaje `job_id`, **zwraca 200 OK natychmiast**. Web nie dotyka OpenAI. (`submit` bierze `Job`, bo kontrakt §4 *jest* API — nie kwargs; PR `client`.)
3. Zlecenie ląduje na `llm-jobs` (trwałe, przeżywa restart).
4. Worker bierze je, woła model z **centralnym** rate-limit/retry, liczy koszt.
5. Worker zapisuje wynik do store i **POST-uje callback** do hate-mod.
6. hate-mod w `/internal/classified` robi resztę: decyzja → `hide_comment` → zapis.

**Wariant bez callbacku (poll):** caller robi `await_result(job_id)` (pętla poll po store). Do skryptów batchowych (news), gdzie caller może poczekać.

## 4. Kontrakt wiadomości
**Job (na `llm-jobs`):**
```json
{
  "job_id": "uuid",
  "project": "hate-moderator",
  "kind": "classify|summarize|…",
  "model": "gpt-5-mini | claude-…",
  "messages": [{"role": "user", "content": "…"}],
  "params": {"temperature": null, "max_tokens": 512},  // temperature opcjonalne; structured output poza v1 (§14 #10)
  "callback_url": "http://…/internal/classified",   // albo null → poll
  "meta": {"comment_id": "…"},                        // wraca nietknięte
  "submitted_at": "…"
}
```
**Result (store + callback):**
```json
{ "job_id": "…", "status": "ok|error", "completion": "…",
  "usage": {"in": 0, "out": 0, "cost_usd": 0.0}, "provider": "openai",
  "error": null, "meta": {"comment_id": "…"} }
```
**Walidacja (v1, ścisła — Pydantic):**
- **`extra="forbid"`** na wszystkich modelach kontraktu — nieznane pole (np. literówka `callback` zamiast `callback_url`) = błąd od razu, nie ciche zgubienie. `meta` zostaje dowolnym słownikiem, więc elastyczność nie ucierpia.
- **`job_id` musi być poprawnym UUID** (generowany jako `uuid4`), **normalizowany do postaci kanonicznej** (lowercase, z myślnikami) — to klucz w store i podstawa idempotencji/dedupu (§6). Warianty tego samego UUID (uppercase, `urn:uuid:…`, `{…}`) sprowadzamy do jednego klucza; pusty/„prosty"/z białymi znakami id jest odrzucany. Akceptujemy wyłącznie wejście typu `str` — `bytes`/inne typy odrzucane (`StrictStr`), żeby leniwa koercja nie przemyciła nie-stringa.
- **`max_tokens` > 0** jeśli podane (nieprawidłowe u każdego providera). **`temperature` jest opcjonalne** (`null`/nieustawione = model używa swojej domyślnej) i nieograniczone w kontrakcie — obsługa i zakresy różnią się per model, więc waliduje je adapter providera (§7). Rodzina GPT-5 **odrzuca jakiekolwiek ustawione `temperature`** (§14 #9): adapter OpenAI zgłasza wtedy błąd **przed** wywołaniem API, zamiast cicho je gubić.
- **`response_format` (structured output) NIE jest w kontrakcie v1** (§14 #10): goły `str` nie mapuje się czysto na żadnego providera — OpenAI chce obiektu, Anthropic używa `output_config.format` — więc pole, które znaczyłoby co innego per adapter, jest **odłożone do v2** zamiast udawać neutralność.

**Uwaga o nagłówkach Iggy:** metadane (`project`, `model`, `priority`) logicznie należą do **nagłówków wiadomości**, ale Python SDK ich nie ma → w v1 wszystko idzie w body JSON. To jest dokładnie miejsce na ewentualną rozbudowę SDK (nagłówki).

## 5. Topiki i partycjonowanie
- **v1:** jeden topic `llm-jobs`, jedna partycja, jeden consumer group, jeden worker. Wolumen niski/bursty → wystarcza.
- **v2:** partycjonowanie po `priority` (fast-lane dla interaktywnych) lub `project`; więcej workerów w consumer group → Iggy rozdaje partycje.
- **Results NIE idą przez Iggy** (są w store) — prostszy request/reply. Można dodać topic `llm-results` dla czystego event-flow, ale v1 tego nie potrzebuje.

## 6. Worker — co robi centralnie

**Podział na dwa PR-y (§14 #11):** logika czysta (`retry.py`, `processing.py` —
retry/backoff/klasyfikacja błędu, routing, koszt, estymacja tokenów, budowa
`Result`, wysyłka callbacku) idzie w PR `worker-core` z pełną bramką (unit +
`mutmut` 0 survivors, bez serwera — całe I/O wstrzykiwane); cienka powłoka Iggy
(pętla consumer-group, poll/commit offsetu, prawdziwy klient httpx) idzie osobno
w PR `worker-loop` z testami integracyjnymi. `process_job(deps, job)` to rdzeń;
`worker.py` (loop) tylko go karmi wiadomościami.

- **rate-limit:** token-bucket per provider (OpenAI i Anthropic osobno; req/min + tok/min). Globalny — to jest cała idea busa. Rezerwacja jest **przed** wywołaniem (inaczej 429), więc worker estymuje tokeny z góry (§14 #13): `sum(len(content))//4` po wiadomościach (input, heurystyka ~4 znaki/token) + `max_tokens` joba, a gdy nie ustawiono — `WORKER_DEFAULT_OUTPUT_TOKENS`. Kubełek sam się koryguje w kolejnym cyklu, więc dokładność nie jest krytyczna. Rezerwacja leci **przed każdą próbą** (retry to kolejny realny request do providera).
- **retry/backoff (§14 #11):** na transient failures (429/408/409, każde 5xx, timeout, zerwane połączenie) z **exponential backoff + full jitter**: opóźnienie retry `i` (0-based) = `min(WORKER_BACKOFF_MAX_S, WORKER_BACKOFF_BASE_S * 2**i) * random()`. Łącznie `WORKER_MAX_ATTEMPTS` prób (wliczając pierwszą; `4` = 1 + 3 retry). Po wyczerpaniu → `Result{status:"error"}` do store + log (v1 „dead-letter"; osobny topic dead-letter w v2, §13). Klasyfikacja transient/terminal (`retry.is_retryable`, §14 #12) **duck-typuje** wyjątek — status HTTP + `TimeoutError`/`ConnectionError` + nazwy klas `APIConnectionError`/`APITimeoutError` — **bez importu SDK**, więc decyzja retry siedzi w bramce mutacyjnej (adaptery też nie importują SDK, §7; kontrakt providera bez zmian).
- **provider routing:** po nazwie `model` → OpenAI albo Anthropic (`provider_for`); nieznany model / brak adaptera w rejestrze → `Result{status:"error"}`, żeby jeden zły job nie zatrzymał pętli.
- **timeout (§14 #11):** **per próba** (`WORKER_JOB_TIMEOUT_S`), przez wstrzyknięty runner (domyślnie `asyncio.wait_for`) — timeout wypada jako `TimeoutError` (transient → retry). Wstrzyknięcie runnera zamiast zegara ściennego trzyma `process_job` w bramce mutacyjnej bez realnego czasu, jak wstrzykiwany `sleep`/`clock` w `ratelimit`.
- **koszt:** z usage, per `project` → tabela kosztów (podstawa budżetu). Cennik jest **datowany** (`cost.py`: każdy model ma historię cen z datą wejścia w życie) — koszt liczony po stawce obowiązującej w dniu `submitted_at`, więc zaplanowane zmiany (np. koniec ceny promo Sonnet 5 dnia 2026-09-01) rozwiązują się same, bez ręcznej edycji i bez pobierania cen z sieci. `Decimal` z `cost.py` schodzi do `float` dopiero na granicy `Result.usage.cost_usd`. (Zapytanie agregujące per projekt/dzień dołoży się w PR `worker-loop`.)
- **idempotencja:** przy at-least-once (worker padł po modelu, przed commitem offsetu) job wraca; `store.finalize` jest one-shot (`WHERE status='pending'`), więc redostawa dostaje `False` → brak podwójnego zapisu i podwójnego callbacku. Redostawa **ponawia** wywołanie modelu (koszt) — świadomy, rzadki koszt (tylko recovery po crashu), a `finalize` jest gwarancją poprawności. hate-mod ma dodatkowo własny dedup po `comment_id`.
- **callback (§14 #14):** worker POST-uje `Result` (JSON, `by_alias`) na `callback_url` klientem **httpx** (extra `worker`). Dostawa jest **best-effort**: błąd POST-a → log + swallow, **bez retry** callbacku w v1 — wynik jest już trwale w store, więc poll (§11) to niezawodna ścieżka; retry/dead-letter callbacku to v2 (§13). Callback leci tylko gdy *ta* dostawa wygrała `finalize` (brak duplikatów).
- **pętla consumer-group (`worker.py`, PR `worker-loop`):** cienka powłoka Iggy. `consumer_group` (join `llm-workers`, `create_if_not_exists`) + `consume_messages(callback, shutdown_event)` z `AutoCommit.After(ConsumingEachMessage)` → **commit offsetu PO przetworzeniu** (at-least-once; redostawa bezpieczna przez one-shot `finalize`). Topologia (`Topology`: stream `llmbus`/topic `llm-jobs`/1 partycja/grupa `llm-workers`) domyślna, ale wstrzykiwalna (izolacja testów integracyjnych po uuid). Payload → `Job` przez `decode_job` (pydantic, `extra="forbid"`). **Poison message (§14 #15):** ciało nieparsujące się na `Job` → log (z obciętym raw) + skip + commit dalej; halt/retry zawiesiłyby jedynego workera na jednej złej wiadomości, a bez `job_id` i tak nie ma czego finalizować (dead-letter → v2, §13). Wejście: `run_worker` (wiring config→deps + pętla, powłoka I/O, poza bramką mutacyjną, testy integracyjne na żywym Iggy) + `python -m llmbus.worker` z SIGINT/SIGTERM → `shutdown`. Czyste szwy (`decode_job`, `ensure_topology`, `make_callback_sender`, `_consume_one`, `_load`) — unit-testy z atrapami.

## 7. Abstrakcja providera
Interfejs `call(model, messages, params) -> {completion, usage}`; implementacje `openai.py`, `anthropic.py`. Mapowanie `model → provider`, normalizacja usage/kosztu do wspólnego formatu. Miejsce na trzeciego (OpenRouter) później.

**Adapter OpenAI (`openai.py`, rodzina GPT-5).** Klient SDK jest **wstrzykiwany** (`config.py` buduje prawdziwy `AsyncOpenAI`; testy podają atrapę) — moduł nie importuje SDK i pozostaje czystą logiką (w bramce mutacyjnej). Mapowanie per-model wg realiów SDK (zweryfikowane, §14 #9): `max_tokens → max_completion_tokens`; `temperature` **odrzucane** gdy job je ustawi (GPT-5 honoruje tylko swoją domyślną — fail-loud przed wywołaniem API); `completion_tokens` (zawiera reasoning tokens) → `output_tokens`.

**Adapter Anthropic (`anthropic.py`, rodzina Claude).** Analogicznie: klient wstrzykiwany, czysta logika, brak importu SDK. Realia Messages API (zweryfikowane): `max_tokens` jest **wymagane** (job bez niego → `ValueError` przed wywołaniem); system prompt to osobny top-level `system=`, więc wiadomości `role=system` są **wydzielane** z `messages`; `temperature` jest **per-model** — `claude-opus-4-8` i `claude-sonnet-5` je odrzucają (400), a `claude-haiku-4-5` przyjmuje 0.0–1.0 (z walidacją zakresu). Completion to pierwszy blok `text` z `response.content`; `usage.input_tokens`/`output_tokens` mapują się wprost na `Usage`. (Wsparcie temperatury w Haiku jest wg dokumentacji Anthropic — do potwierdzenia testami integracyjnymi w PR `config`.)

**Kontrakt „provider nie wycenia":** `ProviderResult.usage.cost_usd` musi zostać `0.0` — provider raportuje wyłącznie tokeny, a `cost.py` jest jedynym źródłem ceny (tabela datowana, §6, bez cen z sieci). `ProviderResult` **odrzuca** (`ValueError`) usage z niezerowym `cost_usd` — fail-loud, jak reszta kontraktu (§4). Dzięki temu cena zgłoszona przez API providera (np. przyszły OpenRouter, który zwraca koszt) nie przecieka i nie przesłania ceny liczonej lokalnie po stawce z dnia `submitted_at`. `Usage` jest przy tym **immutable** (`frozen=True`), więc `cost_usd` nie da się podmienić po konstrukcji — gwarancja jest strukturalna, nie tylko w chwili tworzenia (wycena downstream buduje **nowy** `Usage`, nie mutuje istniejącego).

## 8. Integracja hate-moderator (pilot) — co się zmienia
- **Zostaje w hate-mod:** webhook (HMAC), dedup po `comment_id`, decyzja policy, `hide_comment`, zapis do DB, (semafor/cap — do decyzji czy przenieść do busa).
- **Wychodzi do busa:** samo wywołanie OpenAI (moderation + classify).
- **Zmiana w kodzie:** w `app/workers/poller.py::process_comment` zamiast inline `classifier.classify(...)` → `llmbus.submit(..., callback="/internal/classified")`. Nowy endpoint robi decyzję + hide + zapis.
- **Efekt:** flood nie dotyka web-latency, OpenAI poza procesem web, praca przeżywa restart — dokładnie „out-of-process classification queue" z ich ROADMAPu, tylko na Iggy zamiast Postgres-queue.

## 9. Struktura repo (propozycja)
```
llmbus/
  pyproject.toml            # uv, venv in-project
  .env                      # OPENAI/ANTHROPIC keys, IGGY_ADDRESS (gitignored)
  docker-compose.yml        # serwer Iggy do dev
  ARCHITECTURE.md
  README.md
  src/llmbus/
    __init__.py
    client.py               # submit(), await_result()  ← import w projektach
    schema.py               # Pydantic: Job, Result
    worker.py               # consumer group + pętla
    providers/{base,openai,anthropic}.py
    ratelimit.py
    cost.py
    store.py                # SQLite results
    config.py
```
Katalog: `~/Programming/Python/llmbus/`. Klient `llmbus` używany w innych repo (editable install / lokalny path — do decyzji).

## 9b. Deployment — systemd-first (prod) + lokalny dev

Cały stack projektów chodzi pod **systemd + nginx**, więc Iggy wpinamy tak samo — **bez Dockera na prod**.

### Prod (VPS `izabela213`) — plan wdrożenia (stan 2026-07-14: **jeszcze NIE postawione**)
Artefakty i runbook: **`deploy/`** (`iggy-server.service`, `llmbus-worker.service`,
`iggy.env.example`, `deploy.sh`, `README.md`). Na maszynie nie ma jeszcze ani unitów, ani
niczego na `:8092`. Ustalenia potwierdzone na maszynie (2026-07-14):
- **Iggy = binarka pod systemd, bez Dockera** (zgodnie z pierwotnym §9b). **0.8.0 nie ma
  prebuilt binarki** (release `server-0.8.0` bez assetów; Apache tylko źródła; brak crate'a
  `iggy-server`) → build ze źródeł z tagu `server-0.8.0`, ale **NIE na VPS-ie**: box ma
  1 vCPU, ~0.8 GB wolnego RAM i **zero swapu** (LXC) → link release'owy pada na OOM.
  Buduje **GitHub Actions** (`.github/workflows/build-iggy-server.yml`) w kontenerze
  `ubuntu:22.04` — glibc **2.35**, dokładnie to, co box; `ubuntu-latest` (24.04, glibc 2.39)
  dałby binarkę, której box nie załaduje. Kolejność jak w oficjalnym Dockerfile:
  `npm --prefix web run build:static` (serwer osadza web UI) → `cargo build --bin
  iggy-server --release`. Artefakt → `scp` → `/usr/local/bin/iggy-server`. Konfig przez env
  (`IGGY_*` nadpisuje wbudowane defaulty; bez `config.toml`).
- **Kernel: NIE jest blockerem — i nie da się go zmienić od środka.** `izabela213` to
  **kontener LXC** (`systemd-detect-virt` → `lxc`), więc jedzie na kernelu hosta Proxmox:
  **7.0.12-1-pve**. `apt install linux-generic-hwe-22.04` w kontenerze to **no-op** — kernel
  przychodzi z hosta (i pakiet nie jest tam nawet zainstalowany). Wymóg io_uring (≥ 5.19)
  jest spełniony i **zweryfikowany bezpośrednio**: `kernel.io_uring_disabled=0`, a
  `io_uring_setup(2)` wywołany wewnątrz kontenera zwraca deskryptor (nie blokuje go ani
  AppArmor, ani seccomp LXC). Wcześniejsza notka „Ubuntu 22.04 = 5.15 → zrób HWE" była
  **błędna dla LXC** — kernel podniósł się sam, gdy dostawca zmigrował CT na nowy host.
- **Runtime libs:** binarka linkuje `libhwloc` — box **nie ma `libhwloc15`**
  (`sudo apt install -y libhwloc15`). `libssl3` i `libudev1` już są.
- Unit `iggy-server.service`: `User=bartek`, `AmbientCapabilities=CAP_SYS_NICE`,
  `LimitMEMLOCK=infinity`, **bez** `SystemCallFilter` (zablokowałby io_uring). Dane w
  `IGGY_SYSTEM_PATH=/var/lib/iggy`.
- **Port `127.0.0.1:8092`**, nie 8090 — 8090 zajęte przez `beziarnia` (gunicorn), 8091
  przez uvicorn. Tylko `localhost`, tylko TCP; nginx poza ścieżką (SDK = surowy TCP).
  `llmbus` `.env` → `IGGY_ADDRESS=127.0.0.1:8092`; root-creds brokera w `deploy/iggy.env`
  (gitignore), worker loguje się tymi samymi `IGGY_USERNAME`/`IGGY_PASSWORD`.
- **Worker = `llmbus-worker.service`** w konwencji tego VPS-a (`User=bartek`, kod w
  `~/Projects/llmbus`, `EnvironmentFile=.env`), `ExecStart` na `.venv/bin/python -m
  llmbus.worker` (nie `uv run` — nie zgubić extra `worker`), `After=iggy-server.service`,
  `Restart=always` łata wyścig „worker wstał przed brokerem" (redostawa bezpieczna, §6).

### Dev (laptop, macOS) — WŁASNY lokalny Iggy, NIE prod
- Do prac deweloperskich stawiasz **osobny, lokalny** serwer Iggy (na macu najszybciej **Docker**: `docker compose up -d`). **Nie łączysz się do Iggy na VPS.**
- **Dlaczego lokalnie, nie do VPS:**
  - **izolacja** — dev robi śmieciowe wiadomości, `delete topic`, restartuje workery, testuje replay. To NIE może dotknąć prod-loga, przez który lecą realne komentarze hate-moderatora.
  - **offline** — pracujesz bez VPS/sieci.
  - **bezpieczeństwo** — nie musisz wystawiać portu brokera na VPS (`8092`) na świat (localhost-only na prod zostaje bezpieczne).
- **Ten sam kod, inny serwer:** `llmbus` czyta `IGGY_ADDRESS` z `.env`. Dev `.env` → lokalny serwer; prod `.env` → serwer na VPS. Zero zmian w kodzie.
- **Fallback** (gdybyś nie chciał Dockera na macu): SSH-tunnel `ssh -L 8090:127.0.0.1:8092 izabela213` — **zdalna strona to 8092** (broker), lokalna 8090, więc dev-owy `IGGY_ADDRESS=127.0.0.1:8090` działa bez zmian. Na VPS-ie 8090 to `beziarnia`/gunicorn, **nie** Iggy — stary wariant `-L 8090:localhost:8090` celował w cudzy serwis. Port zostaje prywatny. UWAGA: to celuje w **prod-dane**, więc tylko do podglądu, nie do testów niszczących. Rekomendacja: osobny lokalny serwer.

**Zasada:** dwa osobne serwery, dwa osobne logi — standardowe rozdzielenie dev/prod.

## 10. Konfiguracja i sekrety
`.env`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `IGGY_ADDRESS`/`IGGY_USERNAME`/`IGGY_PASSWORD`, `STORE_PATH` (plik SQLite — pisany przez workera, czytany pollem przez producenta, §9b), limity (req/min, tok/min per provider), polityka workera (`WORKER_*`, §6/§14 #11) i budżety per projekt. Nic hardcoded (python-dotenv). `Config` (współdzielony producent+worker) niesie klucze/Iggy/`db_path`; `WorkerPolicy` (`parse_worker_policy`) parsuje `WORKER_*` osobno, bo producent ich nie potrzebuje.

## 11. Obserwowalność
- **Audyt:** topic `llm-jobs` = log wszystkich promptów (replay).
- **Koszt:** tabela w store per projekt/dzień.
- **Lag/stats:** „ile jobów czeka" — SDK nie ma `get_stats`; v1 przybliża jako
  `Store.pending_count()` (liczba wierszy `status='pending'`); v2 = rozbudowa SDK.
- **Store (impl., PR `store`):** SQLite przez `aiosqlite` (async, bez sync-wrapperów na
  pętli — każde wywołanie leci na wątku per-połączenie), tryb **WAL + `busy_timeout`**
  (worker-pisarz + poller-czytelnik współlokowani, §9b — czytelnik nie blokuje jedynego
  pisarza). Jeden wiersz na job keyowany `job_id`: `pending` → `ok`/`error`; `finalize`
  jest one-shot (`WHERE status='pending'`) → idempotencja przy redostawie (§6). Wiersz
  trzyma pola `Result` **plus** `project`/`model`/`submitted_at` (potrzebne do kosztu per
  projekt/dzień, których `Result` nie niesie).
- **Koszt (impl., PR `worker-loop`):** `Store.cost_by_project_day()` — `SUM(cost_usd)`
  grupowany po `project` i `substr(submitted_at,1,10)` (literalny dzień ISO, ta sama data,
  po której `cost.py` wycenia, §6), z `HAVING SUM(cost_usd) > 0`: grupy bez realnego wydatku
  (tylko `pending`/`error`, albo darmowe completiony) są **pominięte** — księga wydatków
  pokazuje realny koszt, nie wiersze `$0.00`. To „tabela w store per projekt/dzień" z góry tej sekcji.

## 12. Braki Iggy SDK, które tu uderzysz (nie blokują v1)
- **nagłówki wiadomości** — metadane w headerach zamiast body.
- **get_stats** — monitoring lagu.
Oba to potencjalna kontrybucja; obejścia (body JSON, licznik w store) wystarczą na v1.

## 13. Świadomie odłożone (v2+)
skalowanie workerów, priorytety/fast-lane, dead-letter topic, streaming odpowiedzi, twarde limity budżetu, milamber, OpenRouter, dashboard.

## 14. Otwarte decyzje (do rozstrzygnięcia)
1. ~~Worker **generyczny + callback** vs **domenowy**.~~ **ROZSTRZYGNIĘTE —
   generyczny + callback.** Bus zostaje reużywalny: worker liczy model/koszt/retry
   i POST-uje surowy `Result` na `callback_url`, a domenę (decyzja hide, `hide_comment`,
   zapis) robi hate-mod w `/internal/classified` (§3, §8). Worker nie zna semantyki
   `kind` — to trzyma §1 (bus generyczny, mały). Decyzja podjęta w PR `worker-core`.
2. Rate-limit: tylko globalny w busie, czy zostawić też lokalny cap w hate-mod?
3. Dystrybucja klienta `llmbus` do innych repo (editable / path / pip prywatny)?
4. ~~Results: `store + callback` wystarczy, czy chcesz też topic `llm-results`?~~
   **ROZSTRZYGNIĘTE — `store + callback`, bez `llm-results` w v1.** Wyniki NIE wracają
   przez Iggy (§5): worker zapisuje `Result` do store (SQLite), dostawa idzie callbackiem
   (§3) i/lub pollingiem (#7). Osobny topic `llm-results` to scope wobec §1 — odłożony do
   v2. Decyzja podjęta w PR `store`.
5. ~~Iggy server: docker lokalnie → potem VPS?~~ **ROZSTRZYGNIĘTE (sekcja 9b):** prod = binarka pod systemd na VPS (**`127.0.0.1:8092`** — nie 8090, które na tym boxie zajmuje `beziarnia`/gunicorn; nginx poza ścieżką); dev = osobny lokalny Iggy (Docker na macu, `8090`), nie łączymy się do prod. Jeden serwer na VPS dla wszystkich projektów.
6. Model klasyfikacji dla hate-mod: który z rodziny GPT-5 (gpt-5-mini/nano) lub Anthropic? (OpenAI = GPT-5, nie 4o.)
7. ~~Sync (poll `await_result`) vs async (callback) — czy oba wspieramy w v1, czy tylko callback?~~
   **ROZSTRZYGNIĘTE — oba w v1.** Callback to główna ścieżka (hate-mod, §3); poll
   (`await_result(job_id)`) dokłada tanią ścieżkę dla batch/skryptów, bo czyta ten sam plik
   store, który worker zapisuje (współlokacja, §9b). Mechanika: `submit()` wstawia wiersz
   `pending`, worker `finalize` → terminal; `await_result` odpytuje store aż status będzie
   terminalny. To samo `pending` daje przybliżenie lagu (§11). Decyzja podjęta w PR `store`.
   **Wdrożone w PR `client`:** `BusClient.submit(job)` (wstawia `pending` + wysyła na
   `llm-jobs`, partycja 0) / `await_result(job_id)` (poll store, `TimeoutError` po czasie)
   nad współdzielonym store, `from_env`/`from_config`/async-context-manager. Powierzchnia
   importu tylko-producent (apache-iggy + aiosqlite, bez SDK LLM/httpx — §10, §14 #3).
8. ~~Statyczne typowanie: wprowadzać type-checker do bramki merge?~~ **ROZSTRZYGNIĘTE:**
   `mypy --strict` nad `src/` to **obowiązkowa bramka merge** (0 błędów; testy wyłączone,
   analogicznie do ruff `tests/** = ANN`). To jedyne, co egzekwuje **semantyczną** stronę
   kontraktu `Provider` (§7) — że `call` jest `async` i zwraca `ProviderResult`, a `name`
   jest `str`; `@runtime_checkable`/`isinstance` sprawdza tylko *obecność* atrybutów, nie
   ich kształt. Dlatego adaptery muszą być podpięte przez otypowany szew (np. rejestr
   `dict[str, Provider]`), by mypy je weryfikował, i mieć własne testy `await`/assert.
   Pakiet dostaje znacznik PEP 561 `py.typed` — repo importujące `llmbus` też korzysta z
   typów (wsad do decyzji **#3** o dystrybucji klienta).
9. ~~**GPT-5 a `params` (§4) — polityka `temperature`.**~~ **ROZSTRZYGNIĘTE — wariant A
   (waliduj i odrzuć wcześnie).** Zweryfikowane w SDK (lipiec 2026): cała rodzina GPT-5
   (gpt-5/mini/nano) przez Chat Completions **odrzuca `temperature` inne niż domyślne (1)**
   — 400 „Unsupported value: 'temperature'… Only the default (1) value is supported" — i
   wymaga **`max_completion_tokens`, nie `max_tokens`**. Wdrożone: `JobParams.temperature`
   jest opcjonalne (`None` domyślnie, nie `0`, §4); adapter OpenAI **odrzuca (`ValueError`)
   jakiekolwiek ustawione `temperature`** dla GPT-5 **przed** wywołaniem API (fail-loud, jak
   reszta §4), zamiast cicho gubić wartość (B) lub czekać na 400 po zakolejkowaniu (C).
   `max_tokens → max_completion_tokens` to mapowanie w adapterze; reasoning tokens GPT-5
   wchodzą w `completion_tokens` → `output_tokens`, więc wyceniają się poprawnie (§6). §4/§7
   zaktualizowane w tym samym PR.
10. ~~**`response_format` (structured output) w kontrakcie §4.**~~ **ROZSTRZYGNIĘTE —
   usuń z v1, odłóż do v2.** Goły `str` (jak w pierwotnym §4) nie mapuje się czysto na
   żadnego providera: OpenAI oczekuje **obiektu** `response_format`, Anthropic używa
   **`output_config.format`**. Utrzymanie pola zmuszało adaptery do rozjazdu (OpenAI
   forwardował surowy string, Anthropic go odrzucał) — pole „neutralne", które znaczy co
   innego per provider. Wybrane **A (usuń z kontraktu)** zamiast **B (zostaw, tylko-OpenAI)**
   lub **C (przeprojektuj na typ strukturalny już teraz)** — C to scope creep wobec §1
   (v1 mały). Wdrożone: `JobParams.response_format` usunięte; gałęzie forward/reject w obu
   adapterach usunięte; §4 odnotowuje odłożenie. Ustrukturyzowany output wróci w v2 jako typ
   mapujący się na realny kształt każdego providera. Decyzja podjęta w PR `config`.
11. ~~**Polityka retry/backoff/timeout workera + struktura PR.**~~ **ROZSTRZYGNIĘTE
   (PR `worker-core`).** Wartości są konfigiem (`.env`, nic hardcoded, §10): domyślnie
   `WORKER_MAX_ATTEMPTS=4` (1 + 3 retry), `WORKER_BACKOFF_BASE_S=0.5`,
   `WORKER_BACKOFF_MAX_S=30`, `WORKER_JOB_TIMEOUT_S=60` (per **próba**),
   `WORKER_DEFAULT_OUTPUT_TOKENS=512`. Backoff = exponential + full jitter (§6).
   Parsowane osobno (`config.parse_worker_policy`), nie w `Config` — producent
   (`client.py`) tych kluczy nie potrzebuje. **Struktura:** split `worker-core`
   (czysta logika, bramka mutacyjna, bez serwera) + `worker-loop` (powłoka Iggy,
   testy integracyjne) — patrz §6.
12. ~~**Gdzie klasyfikować błąd retry (429/5xx/timeout) bez importu SDK.**~~
   **ROZSTRZYGNIĘTE — czysta `retry.is_retryable(exc)` w `worker-core`.**
   Duck-typing: `status_code` (408/409/429 lub ≥500) + `TimeoutError`/`ConnectionError`
   + nazwy klas `APIConnectionError`/`APITimeoutError`. Bez importu SDK, bez zmiany
   kontraktu §7/adapterów → decyzja retry **w bramce mutacyjnej**, testowana
   syntetycznymi wyjątkami. Odrzucone: (B) adaptery pakujące błąd SDK w typowany
   `ProviderCallError` (zmiana §7 + obu adapterów), (C) worker łapiący konkretne typy
   `openai`/`anthropic` (retry poza bramką).
13. ~~**Estymacja tokenów do rezerwacji rate-limitu (pre-call).**~~ **ROZSTRZYGNIĘTE
   — `len(prompt)//4` (input) + `max_tokens` lub `WORKER_DEFAULT_OUTPUT_TOKENS`
   (output).** Rezerwacja musi być przed wywołaniem (inaczej 429); kubełek koryguje się
   w kolejnym cyklu, więc heurystyka wystarcza. Odrzucone: flat-estimate (nie skaluje
   z długością promptu) i „tylko req/min" (marnuje istniejący bucket tokenów, ryzyko TPM).
14. ~~**Dostawa callbacku: klient HTTP + polityka błędu.**~~ **ROZSTRZYGNIĘTE —
   httpx (extra `worker`); best-effort, bez retry.** POST `Result` na `callback_url`;
   błąd → log + swallow (wynik już w store, poll to niezawodna ścieżka, §11); retry/
   dead-letter callbacku → v2 (§13). Odrzucone: bounded retry callbacku (wciąga
   dead-letter do v1) i stdlib `urllib` (blokujący, nieidiomatyczny w async). Sender
   wstrzykiwany do `process_job`; prawdziwy httpx w PR `worker-loop`.
15. ~~**Poison message: ciało na `llm-jobs`, które nie parsuje się na `Job`.**~~
   **ROZSTRZYGNIĘTE (PR `worker-loop`) — log + skip + commit dalej.** Nieparsowalna
   wiadomość (zły JSON lub złamany kontrakt §4, `extra="forbid"`) nie ma poprawnego
   `job_id`, więc nie da się jej sfinalizować w store ani nigdy nie przejdzie; jest
   logowana (z obciętym raw payloadem) i pomijana, a offset commituje dalej. Odrzucone:
   halt workera (jedna zła wiadomość kładzie cały bus) i retry/park (nieskończona
   redostawa tej samej trucizny — zawiesza jedynego workera). Trwały dead-letter
   (persist raw) → v2 (§13), bo v1 nie ma dead-letter topicu.
