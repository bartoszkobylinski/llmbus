# llmbus — architektura (v0, draft do dyskusji)

**Data:** 2026-07-03 · **Status:** projekt, przed implementacją.
**Decyzje zamknięte:** pilot = `hate-moderator`; providerzy = OpenAI + Anthropic; backbone = Apache Iggy (Python SDK).

> To jest żywy dokument. Sekcja 14 to lista otwartych decyzji — dopisujemy odpowiedzi w miarę pytań.

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

> ⚠ **Pilot (hate-moderator) używa POLLA, nie callbacku — §14 #20 (2026-07-21).** Schemat wyżej
> opisuje wariant callbackowy, który zostaje wspierany (§14 #7) i zaimplementowany po stronie
> workera wraz z podpisem HMAC (§14 #19), ale **pierwszy realny konsument go nie używa**. Powód
> jest w kodzie hate-moda, nie w busie: na żywej ścieżce webhooka komentarz idący do `classify()`
> **nie ma żadnego wiersza `PendingComment`** (wiersze powstają wyłącznie w gałęziach awaryjnych),
> więc zgubiony callback = komentarz cicho zgubiony. Pełny materiał:
> `notes/hate-mod-integration-facts-2026-07-21.md` §4c.

## 4. Kontrakt wiadomości
**Job (na `llm-jobs`):**
```json
{
  "job_id": "uuid",
  "project": "hate-moderator",
  "kind": "classify|summarize|…",
  "model": "gpt-5-mini | claude-…",
  "messages": [{"role": "user", "content": "…"}],
  "params": {"temperature": null, "max_tokens": 512,
             "response_format": {"type": "json_schema", "name": "verdict",
                                 "schema": {"type": "object", "additionalProperties": false, "…": "…"}}},
             // temperature opcjonalne; response_format opcjonalne (null = wolny tekst), §14 #10
  "callback_url": "http://…/internal/classified",   // albo null → poll
  "meta": {"comment_id": "…"},                        // wraca nietknięte
  "submitted_at": "…",
  "ttl_s": 280.0                                      // opcjonalne; null = bez terminu, §14 #22
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
- **`response_format` (structured output) — wyłącznie wariant `json_schema`** (§14 #10, ponownie otwarte i rozstrzygnięte 2026-07-17): `{"type": "json_schema", "name": str, "schema": {…}}` albo `null` (wolny tekst). Ten jeden kształt mapuje się natywnie na obu providerów (OpenAI `response_format` json_schema + `strict: true`; Anthropic `output_config.format`); luźny `json_object` to koncept tylko-OpenAI i celowo NIE wchodzi. Walidacja wczesna (fail-loud): `schema` musi być niepustym schematem obiektu z top-level `additionalProperties: false` — tego wymagają strict-mode OBU providerów, więc odrzucamy przy submit, nie po zakolejkowaniu. Głębsza poprawność schematu i reguły znaków w `name` (wymóg OpenAI; Anthropic pola nie ma — adapter je pomija) zostają po stronie providera. W Pythonie pole nazywa się `json_schema` (`schema` cieniuje atrybut `BaseModel`), na drucie `schema` (`by_alias=True`).
- **`ttl_s` (termin ważności joba) — opcjonalny, `> 0` jeśli podany** (§14 #22). Liczony **względem `submitted_at`**, nie jako absolutny timestamp: `submitted_at` już jest w kontrakcie, więc producent podaje jedną liczbę i obie strony nie muszą uzgadniać nic poza tym zegarem. Worker sprawdza termin **przed każdą próbą** — raz przed rezerwacją rate-limitu (żeby martwy job nie zjadał kwoty potrzebnej żywym) i raz po niej, tuż przed wydaniem pieniędzy (bo `acquire` potrafi przespać całe okno). Job po terminie kończy się **terminalnym `Result` `status="error"`**, nie cichym pominięciem: wiersz w store musi osiągnąć stan terminalny, inaczej producent, który jeszcze pollinguje, wisiałby do własnego timeoutu, a licznik `pending` (§11) rósłby w nieskończoność. `null` = brak terminu (batch — wynik odbierany później); producent pollingujący z własnym timeoutem **powinien** ustawić `ttl_s` równe swojemu czekaniu, żeby obie strony poddawały się razem.
  **PRECONDITION — wspólny zegar (review 2026-07-22).** Termin jest liczony przez workera jako
  `submitted_at + ttl_s` wobec **zegara workera**. Przy rozjeździe zegarów: worker spóźniony o
  X sekund zacznie **płatne** wywołania do X sekund po tym, jak producent przestał czekać;
  worker śpieszący się wygasza **ważne** joby za wcześnie. Dziś to nie boli, bo producent i
  worker są **współlokowani na jednym boxie** (§9b) — jeden zegar, rozjazd zerowy z definicji.
  **Jeśli bus kiedykolwiek rozjedzie się na dwa hosty, to przestaje być prawdą** i trzeba
  wtedy: (a) wymusić NTP na obu i przyjąć tolerancję, albo (b) przejść na deadline absolutny z
  jawnym marginesem na skew. Nie „naprawiać" tego zgadywanką po stronie workera — dopóki
  współlokacja obowiązuje, to jest udokumentowane założenie, nie luka.
  Naiwny (bez strefy) `submitted_at` jest **odrzucany w kontrakcie** — inaczej porównanie
  wybuchłoby `TypeError` dopiero w środku ścieżki joba, po zakolejkowaniu. `ttl_s` musi być
  **skończone i ≤ `MAX_TTL_S` (86 400 s)**: `inf` przechodziło naiwne `> 0`, serializowało się
  do JSON-owego `null` i **po cichu wyłączało wygasanie** (deadline wyglądał na ustawiony i nie
  robił nic), a wielkie skończone wartości wywracały `timedelta` na `OverflowError`.
  **Współlokacja NIE wystarcza — precondition jest szerszy (review 2026-07-22).** Producent
  odmierza swoje czekanie zegarem **monotonicznym** (`await_result` → `time.monotonic`), a
  wygasanie liczy się zegarem **ściennym** (`submitted_at` + `ttl_s` wobec `datetime.now`).
  To są dwa różne zegary **na tym samym hoście**, więc **skok zegara ściennego** w trakcie
  życia joba (korekta NTP, ręczne przestawienie, zmiana czasu) rozjeżdża je nawet bez drugiego
  hosta: skok do przodu wygasza joby wcześniej, niż producent przestał czekać, skok do tyłu
  przedłuża okno, w którym worker może jeszcze zapłacić za pracę już porzuconą. Pełny warunek
  brzmi więc: **jeden host ORAZ brak skoków zegara ściennego w trakcie życia joba** (NTP w
  trybie `slew`, nie `step`). Deadline absolutny tego **nie** naprawia — przesunąłby tylko
  miejsce, w którym skok boli.

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
- **połączenie z brokerem (§14 #16):** klient budujemy **wyłącznie** przez `IggyClient.from_connection_string(iggy_connection_string(...))` — `iggy+tcp://user:pass@host:port`, poświadczenia percent-encoded. To ustawia SDK-owe `auto_login`, więc **`connect()` uwierzytelnia**, także przy każdym wewnętrznym reconnectcie SDK (`send_raw_with_response` → `disconnect` → `connect` → retry). Ręczny `login_user` na `IggyClient(addr)` zostawia `auto_login: Disabled` → reconnect wraca nieuwierzytelniony → `RuntimeError: Unauthenticated` bez samonaprawy. **Nie dodawać `login_user` z powrotem.** `connect_broker` dokłada tylko ponawianie **samego connectu** (własna polityka `WORKER_CONNECT_*`, osobna od retry jobów — zimny broker chce wielu krótkich prób, płatne wywołanie modelu kilku długich; §14 #11 dostroiło tamte pod joby). Ponawia **każdy** wyjątek, nie `is_retryable` (ten jest zakresowo tylko o błędach providera, §14 #12, i uznałby błąd brokera za terminalny). Każda próba dostaje **świeżego klienta** (nieudany connect potrafi zatruć poprzedniego). Po wyczerpaniu prób wyjątek leci dalej → proces pada → systemd `Restart=always` wznawia, więc złe hasło nadal pada głośno. **Nie objęte:** pojedyncza próba nie ma własnego timeoutu (zachowanie sprzed zmiany; `WORKER_CONNECT_TIMEOUT_S` → osobny PR).
- **provider routing:** po nazwie `model` → OpenAI albo Anthropic (`provider_for`); nieznany model / brak adaptera w rejestrze → `Result{status:"error"}`, żeby jeden zły job nie zatrzymał pętli.
- **timeout (§14 #11):** **per próba** (`WORKER_JOB_TIMEOUT_S`), przez wstrzyknięty runner (domyślnie `asyncio.wait_for`) — timeout wypada jako `TimeoutError` (transient → retry). Wstrzyknięcie runnera zamiast zegara ściennego trzyma `process_job` w bramce mutacyjnej bez realnego czasu, jak wstrzykiwany `sleep`/`clock` w `ratelimit`.
- **koszt:** z usage, per `project` → tabela kosztów (podstawa budżetu). Cennik jest **datowany** (`cost.py`: każdy model ma historię cen z datą wejścia w życie) — koszt liczony po stawce obowiązującej w dniu `submitted_at`, więc zaplanowane zmiany (np. koniec ceny promo Sonnet 5 dnia 2026-09-01) rozwiązują się same, bez ręcznej edycji i bez pobierania cen z sieci. `Decimal` z `cost.py` schodzi do `float` dopiero na granicy `Result.usage.cost_usd`. (Zapytanie agregujące per projekt/dzień jest **wdrożone**: `Store.cost_by_project_day()`, PR `worker-loop` — szczegóły w §11.)
- **idempotencja:** przy at-least-once (worker padł po modelu, przed commitem offsetu) job wraca; `store.finalize` jest one-shot (`WHERE status='pending'`), więc redostawa dostaje `False` → brak podwójnego zapisu i podwójnego callbacku. Redostawa **ponawia** wywołanie modelu (koszt) — świadomy, rzadki koszt (tylko recovery po crashu), a `finalize` jest gwarancją poprawności. hate-mod ma dodatkowo własny dedup po `comment_id`.
- **callback (§14 #14):** worker POST-uje `Result` (JSON, `by_alias`) na `callback_url` klientem **httpx** (extra `worker`). Gdy ustawiony `WORKER_CALLBACK_SECRET`, POST jest podpisany HMAC-SHA256 po ciele (nagłówek `X-Llmbus-Signature-256`, §14 #19), a worker serializuje ciało raz i wysyła te same bajty (`content=`), by podpis pokrywał drut. Dostawa jest **best-effort**: błąd POST-a → log + swallow, **bez retry** callbacku w v1 — wynik jest już trwale w store, więc poll (§11) to niezawodna ścieżka; retry/dead-letter callbacku to v2 (§13). Callback leci tylko gdy *ta* dostawa wygrała `finalize` (brak duplikatów).
- **pętla consumer-group (`worker.py`, PR `worker-loop`):** cienka powłoka Iggy. `consumer_group` (join `llm-workers`, `create_if_not_exists`) + `consume_messages(callback, shutdown_event)` z `AutoCommit.After(ConsumingEachMessage)` → **commit offsetu PO przetworzeniu** (at-least-once; redostawa bezpieczna przez one-shot `finalize`). Topologia (`Topology`: stream `llmbus`/topic `llm-jobs`/1 partycja/grupa `llm-workers`) domyślna, ale wstrzykiwalna (izolacja testów integracyjnych po uuid). Payload → `Job` przez `decode_job` (pydantic, `extra="forbid"`). **Poison message (§14 #15):** ciało nieparsujące się na `Job` → log (z obciętym raw) + skip + commit dalej; halt/retry zawiesiłyby jedynego workera na jednej złej wiadomości, a bez `job_id` i tak nie ma czego finalizować (dead-letter → v2, §13). Wejście: `run_worker` (wiring config→deps + pętla, powłoka I/O, poza bramką mutacyjną, testy integracyjne na żywym Iggy) + `python -m llmbus.worker` z SIGINT/SIGTERM → `shutdown`. Czyste szwy (`decode_job`, `ensure_topology`, `make_callback_sender`, `_consume_one`, `_load`) — unit-testy z atrapami.

## 7. Abstrakcja providera
Interfejs `call(model, messages, params) -> {completion, usage}`; implementacje `openai.py`, `anthropic.py`. Mapowanie `model → provider`, normalizacja usage/kosztu do wspólnego formatu. Miejsce na trzeciego (OpenRouter) później.

**Adapter OpenAI (`openai.py`, rodzina GPT-5).** Klient SDK jest **wstrzykiwany** (`config.py` buduje prawdziwy `AsyncOpenAI`; testy podają atrapę) — moduł nie importuje SDK i pozostaje czystą logiką (w bramce mutacyjnej). Mapowanie per-model wg realiów SDK (zweryfikowane, §14 #9): `max_tokens → max_completion_tokens`; `temperature` **odrzucane** gdy job je ustawi (GPT-5 honoruje tylko swoją domyślną — fail-loud przed wywołaniem API); `completion_tokens` (zawiera reasoning tokens) → `output_tokens`. Completion wraca **wyłącznie** przy `finish_reason == "stop"` — `"length"` (budżet `max_completion_tokens` obejmuje tokeny reasoningu, więc mały limit daje PUSTE completion; zmierzone live 2026-07-17) i każdy inny powód → `ValueError`, żadne ucięcie nie udaje sukcesu.

**Adapter Anthropic (`anthropic.py`, rodzina Claude).** Analogicznie: klient wstrzykiwany, czysta logika, brak importu SDK. Realia Messages API (zweryfikowane): `max_tokens` jest **wymagane** (job bez niego → `ValueError` przed wywołaniem); system prompt to osobny top-level `system=`, więc wiadomości `role=system` są **wydzielane** z `messages`; `temperature` jest **per-model** — `claude-opus-4-8` i `claude-sonnet-5` je odrzucają (400), a `claude-haiku-4-5` przyjmuje 0.0–1.0 (z walidacją zakresu). Completion to pierwszy blok `text` z `response.content`; `usage.input_tokens`/`output_tokens` mapują się wprost na `Usage`. Completion wraca **wyłącznie** przy `stop_reason == "end_turn"` — `"max_tokens"` (ucięcie; przy structured output to po cichu niepoprawny JSON), `"refusal"`, `"pause_turn"` → `ValueError`, symetrycznie do adaptera OpenAI. (Wsparcie temperatury w Haiku **potwierdzone live 2026-07-17** testem `live_api` — haiku przyjęło caller-set `temperature` bez 400.)

**Kontrakt „provider nie wycenia":** `ProviderResult.usage.cost_usd` musi zostać `0.0` — provider raportuje wyłącznie tokeny, a `cost.py` jest jedynym źródłem ceny (tabela datowana, §6, bez cen z sieci). `ProviderResult` **odrzuca** (`ValueError`) usage z niezerowym `cost_usd` — fail-loud, jak reszta kontraktu (§4). Dzięki temu cena zgłoszona przez API providera (np. przyszły OpenRouter, który zwraca koszt) nie przecieka i nie przesłania ceny liczonej lokalnie po stawce z dnia `submitted_at`. `Usage` jest przy tym **immutable** (`frozen=True`), więc `cost_usd` nie da się podmienić po konstrukcji — gwarancja jest strukturalna, nie tylko w chwili tworzenia (wycena downstream buduje **nowy** `Usage`, nie mutuje istniejącego).

## 8. Integracja hate-moderator (pilot) — co się zmienia
- **Zostaje w hate-mod:** webhook (HMAC), dedup po `comment_id`, **`moderate()`** (prompt-injection-odporny backstop — §14 #18), decyzja policy, `hide_comment`, zapis do DB, (semafor/cap — §14 #2).
- **Wychodzi do busa:** **wyłącznie `classify`** (chat-completion). `moderate()` NIE wchodzi — `moderations.create` to nie chat-completion (nie mieści się w chat-only `Job`, §4), jest darmowy i jest prompt-injection-odpornym backstopem, którego przenosiny by nie wzmocniły, a mogłyby osłabić (§14 #18).
- **Zmiana w kodzie (POPRAWIONE 2026-07-21, §14 #20 — poll, nie callback):** w
  `app/workers/poller.py::process_comment` (`poller.py:232`, sync) `moderate()` woła się nadal
  inline; następnie zamiast inline `classifier.classify(...)` (`poller.py:427-429`) idzie
  `submit` + **`await_result(job_id, timeout_s=BUS_TIMEOUT_S)`** — wywołanie zostaje
  **blokujące w tej samej ramce**, więc cały ogon decyzja → `hide_comment` → atomowy
  `db.commit()` (`poller.py:465-574`) zostaje **nietknięty**. Job niesie `response_format` =
  `json_schema` (§14 #10 — zamyka fail-open fallback „nieparsowalne → neutral").
  **`TimeoutError` z busa wpada w ISTNIEJĄCĄ ścieżkę awaryjną** `_enqueue_pending(reason=
  "classifier_unavailable")` (`poller.py:442-451`) — z punktu widzenia hate-moda timeout busa
  wygląda dokładnie jak dzisiejsza niedostępność klasyfikatora, więc nie powstaje nowa semantyka
  awarii. **`/internal/classified` NIE powstaje.** Cykl życia `BusClient` — §14 #17 (lifespan dla
  weba, `asyncio.run` wokół całego przebiegu dla crona). Blokery B1–B6:
  `notes/hate-mod-integration-survey.md`; stan faktyczny kodu:
  `notes/hate-mod-integration-facts-2026-07-21.md`.
- **Czego ta wersja świadomie NIE kupuje:** web **czeka** na werdykt (dziś też czeka — na
  synchroniczne OpenAI), więc „flood nie dotyka web-latency" z pierwotnego §8 **nie zachodzi**
  w tym wariancie. Kupujemy centralny rate-limit/retry/koszt (§1, §6) i trwałość zlecenia na
  Iggy; nie kupujemy odczepienia latencji. To był świadomy wybór (§14 #20): odczepienie latencji
  kosztowałoby przebudowę prodowej ścieżki moderacji i otwierało dziurę cichego gubienia
  komentarzy.
- **Budżet czasu — sprzężenie configu MIĘDZY REPOZYTORIAMI, dwustronne.** Czas czekania
  hate-moda (`settings.llmbus_timeout_seconds`) jest ściśnięty z dwóch stron i **żadna** wartość
  nie spełniała obu przy pierwotnym leasie:
  **(góra)** musi być **mniejszy** niż `CLAIM_LEASE` hate-moda — inaczej cron przejmie wiersz,
  którego job jeszcze leci, i zapłacimy za `classify` dwa razy + zrobimy drugi `hide`
  (regresja zamknięta ich commitem `376699b`);
  **(dół)** musi być **większy** niż najgorszy przypadek workera — inaczej porzucamy job, który
  worker wciąż ponawia; ten kończy się potem sukcesem do store'a, którego już nikt nie czyta, a
  ponowienie wysyła **nowy `job_id`** i płaci drugi raz.
  **KOREKTA (2026-07-21, po review Codeksa): dolna granica to NIE jest najgorszy przypadek
  pojedynczego joba — to on razy GŁĘBOKOŚĆ KOLEJKI.** Pierwsza wersja tego akapitu liczyła
  latencję per-job i to było **błędne**. Bus v1 ma **jedną partycję** i konsumuje **ściśle
  szeregowo** (`worker.py:74` `partitions: int = 1`; `_handle_message` robi gołe
  `await process_job` per wiadomość, commit-after-each), a hate-mod wysyła do
  `max_concurrent_classifications` (4) jobów **naraz**. Ostatni z N równoległych submitów czeka
  więc N najgorszych przypadków, nie jeden. Pełny warunek:
  `concurrency × per_job_worst ≤ timeout < CLAIM_LEASE`.
  Stock worker (`WORKER_MAX_ATTEMPTS=4` × `WORKER_JOB_TIMEOUT_S=60` + backoff, §14 #11) daje
  ~275 s/job → `4 × 275 = ~1100 s`, czego **żaden** timeout pod leasem nie spełnia. Sam lease
  300 → 600 s tego nie ratował.
  **Rozstrzygnięte (2026-07-21, user): tniemy człon per-job — worker dla tego wdrożenia dostaje
  `WORKER_MAX_ATTEMPTS=2` i `WORKER_JOB_TIMEOUT_S=30` (~61 s/job).** Wtedy
  `4 × 61 = 244 ≤ 280 < 600` domyka się z zapasem, a `CLAIM_LEASE` 10 min (podniesiony wcześniej)
  zostaje. Założenie o worst-case workera mieszka teraz w **nazwanym ustawieniu**
  `llmbus_worker_worst_case_seconds` po stronie hate-moda i jest **walidowane przy starcie**
  (razem z resztą kontraktu enabled-mode), więc rozjazd wywala się przy boocie, a nie jako cicha
  podwójna płatność pod obciążeniem.
  **Uwaga — `WORKER_*` jest globalny dla workera, nie per-job:** drugi konsument odziedziczy 2
  próby zamiast 4. Akceptowalne, dopóki hate-mod jest jedynym konsumentem; **to jest trigger do
  rewizji** (wtedy: per-job budżet w kontrakcie §4 albo realna równoległość — więcej partycji /
  współbieżny worker, co jest właściwym lekiem na niedopasowanie „równoległy producent ↔
  szeregowy konsument", a nie budżet obchodzący problem). Rozważone i odrzucone teraz:
  serializacja submitów po stronie hate-moda (działa, ale różnicuje zachowanie ścieżki busowej
  pod obciążeniem) oraz równoległość w busie (osobny PR + własna bramka, blokuje pilota).

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

### Prod (VPS `izabela213`) — **POSTAWIONE i działa** (zweryfikowane 2026-07-17)
Artefakty i runbook: **`deploy/`** (`iggy-server.service`, `llmbus-worker.service`,
`iggy.env.example`, `deploy.sh`, `README.md`).

**Stan na 2026-07-17 09:30 UTC — sprawdzone bezpośrednio na maszynie** (nie z notatek):
`iggy-server.service` i `llmbus-worker.service` oba `active`; broker słucha na
`127.0.0.1:8092`; worker `NRestarts=0` (systemd **nigdy** go nie wskrzeszał → zero crashy),
`MainPID` stabilny od 08:23:03 UTC, zero tracebacków i zero `broker connect failed`.
Siedem linii „worker consuming" z 08:06–08:23 to **ręczny** test 6 restartów z PR
`iggy-connection-string` (§14 #16), nie pętla awaryjna — dowodzi tego właśnie `NRestarts=0`.

Z **wcześniejszych** sesji, nieprzewierzone ponownie 2026-07-17 (nie mylić z powyższym):
smoke end-to-end (`deploy/smoke.py`, realny gpt-5-nano: submit → Iggy → worker → OpenAI →
SQLite → `await_result` → `'pong'`) przeszedł **2026-07-17 ~08:55 UTC**, po wdrożeniu
`iggy-connection-string`; zużycie `iggy-server` ~94 MB / `llmbus-worker` ~113 MB (cgroup
`MemoryCurrent`) zmierzono **2026-07-14**. Odtworzenie: `.venv/bin/python deploy/smoke.py`
na boxie (~$0.00006).

Ustalenia potwierdzone na maszynie (2026-07-14, nadal aktualne):
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
`.env`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `IGGY_ADDRESS`/`IGGY_USERNAME`/`IGGY_PASSWORD`, `STORE_PATH` (plik SQLite — pisany przez workera, czytany pollem przez producenta, §9b), limity (req/min, tok/min per provider), polityka workera (`WORKER_*`, §6/§14 #11), polityka handshake'u z brokerem (`WORKER_CONNECT_*`, §6/§14 #16) i budżety per projekt. Nic hardcoded (python-dotenv). `Config` (współdzielony producent+worker) niesie klucze/Iggy/`db_path`; `WorkerPolicy` (`parse_worker_policy`) parsuje `WORKER_*` osobno, bo producent ich nie potrzebuje, a `parse_connect_policy` — `WORKER_CONNECT_*` jako **osobną** `RetryPolicy` (nie pole `WorkerPolicy`: ta jest per-job, handshake jest startupowy; §14 #16).

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
- **Widok kosztu (impl., PR `cost-dashboard`):** `llmbus-costs` — CLI renderujący ten sam
  `cost_by_project_day()` do **samodzielnego pliku HTML** (`dashboard.py` = czysta funkcja
  `wiersze → string`, w bramce mutacyjnej; `cli.py` = powłoka I/O, jak split
  `worker-core`/`worker-loop` z §6). Strona nie ma zależności sieciowych (inline CSS, zero
  JS, zero fontów), więc czyta się z `file://` i przenosi `scp`. Zakres celowo mały:
  **generator pliku, nie serwer** — bus nie dostaje portu HTTP ani powierzchni auth (§1),
  a na VPS-ie z problemem OOM (runbook) nie przybywa procesu. Trzy rzeczy warte
  odnotowania, bo są decyzjami, nie domyślnymi:
  (a) **6 miejsc po przecinku wszędzie** — koszt joba jest podcentowy (smoke ≈ $0,00006),
  więc konwencjonalne 2 miejsca renderowałyby całą księgę jako `$0.00`;
  (b) **kwoty wracają do `Decimal`** (`Decimal(str(x))`, nie `Decimal(float)`) — store
  oddaje `float` z SQLite `SUM`, a sumowanie floatów odtworzyłoby dokładnie ten błąd
  akumulacji, przed którym `cost.py` broni;
  (c) **dni to słupki, nie linia** — ledger ma `HAVING > 0`, więc dni bez wydatku są
  *nieobecne*, nie zerowe; linia narysowałaby zbocze przez lukę, której w danych nie ma.
  Brak pliku store'a jest **błędem twardym** (exit 2), nie pustą stroną: `Store.connect()`
  tworzy plik i schemat, więc literówka w ścieżce dałaby pewny siebie raport `$0.000000`
  i zostawiła pusty plik DB. Raport czyta **wyłącznie** SQLite (`config.parse_store_path` —
  bez kluczy API i bez Iggy), więc chodzi też przy leżącym workerze, a WAL (§9b) gwarantuje,
  że czytelnik nie blokuje pisarza.
- **Widok kosztu przez HTTP (impl., PR `cost-dashboard`):** `llmbus-costs-serve` — ta sama
  strona, renderowana **na każde żądanie** zamiast zapisywana raz do pliku, po to żeby
  wpiąć ją w moduł `projects` milambera. Ten moduł jest **rejestrem, nie proxy**
  (`api/routers/projects.py`, `api/templates/projects.html`): trzyma `{name, port,
  description}`, zapala „online" wyłącznie na podstawie `socket.create_connection(
  ("127.0.0.1", port))`, a link otwiera `http://<host, na którym oglądasz milambera>:<port>`.
  Nic tam nie przekazuje ruchu, więc to llmbus musi słuchać na tailnecie.
  **Dlatego serwer binduje WIĘCEJ NIŻ JEDEN adres** (`COSTS_BIND_HOSTS`, domyślnie sam
  loopback): health-check chce `127.0.0.1`, przeglądarka chce adresu tailnetu. `0.0.0.0`
  spełnia oba, ale wystawia koszt per projekt na **publiczny** interfejs bez auth; sam
  adres tailnetu (wzorzec `capcycle-web`) zostawia kartę na wiecznym, fałszywym „Offline".
  Dwa gniazda w jednym procesie dają oba, nie dają publicznego i **nie wymagają vhosta
  nginx** — czyli omijają pułapki 443/tailscaled z runbooka. Serwer to **wyłącznie
  stdlib** (`http.server`); wciąganie FastAPI/uvicorn do *busa* dla jednej strony to
  dokładnie ten scope creep, przed którym stoi §1. `asyncio.run` na żądanie jest legalne,
  bo każde żądanie leci na własnym wątku (bez działającej pętli) — ten sam most „na
  własnej krawędzi" co `cli.py` (§14 #17).
  **Strona nie ma uwierzytelniania — to jest świadome i dlatego domyślny bind to loopback:**
  granicą dostępu jest sieć (ta sama postawa „Phase 0", którą runbook zapisuje dla
  `capcycle-web`). Nie poszerzać bindu do `0.0.0.0` bez dołożenia auth — i to jest
  **wymuszone, nie tylko opisane**: `0.0.0.0`, `::` oraz pusty host lecą `ConfigError`
  (exit 2) zarówno z `COSTS_BIND_HOSTS`, jak i z flagi `--host`. Review Codeksa pokazał,
  że sam komentarz w `.env.example` nie jest mechanizmem: flaga omijała walidację `.env`,
  a `--host ""` to dokładnie ten sam wildcard, tylko zapisany po socketowemu.
- **Polityka workera (impl., PR `worker-policy-publish`, §14 #21):** tabela `worker_policy`
  (jeden wiersz, `id=1`, upsert przy każdym boocie workera **przed** rozpoczęciem konsumpcji)
  niesie `max_attempts`/`job_timeout_s`/`base_delay_s`/`max_delay_s`, wyliczony
  `worst_case_s` (`retry.worst_case_seconds`, czysta funkcja w bramce mutacyjnej) oraz
  `updated_at`. Czyta się przez `BusClient.worker_policy()`. Dzięki temu producent, który
  pollinguje wynik, **skaluje swój timeout wobec faktu**, a nie wobec liczby przepisanej do
  własnego configu. Dla operatora to też odpowiedź na „z jaką polityką ten worker realnie
  chodzi" bez wchodzenia w `.env` na boxie.

## 12. Braki Iggy SDK, które tu uderzysz (nie blokują v1)
- **nagłówki wiadomości** — metadane w headerach zamiast body.
- **get_stats** — monitoring lagu.
Oba to potencjalna kontrybucja; obejścia (body JSON, licznik w store) wystarczą na v1.

## 13. Świadomie odłożone (v2+)
skalowanie workerów, priorytety/fast-lane, dead-letter topic, streaming odpowiedzi, twarde limity budżetu, milamber, OpenRouter.

**„dashboard" zszedł z tej listy tylko częściowo (2026-07-23).** Zbudowany jest *statyczny
raport kosztu* — `llmbus-costs` generuje plik HTML (§11). **Nadal odłożone** jest to, co z
tej pozycji zostało: dashboard **serwowany** (proces HTTP, auth, filtry zakresu dat,
odświeżanie na żywo, lag/kolejka obok kosztu). To osobna decyzja niż widok kosztu — dodaje
busowi port, powierzchnię uwierzytelniania i stały proces na boxie; nic z tego nie było
potrzebne, żeby odpowiedzieć na pytanie „ile wydałem i na co".

## 14. Otwarte decyzje (do rozstrzygnięcia)
1. ~~Worker **generyczny + callback** vs **domenowy**.~~ **ROZSTRZYGNIĘTE —
   generyczny + callback.** Bus zostaje reużywalny: worker liczy model/koszt/retry
   i POST-uje surowy `Result` na `callback_url`, a domenę (decyzja hide, `hide_comment`,
   zapis) robi hate-mod w `/internal/classified` (§3, §8). Worker nie zna semantyki
   `kind` — to trzyma §1 (bus generyczny, mały). Decyzja podjęta w PR `worker-core`.
2. ~~Rate-limit: tylko globalny w busie, czy zostawić też lokalny cap w hate-mod?~~
   **ROZSTRZYGNIĘTE (2026-07-18) — lokalne limity ZOSTAJĄ w hate-mod.** To dwie różne
   rzeczy, nie jeden „rate-limit": `BoundedSemaphore(4)` chroni *własny* threadpool/pamięć
   procesu hate-moda (nie providera), a rolling-24h cap per user to *reguła produktowa*
   (domena). Bus przejmuje wyłącznie globalny rate-limit providera (§6); §1 mówi, że bus
   nie zna domeny, więc żadnego z tych dwóch nie wciąga. Wsad: `notes/hate-mod-integration-survey.md` §3.
3. ~~Dystrybucja klienta `llmbus` do innych repo (editable / path / pip prywatny)?~~
   **ROZSTRZYGNIĘTE (2026-07-18) — editable path install.** Oba repo są lokalne w
   `~/Programming/Python/`, a `llmbus` niesie `py.typed` (§14 #8), więc typy przechodzą do
   konsumenta. hate-mod dodaje `llmbus` jako editable/path zależność (`uv`), bez prywatnego
   indeksu pip w v1.
4. ~~Results: `store + callback` wystarczy, czy chcesz też topic `llm-results`?~~
   **ROZSTRZYGNIĘTE — `store + callback`, bez `llm-results` w v1.** Wyniki NIE wracają
   przez Iggy (§5): worker zapisuje `Result` do store (SQLite), dostawa idzie callbackiem
   (§3) i/lub pollingiem (#7). Osobny topic `llm-results` to scope wobec §1 — odłożony do
   v2. Decyzja podjęta w PR `store`.
5. ~~Iggy server: docker lokalnie → potem VPS?~~ **ROZSTRZYGNIĘTE (sekcja 9b):** prod = binarka pod systemd na VPS (**`127.0.0.1:8092`** — nie 8090, które na tym boxie zajmuje `beziarnia`/gunicorn; nginx poza ścieżką); dev = osobny lokalny Iggy (Docker na macu, `8090`), nie łączymy się do prod. Jeden serwer na VPS dla wszystkich projektów.
6. ~~Model klasyfikacji dla hate-mod: który z rodziny GPT-5 (gpt-5-mini/nano) lub Anthropic?~~
   **ROZSTRZYGNIĘTE (2026-07-18) — zostaje `gpt-5.4-mini`.** To już świadomy wybór prod
   hate-moda (`config.py:23-29`): `-nano` przepalał false-positive na polskich niuansach,
   a stawki kosztu są przypięte do tego modelu. #6 to więc potwierdzenie istniejącej
   wartości, nie zmiana kodu. **KOREKTA (2026-07-20): to zdanie było BŁĘDNE.** Sprawdzono
   wyłącznie, że hate-mod ma ten model w swoim configu — NIE sprawdzono, czy llmbus go w
   ogóle **routuje i wycenia**. Nie robił ani jednego, ani drugiego: `gpt-5.4-mini` nie
   istniał w `providers/base.py::PROVIDERS` ani w `cost.py::PRICING`, więc **każdy** job
   `classify` padłby na `UnknownModelError` → `result_error` — integracja wyglądałaby na
   żywą, a nie przetworzyłaby ani jednego komentarza. Model dodany w PR
   `feat/model-registry-fail-loud` (0.75/4.50 per M; zweryfikowane 2026-07-20 wobec
   cennika OpenAI i niezależnie zgodne z `config.py:30-31` hate-moda; dowody:
   `notes/model-pricing-openai.md`). W tym samym PR `BusClient.submit` waliduje model
   **przed** zapisem `pending` i wysyłką na Iggy — nieznany model pada teraz w miejscu
   wywołania, a nie jako błędny `Result` rundę później. Lekcja: „model jest w configu
   konsumenta" nie znaczy „bus go obsłuży" — to dwie osobne tabele i rozjazd wychodzi
   dopiero w runtime. Uwaga: live-test structured output (§14 #10) szedł na
   `gpt-5-nano`; `-mini` to ta sama rodzina/API, mapowanie `json_schema`+`strict`
   identyczne, ale nie zweryfikowane live osobno dla `-mini`.
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
   **PONOWNIE OTWARTE (2026-07-17) → ROZSTRZYGNIĘTE NA NOWO — wariant C (typ
   strukturalny); wdrożenie w PR `structured-output`.** Decyzja A zapadła bez sprawdzenia
   pilota: hate-moderator — jedyny realny konsument busa i powód jego istnienia — **stoi
   na tym polu** (`classifier.py:171`: `response_format={"type": "json_object"}`; pełny
   materiał dowodowy: `notes/hate-mod-integration-survey.md`, bloker B1). Bez tego pola
   kontrakt JSON klasyfikatora jest tylko promptowy, a fallback „nieparsowalne → cicho
   neutral" **failuje otwarcie** (przepuszcza komentarz). Argument „C to scope creep"
   upadł: to nie jest funkcja na zapas, tylko warunek wykonalności §8.
   **Nowy kształt — kontrakt dostaje WYŁĄCZNIE wariant `json_schema`:**
   `JobParams.response_format = {"type": "json_schema", "name": str, "schema": {…JSON
   Schema…}} | None`. Ten wariant mapuje się czysto na OBU providerów — zweryfikowane
   2026-07-17 na SDK w wersjach przypiętych w `uv.lock` (introspekcja typów, nie pamięć):
   OpenAI 2.44.0 → `response_format={"type": "json_schema", "json_schema": {name, schema,
   strict, description?}}`; Anthropic 0.116.0 → `output_config={"format": {"type":
   "json_schema", "schema": …}}` (top-level `output_format` jest deprecated). Oba strict
   warianty wymagają `additionalProperties: false` — czysty walidator w `schema.py`
   wymusi to wcześnie (fail-loud, jak §4). **`json_object` (luźny „JSON mode") celowo NIE
   wchodzi do kontraktu** — to koncept tylko-OpenAI bez odpowiednika u Anthropica, czyli
   dokładnie ta per-providerowa niesymetria, przez którą #10 poleciało za pierwszym razem.
   hate-mod przy integracji przechodzi z `json_object` na `json_schema` — dla niego
   ściśle lepiej: odpowiedź walidowana schematem, więc otwarty fallback przestaje być
   osiągalny w zwykłym trybie. Oryginalny zarzut #10 zostaje uszanowany: pole znaczy
   dokładnie to samo u każdego providera. **POTWIERDZONE LIVE (2026-07-17, testy
   `live_api`):** `gpt-5-nano` przez Chat Completions przyjmuje `json_schema`+`strict`
   z naszymi mapowaniami (#9: `max_completion_tokens`, brak `temperature`), a
   `claude-haiku-4-5` przyjmuje nasz `output_config` — oba zwracają JSON zgodny ze
   schematem. Gotcha z weryfikacji: `max_completion_tokens` u GPT-5 obejmuje tokeny
   reasoningu (zmierzone: 448 na jednolinijkowy prompt), więc budżet 128 kończy się
   `finish_reason="length"` i **pustym** completion — początkowo adapter przepuszczał to
   jako sukces (fail-loud łapał tylko `content=None`); **załatane w tym samym PR:** oba
   adaptery zwracają completion wyłącznie przy czystym zakończeniu (`finish_reason ==
   "stop"` / `stop_reason == "end_turn"`), każde ucięcie → `ValueError` (§7). Minimum
   dla structured output na GPT-5 to realnie ~1–2k tokenów budżetu.
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
16. ~~**`Unauthenticated`/`Disconnected` przy starcie workera: jak budować klienta Iggy.**~~
   **ROZSTRZYGNIĘTE (PR `iggy-connection-string`) — `IggyClient.from_connection_string`,
   NIGDY `IggyClient(addr)` + ręczny `login_user`.** Auth w Iggy jest **per sesja TCP**.
   Każda komenda idzie przez SDK-owe `send_raw_with_response`, które po błędzie
   przejściowym — **oraz po samym `Unauthenticated`** — robi `disconnect()` → `connect()`
   → retry. Przy `IggyClient(addr)` SDK ma `auto_login: Disabled`, więc **`connect()` nie
   uwierzytelnia** (loguje „Automatic sign-in is disabled"), a my logujemy się ręcznie —
   ten reconnect wraca więc na sesji **nieuwierzytelnionej**, bo SDK nie ma poświadczeń do
   odtworzenia. Efekt: `RuntimeError: Unauthenticated` na pierwszej komendzie po
   reconnectcie, bez szans na samonaprawę (Unauthenticated → reconnect → znów
   Unauthenticated). Connection string ustawia `auto_login: Enabled(UsernamePassword)`, więc
   SDK loguje się **wewnątrz `connect()`** — także przy każdym swoim reconnectcie.
   **Dowody (2026-07-16/17):** (1) na żywym brokerze `IggyClient(addr).connect()` →
   `get_stream()` = `Unauthenticated`, a `from_connection_string(...).connect()` →
   `get_stream()` = OK, bez żadnego `login_user` w obu przypadkach; (2) log brokera: „Logged
   in user: iggy" na sesji A, „unauthenticated access attempt" na sesji B; (3) 5-sekundowa
   luka między logowaniem a krachem = `reconnection.reestablish_after` (default 5 s), które
   `connect()` odczekuje; (4) źródło `apache_iggy` 0.8.0 (sdist, sha256 zgodny z `uv.lock`):
   `tcp_client.rs` `send_raw_with_response` + gałąź `AutoLogin::Disabled`,
   `connection_string.rs` → `AutoLogin::Enabled(Credentials::UsernamePassword)`.
   **Czyj to błąd — uczciwie:** naprawa jest po **naszej** stronie i nie zależy od Iggy
   (dowód (1) jest samowystarczalny). Ale **nie** dlatego, że „trzymaliśmy SDK niezgodnie z
   ich przykładami" — to twierdzenie było **fałszywe** i zostało stąd usunięte: ich
   **główna** fikstura testowa (`foreign/python/tests/conftest.py:55`) używa dokładnie
   naszego, ręcznego wariantu (`IggyClient(addr)` + `login_user`); connection string pojawia
   się u nich w teście *samego konstruktora*, w testach TLS (gdzie jest jedyną opcją) i w
   teście **negatywnym**. Ich testy tego nie łapią, bo są krótkie, na świeżym serwerze i
   nigdy nie reconnectują. **Do zgłoszenia upstream (dwie rzeczy, nie blokują nas):**
   (a) ręczny `login_user` + auto-reconnect **po cichu** gubi uwierzytelnienie — kanoniczny
   wariant z ich własnej fikstury nie jest reconnect-safe; (b) `leader_aware.rs` **połyka**
   błąd `get_cluster_metadata` (`warn!` + `Ok(None)`), więc `login_user()` zwraca `Ok` na
   sesji, która właśnie odpowiedziała `Unauthenticated` — to ukrywa (a), ale go nie powoduje.
   **Pełny materiał dowodowy (dosłownie: wyniki, logi brokera, wycinki źródeł, oba skrypty
   odtwarzające, gotowe zgłoszenia PL/EN, lista rzeczy NIEudowodnionych):**
   `notes/iggy-sdk-auth-reconnect.md`. Hasło/użytkownik są **percent-encoded** (`:`/`@`/`/` w haśle przemodelowałyby
   URL). **Wcześniejsza diagnoza w tym #16 była BŁĘDNA** i została tu zastąpiona: mówiła, że
   sedno to `is_retryable(RuntimeError("Disconnected")) == False` i że lekiem jest retry
   handshake'u; retry **nie wystarcza** (Python widzi `connect`+`login` jako sukces, więc
   nigdy się nie odpala) — zostaje wyłącznie na **zimny broker**. Nie powielać tamtej
   historii.
   **(a) Budżet retry connectu:** osobne `WORKER_CONNECT_MAX_ATTEMPTS=10` /
   `WORKER_CONNECT_BACKOFF_BASE_S=0.25` / `WORKER_CONNECT_BACKOFF_MAX_S=5` zamiast reużycia
   `WORKER_*` — inne domeny awarii (zimny broker vs płatne 429), a §14 #11 dostroiło tamte
   pod ścieżkę jobów; wspólna polityka znaczyłaby, że strojenie jednej cicho stroi drugą.
   **(b) Klasyfikacja:** `connect_broker` ponawia **każdy** wyjątek zamiast uczyć
   `is_retryable` o błędach brokera — to poszerzyłoby zakres §14 #12 (tylko błędy providera,
   w bramce mutacyjnej) na ścieżkę jobów. Świeży klient na próbę: nieudany connect potrafi
   zatruć poprzedniego (tak samo robi `_connect_or_skip` w suite integracyjnym).
   **Odłożone:** timeout pojedynczej próby (`WORKER_CONNECT_TIMEOUT_S`) — dziś nieosiągalny
   broker blokuje w kliencie Rust; zachowanie sprzed zmiany, więc osobny PR.
17. **Sync/async na styku z pilotem.** **ROZSTRZYGNIĘTE (2026-07-17) — hate-mod pozostaje
   synchroniczny; llmbus pozostaje async-only.** Zasada „no sync wrappers in v1" stoi —
   każdy most sync→async mieszka po stronie hate-moda, nie w busie. Uzasadnienie z kodu
   (cytaty: `notes/hate-mod-integration-survey.md`), nie z gustu:
   (a) sama zmiana §8 **usuwa** jedyne wolne I/O hate-moda — wywołanie OpenAI wychodzi
   z procesu — więc przepisanie apki na async kupowałoby zdolność czekania dokładnie
   wtedy, gdy przestaje mieć na co czekać; (b) promień rażenia przepisania: 212 miejsc
   synchronicznego SQLAlchemy (`app/db.py:4-33`, `create_engine`/`Session`) + ~60 testów
   `process_comment`, na systemie moderującym realne komentarze na produkcji;
   (c) jedyny szew, który potrzebuje async — webhook `handle_webhook`
   (`routes/webhook.py:37`) — **już jest** `async def`. Kształt integracji: submit
   natywnie async w webhooku; `/internal/classified` **sync** (FastAPI i tak woła sync
   endpointy w threadpoolu — istniejący kod decyzja/hide/`db.commit()` reużyty bez
   zmian); cron `drain_queue.py` mostkuje `asyncio.run` na własnej krawędzi.
   **Cykl życia `BusClient` — ROZSTRZYGNIĘTE (2026-07-19); survey ujawnił DWA procesy
   sięgające `classify()`, nie jeden (`notes/hate-mod-integration-facts.md` §5):**
   (1) **ścieżka web** (zawsze żyjący uvicorn, 1 worker) — jeden trwały loop + jeden
   `BusClient` w FastAPI `lifespan` (`main.py:220-235`); sync `process_comment`
   (`poller.py:232`) sięga po niego przez `run_coroutine_threadsafe`.
   (2) **cron `drain_queue.py`** (osobny, krótko żyjący proces, BEZ lifespanu i loopa) —
   **Option A:** jeden `BusClient` na CAŁY przebieg draina (`asyncio.run` wokół
   `drain_all()`), reużywany dla wszystkich pending-komentarzy w tym przebiegu, **nie**
   nowy klient per komentarz (to jest churn sesji TCP, przed którym ostrzega #16). Naiwne
   `asyncio.run(submit)` per komentarz odrzucone.
18. ~~**`moderate()` na bus w v1?**~~ **ROZSTRZYGNIĘTE (2026-07-18) — NIE; potwierdzone przez usera.**
   §8 mówi „wychodzi do busa moderation + classify", ale `moderations.create` nie jest
   chat-completion i nie mieści się w chat-only `Job` (§4, `messages: list[Message]`);
   rozszerzenie kontraktu o typ nie-chatowy to realny scope creep, którego nie wymusza
   żaden konsument. Ważniejsze — własny komentarz hate-moda (`classifier.py:240-248`)
   opisuje `moderate()` jako **odporny na prompt-injection backstop** („content
   classifier, not an instruction-follower — CANNOT be prompt-injected"); endpoint jest
   darmowy i nie liczy się do usage, więc centralny rate-limit/koszt/retry nie daje mu
   nic, a przenosiny ryzykują osłabienie zabezpieczenia przy okazji. Decyzja:
   `moderate()` zostaje inline w hate-mod; na bus idzie samo `classify`; §8
   przepisane zgodnie z tym w tym PR.
19. **Uwierzytelnianie callbacku (bus→hate-mod) — ROZSTRZYGNIĘTE (2026-07-19) — HMAC po ciele.**
   Worker POST-uje `Result` na `callback_url` (§6/§14 #14); po stronie hate-moda to nowy
   **sync** endpoint `/internal/classified`, którego dziś NIE ma (survey:
   `notes/hate-mod-integration-facts.md` §3). Callback podpisujemy **HMAC-SHA256 po surowym
   ciele** żądania, nagłówek `X-Llmbus-Signature-256: sha256=<hex>` — ten sam kształt co
   istniejąca weryfikacja Meta-webhooka w hate-mod (`webhook.py:100-106`,
   `hmac.compare_digest`), więc strona odbiorcza jest niemal darmowa. Sekret: opcjonalny
   `WORKER_CALLBACK_SECRET` (`.env`, worker-only; brak = callback niepodpisany — domyślnie
   v1 dla callbacku tylko-localhost). **Wdrożenie (PR `feat/callback-hmac-auth`):** czyste
   (bramka mutacyjna) `callback_signature`/`callback_headers` w `processing.py`;
   `worker.make_callback_sender` serializuje ciało RAZ i POST-uje **te same bajty**
   (`content=`, nie `json=`), by podpis pokrywał dokładnie to, co idzie na drut. Odrzucone:
   (A) sam shared-secret w nagłówku (tożsamość bez integralności), (B) tylko-localhost bez
   auth (lokalny proces mógłby sfałszować werdykt hide).
21. **Czym producent ma weryfikować budżet czasu (§8)?** **ROZSTRZYGNIĘTE (2026-07-21) —
   worker PUBLIKUJE swoją efektywną politykę do współdzielonego store'a, producent ją czyta.**
   §8 wymaga `concurrency × per_job_worst ≤ timeout < CLAIM_LEASE`, ale `per_job_worst` żył
   jako stała w configu **konsumenta** (`llmbus_worker_worst_case_seconds = 61`) — czyli jako
   *przekonanie* o `.env` cudzego procesu. Nic nie wykrywało, że przestało być prawdziwe, a
   objawem nie jest błąd, tylko **cicha podwójna płatność** przy retry providera.
   **Wdrożenie:** `Store.publish_worker_policy(policy)` przy każdym boocie workera, **przed**
   rozpoczęciem konsumpcji (producent łączący się w trakcie dołączania workera do grupy i tak
   czyta aktualny wiersz); tabela `worker_policy`, jeden wiersz przypięty `id=1`, upsert — więc
   store opisuje workera, który chodzi **teraz**, nie tego, który wstał pierwszy.
   `BusClient.worker_policy()` czyta. Ceiling liczy czysta `retry.worst_case_seconds`
   (bramka mutacyjna), a nie komentarz.
   **`None` jest stanem legalnym, nie błędem:** producent może wysłać zanim worker wystanie
   pierwszy raz (§5 — tworzenie topologii jest idempotentne i nie zależy od kolejności). Bus
   **nie rozstrzyga**, czy nieweryfikowalny budżet to ostrzeżenie czy odmowa — publikuje fakt,
   decyzja należy do konsumenta (§1: bus nie zna domeny).
   **Przy okazji wyszedł błąd w moich własnych liczbach:** wcześniejsze „~275 s" dla stock
   workera jest **złe** — prawdziwy ceiling to **243,5 s** (backoffy 0,5+1+2 = **3,5**, nie
   ~35; liczone ręcznie w komentarzu). Żadnej decyzji to nie zmienia (4 × 243,5 = 974 > 600
   nadal nie mieści się pod leasem, a `61` po stronie hate-moda jest konserwatywne wobec
   realnych 60,5) — ale to dokładnie klasa pomyłki, którą ta funkcja likwiduje.
   **Odrzucone:** (B) skrypt asercji przy deployu — sprawdza **pliki** configu w momencie
   deployu, nie działający proces; ręczna edycja `.env`, restart workera z innym ustawieniem
   albo po prostu nieuruchomienie skryptu otwierają lukę z powrotem, równie cicho.
   (C) per-job budżet w kontrakcie §4 — **właściwy docelowy lek** (usuwa sprzężenie zamiast je
   monitorować i odblokowuje ograniczenie „`WORKER_*` jest globalny dla workera" z §8), ale to
   zmiana kontraktu godzinę po pierwszym ruchu produkcyjnym. Zostaje jako następny krok, gdy
   pojawi się drugi konsument o innym profilu latencji.
22. **Bezpieczeństwo kosztowe polla: kto pilnuje, żeby nie zapłacić dwa razy?**
   **ROZSTRZYGNIĘTE (2026-07-22) — `Job.ttl_s`: worker ODMAWIA joba po terminie,
   zamiast producenta zgadującego kolejkę.**
   **Dlaczego #21 nie wystarczyło (review to udowodnił, nie wydedukował):**
   (a) `retry_budget_seconds` **nie jest** ograniczeniem czasu zajętości workera —
   `processing.py` czeka jeszcze w `RateLimiter.acquire` przed **każdą** próbą, a tego
   nie da się ograniczyć statycznie: zależy od stanu kubełka, czyli od wszystkich innych
   jobów (repro: limit 60/min → następny job śpi ~60 s). Poprzedni docstring twierdził, że
   to prawdziwy ceiling — **to było fałszywe** i jest teraz zapisane jako przetestowany fakt
   (`test_retry_budget_deliberately_excludes_the_rate_limit_wait`).
   (b) Producent liczył `concurrency × per_job`, ale `concurrency` to limit **jednego
   procesu**. hate-mod ma **dwa** procesy sięgające `classify()` (uvicorn + cron
   `drain_queue`), każdy z własnym semaforem, a na busie mogą stać joby innych producentów.
   Realnie: 2 × 4 = 8 równoległych → 8 × 60,5 = 484 s przy „bezpiecznym" czekaniu 280 s.
   **Żaden producent nie ograniczy własnego czekania z lokalnej informacji**, bo głębokość
   kolejki jest globalna.
   **Rozwiązanie odwraca kierunek:** zamiast przewidywać kolejkę, producent deklaruje termin
   (`ttl_s`, względem `submitted_at`, §4), a worker sprawdza go przed każdą próbą i **nie
   dzwoni do providera** po terminie. Wtedy zły budżet czekania kosztuje **ponowne
   zakolejkowanie, a nie drugą płatność** — bo pracy porzuconej nikt już nie opłaca.
   Resztkowa ekspozycja: job, który wygasa **w trakcie** lotu, ograniczona jedną próbą,
   a nie głębokością kolejki.
   **Status #21:** publikacja polityki **zostaje**, ale zdegradowana — pole nazywa się teraz
   uczciwie `retry_budget_s`, a po stronie konsumenta check jest **doradczy** (warunek
   konieczny, nie wystarczający) i służy widoczności operacyjnej. Bezpieczeństwo kosztowe
   daje #22, nie #21.
   **Uwaga produkcyjna:** ekspozycja była **realna na prodzie** między 2026-07-21 (włączenie
   busa) a wdrożeniem tego PR-a: `LLMBUS_TIMEOUT_SECONDS=280` przy 8 możliwych równoległych
   jobach. Skala mała (~$0,001 za dotknięty komentarz, tylko przy zbiegu burstu z cronem),
   ale realna — nie zamiatać.
20. **Dostawa werdyktu do pilota: callback czy poll?** **ROZSTRZYGNIĘTE (2026-07-21) — POLL
   (`await_result`), nie callback; potwierdzone przez usera.** Bus wspiera oba (§14 #7) i
   callback jest po stronie workera gotowy wraz z HMAC (§14 #19) — pilot go po prostu **nie
   używa**. Powód nie jest estetyczny; wyszedł z przeglądu realnego kodu hate-moda
   (`notes/hate-mod-integration-facts-2026-07-21.md`, cytaty `plik:linia`):
   **(a) Callback gubiłby komentarze po cichu.** Wiersz `PendingComment` powstaje **wyłącznie**
   w gałęziach awaryjnych `_enqueue_pending` (`poller.py:406/442/488/508`). Na żywej ścieżce
   webhooka komentarz dochodzący do `classify()` **nie ma żadnego wiersza**. Gdyby `classify()`
   był submitem z callbackiem, zgubiony callback = brak wiersza, brak tokenu dedup (insert
   `ClassifiedComment` `poller.py:528` nigdy nie biegnie), nic do zdrenowania — komentarz znika
   bez śladu. Naprawa wymagałaby tworzenia wiersza **przed** submitem na obu ścieżkach, czyli
   zmiany zachowania prodowej moderacji.
   **(b) Kolizja z leasem.** `CLAIM_LEASE = 5 min` (`poller.py:582`), bez heartbeatu, bez
   odnawiania, nigdy nieprzedłużany (`poller.py:607-610`; jedyni pisarze `claimed_at` to
   `poller.py:615` i `poller.py:220`). Najgorszy przypadek llmbusa to ~5 min
   (`WORKER_MAX_ATTEMPTS=4` × `WORKER_JOB_TIMEOUT_S=60` + backoff, §14 #11). Job przekraczający
   lease → cron przejmuje wiersz → **druga płatna klasyfikacja i drugi `hide`**, czyli regresja,
   którą ich commit `376699b` zamykał.
   **(c) Zgubiony callback nie bije `attempts`** (robi to tylko `_enqueue_pending`,
   `poller.py:206`), więc taki wiersz re-drive'owałby się **w nieskończoność** zamiast poddać
   po `MAX_RETRY_ATTEMPTS` (`poller.py:159/641`).
   **Co poll zachowuje bez zmian:** atomowy `db.commit()` (`poller.py:568`) spinający dedup +
   liczniki kosztu + `HiddenComment` + kasowanie wiersza pending; lease; `BoundedSemaphore(4)`
   (§14 #2); kształt ~60 testów `process_comment`. `TimeoutError` mapuje się na istniejący
   `_enqueue_pending("classifier_unavailable")` — **zero nowej semantyki awarii**.
   **Cena, świadomie zapłacona:** wątek jest trzymany przez czas rundy busa zamiast ~15 s
   (`classifier.py:142`), a `BoundedSemaphore(4)` znaczy, że cztery wolne joby wstrzymują
   klasyfikację. Dlatego `BUS_TIMEOUT_S < CLAIM_LEASE` jest **twardym** wymogiem (§8), a nie
   strojeniem. Odrzucone: **callback** (przebudowa prodowej ścieżki + dziura z (a)),
   **callback z fallbackiem na poll** (obie ścieżki i oba komplety testów — najwięcej pracy,
   uzasadnione dopiero gdy odczepienie latencji zacznie być realnie potrzebne).
   **Konsekwencja dla §3/§14 #1:** „generyczny worker" z #1 stoi bez zmian; to **wyłącznie**
   wybór sposobu dostawy dla pilota. Gdy pojawi się konsument, który naprawdę nie może czekać
   (batch news?), callback jest gotowy i nieużywany, nie do napisania.
23. **Centralny wybór modelu — kto decyduje, którym modelem leci job?** **OTWARTE
   (postawione 2026-07-23), kierunek uzgodniony z userem: polityka po stronie busa,
   klucz `(project, kind)`.**
   **Problem, realny i zmierzony:** model żyje dziś w KAŻDYM projekcie osobno. W samym
   milamberze: `parser/openai_parser.py:11` (`DEFAULT_MODEL`, env `OPENAI_MODEL`), `:261`
   (`STONE_VISION_MODEL`), `:366` (`ESTIMATION_MODEL`), `api/routers/training.py:545,591`
   (`gpt-5.4` wpisany na sztywno), `db/models/language.py:74-77` (picker usera) i
   `bot/commands/admin.py:21-34` (przełącznik admina, 14 modeli). User: „nie jestem w stanie
   pamiętać, gdzie i czego używam" — i to jest właściwa diagnoza, nie wygoda.
   **Kształt:** tabela `model_policy` w store — `(project, kind) → model, updated_at`, ten
   sam wzorzec co `worker_policy` (#21). `Job.model` staje się **opcjonalny**: `None` =
   „bus decyduje", a `BusClient.submit()` **rozwiązuje politykę w momencie submitu** i kładzie
   na drut **konkretny** model. Jawny model wygrywa (pinowanie zostaje możliwe).
   **Dlaczego rozwiązanie po stronie klienta, a nie workera:** job na topicu i wiersz w store
   zawsze niosą konkretny model, więc audyt (§11) i koszt (§6) pozostają dokładne, a walidacja
   fail-loud z #6 nadal pada **w miejscu wywołania**, nie rundę później. Rozwiązywanie w
   workerze odwróciłoby kontrakt i osłabiło #6.
   **GRANICA, której nie przekraczamy:** UI zmienia **wybór** modelu (dropdown po już
   zarejestrowanych), ale **NIE ceny**. `cost.py` trzyma datowaną historię cen w kodzie —
   to ona sprawia, że zaplanowana zmiana ceny rozwiązuje się sama i że job wycenia się stawką
   z dnia `submitted_at`. Formularz nad tym znaczy, że jedna literówka **wstecznie** psuje
   każdą liczbę na stronie, bez niczego, co by to złapało. Dodanie modelu zostaje małym PR-em
   ze **zweryfikowaną** ceną (wzór: `notes/model-pricing-openai.md`).
   **Nowe ryzyko, którego strona kosztu dotąd nie miała: to jest powierzchnia ZAPISU.**
   Read-only stroną dało się goło (tailnet = granica dostępu). Strona, która zmienia model,
   ma za sobą pieniądze: wg tabeli milambera `gpt-5.5-pro` to 30/180 za Mtok wobec 0,05/0,40
   dla `gpt-5-nano` — ~600× na wejściu. Tailnet nie jest jednoosobowy (`tailscale status`:
   `macbook-air-adam`). **Decyzja usera (2026-07-23): auth na stronie.**
   **ROZSTRZYGNIĘTE (2026-07-23, user): brak wiersza dla `(project, kind)` = TWARDY BŁĄD**
   (`ModelPolicyError` w `submit()`, przed zapisem `pending` i przed wysyłką na Iggy).
   Cicha domyślka byłaby dokładnie tym rozjazdem, który #23 likwiduje: projekt chodziłby
   na modelu, którego nikt dla niego nie wybrał — czyli znów „nie wiem, czego używam".
   **Wdrożone (PR `model-policy`):** tabela `model_policy` (`(project, kind)` jako klucz
   główny, upsert), `Job.model: str | None`, czysta `schema.resolve_model` (bramka
   mutacyjna) i `BusClient._resolve_model_name`. Tabela nie jest ufana bardziej niż
   producent — model z polityki przechodzi przez ten sam `provider_for` (#6), więc polityka
   wskazująca nieroutowalny model pada tak samo głośno i bez skutków ubocznych.
   **Dwie warstwy niezmiennika:** `submit()` rozwiązuje model *przed* zapisem, a kolumna
   `jobs.model` jest `NOT NULL` — więc job bez modelu fizycznie nie może trafić do store'a.
   Worker mimo to sprawdza (`_model_of`, `process_job`) i zwraca błędny `Result` zamiast
   rzucać: jedna zła wiadomość na topicu nie może zatrzymać pętli konsumpcji (§14 #15).
   **Seed parytetu (PR `seed-model-policy`):** `policy_seed.py` (czyste dane) +
   `llmbus-seed-policy` (`seed_cli.py`, idempotentny upsert) wgrywają wiersz per
   `(project, kind)` z modelem, na którym kind chodzi DZIŚ — żeby przepięcie na busa
   (`model=None`) nie podmieniło modelu po cichu. Seed odmawia modelu spoza rejestru (ten
   sam `provider_for`), więc niewroutowalny wiersz nie wjedzie na produkcję; dziś obejmuje
   3 kindy `instagram.*` (pilot F1) na `gpt-5-nano`. `nutrition.estimate`/`training.*`
   czekają, aż `gpt-5.2`/`gpt-5.4` trafią do rejestru.
   **UZUPEŁNIENIE (2026-07-23) — granulacja `kind` i ZDOLNOŚĆ modelu.** Przegląd milambera
   pokazał, że `(project, kind)` musi być drobniejsze, niż wyglądało, bo **jeden moduł używa
   kilku RÓŻNYCH rodzajów LLM-a**: samo `language/` to 7 wywołań chat, `ocr.py:79` (vision,
   treść z `image_url`) i Whisper (`service.py:354`). Jedno `kind="language"` musiałoby
   trzymać naraz model czatowy i `whisper-1` — czyli nie działa.
   **(a) Taksonomii NIE wymyślamy — milamber już ją ma.** Jego `record_usage()` etykietuje
   ruch per funkcja: `language.chat`, `language.assessment`, `language.plan`,
   `language.session`, `language.daily_session`, `language.drills`, `language.check`,
   `language.ocr`, `language.whisper`, `knowledge.embed`, `knowledge.recall`,
   `knowledge.whisper`, `health.scan`, `instagram.series`. To są gotowe wartości `kind`.
   Bonus, nie kosmetyczny: własne liczenie wydatku milambera (`usage/spend.py`) grupuje po
   **tych samych** etykietach, więc po przepięciu na busa kubełki się zgadzają i nie trzeba
   mapować liczb między dwoma systemami.
   **(b) Rejestr modeli dostaje wymiar ZDOLNOŚCI** (`providers/base.py::CAPABILITIES`,
   `chat` | `transcription` | `embedding`). Powód jest dokładnie taki jak w #6, tylko o
   piętro wyżej: `PROVIDERS` mówi, że `whisper-1` obsługuje OpenAI, ale **nic** nie mówi, że
   on transkrybuje, a nie rozmawia. Bez tego wymiaru dropdown pozwoliłby ustawić
   `language.whisper → gpt-5.5`, a błąd wyszedłby dopiero u providera (albo gorzej — poszedłby
   jako zwykły czat). Trzecia tabela trzymana w zamku testem, tak samo jak `PROVIDERS` wobec
   `cost.PRICING`: model nie może być routowany bez zdolności ani zdolny bez route'u.
   **Strona polityki filtruje dropdown po zdolności**, więc zła para po prostu nie jest do
   wyboru — to jest właściwa obrona, wcześniejsza niż walidacja.
   **(c) Wymuszenie przy submicie jest ODŁOŻONE do #24 i to jest uczciwa kolejność.** Żeby
   `submit()` sprawdził „zdolność modelu == typ zadania", job musi ten typ **deklarować** —
   a `Job.task` powstaje dopiero w §4 v2 (#24). Dziś więc zdolność jest **danymi dla UI**
   (filtr dropdownu), nie bramką w kodzie. Nie udawajmy, że jest inaczej: dopóki wszystkie
   zarejestrowane modele są `chat`, niezgodności nie da się nawet wyprodukować.
   **Strona polityki — WDROŻONA (PR `model-policy-page`).** `/policy` na tym samym porcie co
   ledger (8093): tabela `(project, kind) → model` z formularzem **per wiersz** (zapis jednej
   polityki nie może przepisać innej) plus formularz dodania pary.
   **Auth — decyzja usera: shared secret w `.env` (`COSTS_AUTH_SECRET`), HTTP Basic.** Trzy
   zabezpieczenia, każde przeciw konkretnej pomyłce, nie dla ozdoby:
   (a) **brak sekretu = strona WYŁĄCZONA (503), nigdy otwarta** — upgrade bez ustawienia
   klucza nie może po cichu dać komukolwiek w tailnecie możliwości przestawienia modelu
   wszystkim projektom; (b) **POST z obcego origin = 403** — przeglądarka sama dosyła
   zapamiętane poświadczenia Basic, więc samo uwierzytelnienie NIE zatrzymałoby CSRF;
   (c) **model to `<select>` po zarejestrowanych**, grupowany po capability, a formularz
   dodawania otwiera się na placeholderze — „nie dotknąłem dropdownu" nie może znaczyć
   „wybrałem model" przy ~600× rozrzutu cen; posłany model i tak jest sprawdzany po stronie
   serwera (formularz to konstrukcja klienta, a to jest endpoint zapisu).
   Basic to base64, **nie szyfrowanie** — dopuszczalne WYŁĄCZNIE dlatego, że transport
   szyfruje Tailscale, a bind nigdy nie obejmuje interfejsu publicznego
   (`validate_costs_hosts`). Jeśli którakolwiek z tych dwóch rzeczy przestanie być prawdą,
   to jest hasło jawnym tekstem na drucie.
   **Bramka mutacyjna znowu zapłaciła za siebie, i to na module bezpieczeństwa:** `b64decode`
   był wołany z `validate=True`, ale nic nie dowodziło, że to ma znaczenie — bez tego dekoder
   **po cichu wyrzuca** znaki spoza alfabetu, więc `eDpz!M2NyZXQ=` dekoduje się do tych samych
   bajtów co prawdziwe poświadczenia i **uwierzytelnia**. Znalazł to przeżywający mutant; jest
   na to test.
   **Znane ograniczenie, świadome:** strona pokazuje tylko pary **skonfigurowane**, nie te,
   których joby faktycznie używają — tabela `jobs` **nie zapisuje `kind`** (ma go tylko
   `model_policy`). Dodanie kolumny `kind` do `jobs` dałoby jedno i drugie: listę „żywe, ale
   nieskonfigurowane" oraz **koszt per feature** na stronie §11 (dziś ledger grupuje wyłącznie
   po projekcie). Naturalny następny krok, tutaj nie zrobiony.
   **Uwaga operacyjna (review Codeksa):** `COSTS_AUTH_SECRET` czytany jest **raz, przy starcie**
   i trzymany przez życie procesu — więc sama edycja `.env` **NIE odbiera dostępu**, dopóki
   `llmbus-costs` nie zostanie zrestartowany. Udokumentowane w `deploy/README.md` §6 i w
   `.env.example`; celowo nie przeładowujemy sekretu per żądanie (odczyt pliku na każdym
   requeście to gorszy kompromis niż restart przy rotacji).
   **Zapis równoległy:** osiem równoczesnych POST-ów na osobnych połączeniach SQLite commituje
   każdą parę (test Codeksa, realny store, nie mock). Równoczesny zapis tej **samej** pary to
   świadomie **last-writer-wins** — to jest ta sama semantyka co upsert, a nie przeoczenie.
   **UZUPEŁNIENIE (2026-07-24) — integracja milambera: fasada + fazy, i decyzja „kto płaci".**
   Pełny przegląd powierzchni LLM milambera: 21 wywołań, 24 `kind`
   (`analysis/milamber-llm-surface.md` + plan `analysis/milamber-bus-migration-plan.md` w repo
   milambera; wcześniejszy przegląd busa: `notes/milamber-integration-survey.md`). Ustalenia:
   **(a) Jedna wewnętrzna FASADA w milamberze, nie 15 przepięć.** Każdy moduł woła
   `complete_chat(kind, …)`; transport (`direct`|`bus`) to wewnętrzna decyzja per `kind`, która
   przełącza się `direct→bus`, gdy znika bloker. Dziś nie ma wspólnego wrappera — 21 miejsc
   samodzielnie woła OpenAI i osobno liczy koszt; fasada to realna konsolidacja (wywołanie +
   koszt + ledger + transport w jednym szwie).
   **(b) „Interaktywne" NIE jest twardym blokerem dla milambera.** milamber **niczego nie
   streamuje** (grep czysty: brak `stream=True`/SSE), więc przepięcie na busa nie traci UX
   streamingu — dokłada tylko latencję, a przy jednoosobowej skali serial-worker (§5) rzadko
   blokuje. To **zawęża** linię „milamber = non-goal": kolejność gatuje *zdolność kontraktu*
   (vision/whisper/embedding) i *kto płaci*, nie „interaktywność" sama w sobie.
   **(c) „Kto płaci" — DECYZJA: `language.*` rozlicza KLUCZE UŻYTKOWNIKA** (`KEY_SOURCE_OWN`,
   `language/usage.py:37`). Nie zostają jednak „na zawsze direct": bus dostaje wejście
   BYOK/bill-back (**#25**), więc `language.*` (i każdy przyszły konsument, którego użytkownicy
   przynoszą własne klucze) pojedzie po busie, a koszt zostanie na użytkowniku, nie na koncie
   centralnym. Fasada już ma wejścia `key_source`/`user_id` — to jest jej strona #25.

24. **Transkrypcja (Whisper) na busie — §4 przestaje być tylko-chatowe.** **OTWARTE
   (postawione 2026-07-23), user potwierdził, że tego potrzebuje** (milamber:
   `knowledge/whisper.py`, `podcast/audio_transcribe.py`).
   To **cofa** część §14 #18 / §12: tam „nie-chatowy typ to scope creep, bo żaden konsument
   go nie wymusza". Teraz konsument go wymusza, więc argument upadł — ale rachunek się nie
   zmienił, tylko druga strona zaczęła ważyć więcej.
   **(a) Audio NIE MOŻE jechać w wiadomości.** `whisper.py:38` wysyła surowe bajty, limit
   25 MB (`api/routers/language.py:115`), a `podcast/audio_transcribe.py:28` **tnie** odcinki,
   bo regularnie ten limit przekraczają. Base64 w JSON to ~33 MB **na wiadomość**, na topicu,
   który jest **trwałym logiem audytowym** (§11), na boxie z problemem OOM i bez swapu
   (runbook). Jeden podcast to dziesiątki takich wiadomości, trzymanych na zawsze.
   **Kształt:** `Job` niesie **ścieżkę**, nie bajty. Producent i worker są już współlokowani
   (§9b — dzielą plik store'a), więc worker otwiera plik sam. **Wymaga skonfigurowanego
   katalogu-korzenia** (`WORKER_AUDIO_ROOT`): ścieżka od producenta bez tego to dowolny odczyt
   pliku przez worker.
   **(b) Whisper nie jest wyceniany po tokenach.** `whisper.py:8,46`: `(duration/60) * 0.006`
   — za minutę audio. `cost.py` liczy `input_tokens × stawka + output_tokens × stawka`, a
   `Usage` nie ma pola na czas. Obie rzeczy dostają drugi wymiar (`audio_seconds`).
   Uwaga: `audio_transcribe.py:7` sam pisze, że koszt to „duration × rate, NEVER an
   LLM-estimated" — ten sam instynkt co `cost.py`, więc po spięciu będą zgodne.
   **(c) Polityka modelu (#23) nic tu nie daje** — `whisper-1` to jedyny model. Wartość
   Whispera na busie to **wyłącznie** widoczność kosztu i wspólny retry, nie wybór modelu.
   **(d) `kind` NIE służy do dispatchu.** §14 #1 mówi wprost, że worker nie zna semantyki
   `kind`; użycie go do wyboru ścieżki złamałoby tę decyzję. Dispatch idzie po `task.type`.
   **Proponowany §4 v2 (jedna zmiana kontraktu zamiast dwóch):**
   `Job.task: ChatTask | TranscriptionTask` (dyskryminowane po `type`), `Job.model: str | None`
   (#23). Embeddingi wchodzą później w tę samą unię **bez kolejnego łamania kontraktu** — są
   łatwiejsze niż Whisper (wejście tekstowe, rozliczenie tokenowe).
   **Migracja:** hate-moderator stoi na obecnym kształcie **na produkcji**. Górne `messages`
   zostaje akceptowane przez jedno wydanie (deprecated), żeby żywy konsument nie musiał
   lądować w tej samej minucie co bus. Rate-limit (`ratelimit.py`) rezerwuje dziś tokeny —
   transkrypcja nie ma wejściowych, więc dostaje rezerwację tylko-requestową.

25. **Klucz per-użytkownik / bill-back na busie (BYOK) — §4 dostaje wymiar „czyim kluczem".**
   **OTWARTE (postawione 2026-07-24), user potwierdził, że będzie tego potrzebował szybko.**
   **Problem:** część ruchu rozlicza **własny klucz końcowego użytkownika**, nie centralny
   (milamber `language.*` przez `resolve_user_client`, `KEY_SOURCE_OWN`). To dziś trzyma te
   `kind`-y **poza busem** — worker woła providera kluczem centralnym llmbusa, więc przepięcie
   przeniosłoby koszt na konto centralne. User przewiduje kolejnych konsumentów (jak
   hate-moderator, ale też instagram/language), których użytkownicy przynoszą własne klucze.
   **TWARDE ograniczenie (jak audio w #24): surowy klucz NIE jedzie w ciele `Job`.** Topic to
   trwały log audytowy (§11) na boxie bez swapu — sekret w nim wyciekłby na zawsze. `Job` niesie
   **referencję** klucza, nie klucz.
   **Proponowany kształt (kierunek do potwierdzenia):** `Job.key_ref: str | None` — `None` =
   klucz centralny (dziś); ustawione = „użyj klucza, na który wskazuje ta referencja". Worker
   rozwiązuje `key_ref` → klucz z **magazynu kluczy**, który sam czyta (model współlokacji jak
   store §9b): mała, **szyfrowana** tabela `ref → key` po stronie llmbusa (milamber ma już
   wzorzec szyfrowania per-user, `db/models/usage.py:35` + `crypto.py` — wzorzec do przejęcia,
   ale llmbus **nie sięga** do bazy milambera; granica repo zostaje). Ledger (§6/§11) zapisuje
   `key_source` (own|central) per job, żeby atrybucja kosztu była uczciwa i strona pokazała
   wydatek „własnym kluczem" osobno.
   **Konsekwencja dla rate-limitu (realna):** `ratelimit.py` jest **per-provider** (kwota
   centralna). Job na kluczu użytkownika zużywa **jego** kwotę, nie centralną — więc token-bucket
   musi być **per-klucz**, nie tylko per-provider, albo joby BYOK omijają/segmentują limiter.
   Do rozstrzygnięcia razem z kształtem.
   **Decyzje bezpieczeństwa PRZED budową:** gdzie żyją klucze i jak szyfrowane w spoczynku, kto
   może je zapisać (to powierzchnia ZAPISU, jak strona polityki #23), jak `ref` mapuje się na
   klucz, rotacja. **Dotyka non-goala „multi-tenant" (§1)** — ale wąska forma (referencja
   rozwiązywana przez workera z magazynu) mieści się bez pełnej wielodostępności.
   **Zakres na teraz (user):** 99% ruchu to jego klucz + klucz znajomego w `language`, więc
   pierwsze cięcie może być minimalne (mała szyfrowana tabela `ref→key`, `Job.key_ref`
   opcjonalny, worker go używa). To **osobny PR**, nie coś doklejonego po cichu do fasady
   milambera. Odblokowuje fazę 2 planu milambera.
