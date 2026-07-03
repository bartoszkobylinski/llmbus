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
2. hate-mod woła `llmbus.submit(project="hate-moderator", kind="classify", model=…, messages=[…], callback="…/internal/classified", meta={comment_id})` → dostaje `job_id`, **zwraca 200 OK natychmiast**. Web nie dotyka OpenAI.
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
  "params": {"temperature": 0, "max_tokens": 512, "response_format": "…"},
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
- **`max_tokens` > 0** jeśli podane (nieprawidłowe u każdego providera). **`temperature` bez ograniczeń w kontrakcie** — zakresy różnią się per provider (OpenAI 0–2, Anthropic 0–1), więc waliduje/normalizuje je adapter providera (§7).

**Uwaga o nagłówkach Iggy:** metadane (`project`, `model`, `priority`) logicznie należą do **nagłówków wiadomości**, ale Python SDK ich nie ma → w v1 wszystko idzie w body JSON. To jest dokładnie miejsce na ewentualną rozbudowę SDK (nagłówki).

## 5. Topiki i partycjonowanie
- **v1:** jeden topic `llm-jobs`, jedna partycja, jeden consumer group, jeden worker. Wolumen niski/bursty → wystarcza.
- **v2:** partycjonowanie po `priority` (fast-lane dla interaktywnych) lub `project`; więcej workerów w consumer group → Iggy rozdaje partycje.
- **Results NIE idą przez Iggy** (są w store) — prostszy request/reply. Można dodać topic `llm-results` dla czystego event-flow, ale v1 tego nie potrzebuje.

## 6. Worker — co robi centralnie
- **rate-limit:** token-bucket per provider (OpenAI i Anthropic osobno; req/min + tok/min). Globalny — to jest cała idea busa.
- **retry/backoff:** na 429/5xx/timeout z jitter; po M próbach → dead-letter (v1: zapis błędu do store + log; osobny topic dead-letter w v2).
- **provider routing:** po nazwie `model` → OpenAI albo Anthropic.
- **koszt:** z usage, per `project` → tabela kosztów (podstawa budżetu). Cennik jest **datowany** (`cost.py`: każdy model ma historię cen z datą wejścia w życie) — koszt liczony po stawce obowiązującej w dniu `submitted_at`, więc zaplanowane zmiany (np. koniec ceny promo Sonnet 5 dnia 2026-09-01) rozwiązują się same, bez ręcznej edycji i bez pobierania cen z sieci.
- **timeout** per job.
- **idempotencja:** przy at-least-once (worker padł po modelu, przed commitem offsetu) job wraca; `job_id` w store chroni przed podwójnym zapisem/callbackiem. hate-mod ma dodatkowo własny dedup po `comment_id`.

## 7. Abstrakcja providera
Interfejs `call(model, messages, params) -> {completion, usage}`; implementacje `openai.py`, `anthropic.py`. Mapowanie `model → provider`, normalizacja usage/kosztu do wspólnego formatu. Miejsce na trzeciego (OpenRouter) później.

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

### Prod (VPS) — systemd
- Serwer Iggy = **binarka z GitHub Releases** (nie kompilujesz Rusta), uruchamiana unitem systemd jak reszta usług.
- `Restart=always`, `WorkingDirectory` na katalog loga, `EnvironmentFile` na credentiale.
- **Port 8090 tylko na `localhost`** — projekty siedzą na tym samym VPS i gadają po `localhost:8090`. **nginx NIE jest w ścieżce** (SDK = surowy TCP, nie HTTP → nic nie proxujemy). Port nie musi być publiczny.
```ini
# /etc/systemd/system/iggy-server.service (szkic — flagi/ścieżki potwierdzić z docs)
[Unit]
Description=Apache Iggy server
After=network.target
[Service]
ExecStart=/usr/local/bin/iggy-server
WorkingDirectory=/var/lib/iggy
EnvironmentFile=/etc/iggy/.env
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
```

### Dev (laptop, macOS) — WŁASNY lokalny Iggy, NIE prod
- Do prac deweloperskich stawiasz **osobny, lokalny** serwer Iggy (na macu najszybciej **Docker**: `docker compose up -d`). **Nie łączysz się do Iggy na VPS.**
- **Dlaczego lokalnie, nie do VPS:**
  - **izolacja** — dev robi śmieciowe wiadomości, `delete topic`, restartuje workery, testuje replay. To NIE może dotknąć prod-loga, przez który lecą realne komentarze hate-moderatora.
  - **offline** — pracujesz bez VPS/sieci.
  - **bezpieczeństwo** — nie musisz wystawiać portu 8090 VPS-a na świat (localhost-only na prod zostaje bezpieczne).
- **Ten sam kod, inny serwer:** `llmbus` czyta `IGGY_ADDRESS` z `.env`. Dev `.env` → lokalny serwer; prod `.env` → serwer na VPS. Zero zmian w kodzie.
- **Fallback** (gdybyś nie chciał Dockera na macu): SSH-tunnel `ssh -L 8090:localhost:8090 vps` — port zostaje prywatny. UWAGA: to celuje w **prod-dane**, więc tylko do podglądu, nie do testów niszczących. Rekomendacja: osobny lokalny serwer.

**Zasada:** dwa osobne serwery, dwa osobne logi — standardowe rozdzielenie dev/prod.

## 10. Konfiguracja i sekrety
`.env`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `IGGY_ADDRESS`, limity (req/min, tok/min per provider), budżety per projekt. Nic hardcoded (python-dotenv).

## 11. Obserwowalność
- **Audyt:** topic `llm-jobs` = log wszystkich promptów (replay).
- **Koszt:** tabela w store per projekt/dzień.
- **Lag/stats:** „ile jobów czeka" — SDK nie ma `get_stats`; v1 przybliża licząc w store; v2 = rozbudowa SDK.

## 12. Braki Iggy SDK, które tu uderzysz (nie blokują v1)
- **nagłówki wiadomości** — metadane w headerach zamiast body.
- **get_stats** — monitoring lagu.
Oba to potencjalna kontrybucja; obejścia (body JSON, licznik w store) wystarczą na v1.

## 13. Świadomie odłożone (v2+)
skalowanie workerów, priorytety/fast-lane, dead-letter topic, streaming odpowiedzi, twarde limity budżetu, milamber, OpenRouter, dashboard.

## 14. Otwarte decyzje (do rozstrzygnięcia)
1. Worker **generyczny + callback** (reużywalny bus, hate-mod robi resztę) vs **domenowy** (worker robi classify+hide, pełne odcięcie ale bus nie-generyczny). Wstępna rekomendacja: **generyczny + callback**.
2. Rate-limit: tylko globalny w busie, czy zostawić też lokalny cap w hate-mod?
3. Dystrybucja klienta `llmbus` do innych repo (editable / path / pip prywatny)?
4. Results: `store + callback` wystarczy, czy chcesz też topic `llm-results`?
5. ~~Iggy server: docker lokalnie → potem VPS?~~ **ROZSTRZYGNIĘTE (sekcja 9b):** prod = binarka pod systemd na VPS (port 8090 localhost, nginx poza ścieżką); dev = osobny lokalny Iggy (Docker na macu), nie łączymy się do prod. Jeden serwer na VPS dla wszystkich projektów.
6. Model klasyfikacji dla hate-mod: który z rodziny GPT-5 (gpt-5-mini/nano) lub Anthropic? (OpenAI = GPT-5, nie 4o.)
7. Sync (poll `await_result`) vs async (callback) — czy oba wspieramy w v1, czy tylko callback?
