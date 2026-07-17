# §8 pilot (hate-moderator) — przegląd realnego kodu przed integracją

**Data:** 2026-07-17. **Cel:** sprawdzić, czy §8 („w `poller.py::process_comment` zamiast
inline `classifier.classify(...)` → `llmbus.submit(..., callback="/internal/classified")`")
da się zbudować tak, jak jest napisane.

**Wynik: NIE, nie w tej formie.** §8 opisuje zmianę jednej linii; realnie zderza się z
sześcioma rzeczami, z czego trzy wymagają decyzji architektonicznej, a jedna **zaprzecza już
podjętej decyzji §14 #10**. Ten dokument jest wsadem do tych decyzji, nie planem wdrożenia.

**Repo:** `~/Programming/Python/hate-moderator` (POZA tym repo). Cytaty `plik:linia` odnoszą
się do niego, chyba że ścieżka zaczyna się od `src/llmbus/`.

**Proweniencja:** przegląd zrobił subagent (Explore, read-only); **wszystkie sześć punktów
poniżej zweryfikowałem następnie sam**, czytając cytowane linie. Rzeczy niezweryfikowane są
oznaczone `(niezweryfikowane)`.

---

## 1. Powierzchnia integracji (stan faktyczny)

- **Framework:** FastAPI (`pyproject.toml:8`).
- **Wejście:** webhook Meta, HMAC `X-Hub-Signature-256` (`routes/webhook.py:42-45,100-106`)
  → `BackgroundTasks` → **synchroniczny** `poller.handle_webhook_comment`
  (`webhook.py:88`, `poller.py:913`) → threadpool.
- **`process_comment`:** `poller.py:232` — **`def`, nie `async def`** (zweryfikowane).
- **Współbieżność:** `threading.BoundedSemaphore` (`poller.py:36-53`,
  `settings.max_concurrent_classifications=4`, `config.py:45`) + rolling-24h cap per user
  (`config.py:41`, egzekwowany `poller.py:395-415`).
- **Klasyfikator** (`app/moderation/classifier.py`) — **dwa osobne wywołania OpenAI**:
  - `moderate()` (`:240`) → `moderations.create`, model `omni-moderation-latest` (`:237`).
  - `classify()` (`:146`) → `chat.completions.create`, `settings.llm_model`, bez
    `temperature`, `max_completion_tokens=128`, zwraca
    `(Category, confidence, prompt_tokens, completion_tokens)`.
  - Klient: modułowy, leniwy `OpenAI(timeout=15, max_retries=1)` (`:132-143`).
- **Dedup** po `comment_id`: `poller.py:270`. **Hide:** `:478-520`
  (`ig_client.hide_comment`, `ig_client.py:29`). **Zapis DB:** `:528-568` — **jeden commit**
  (`ClassifiedComment` + opcjonalny `HiddenComment` + usunięcie `PendingComment`).
- **Testy:** `tests/test_classifier.py`, `tests/test_poller.py` (~60 testów `process_comment`).

---

## 2. Blokery — zweryfikowane

### B1. `response_format` — WPROST zaprzecza §14 #10 ⚠ najważniejsze
`classifier.py:171` przekazuje `response_format={"type": "json_object"}`. llmbus **usunął**
to pole z kontraktu v1 (§14 #10), a `JobParams` ma `extra="forbid"`
(`src/llmbus/schema.py:69`) i tylko `temperature` + `max_tokens` (`:71-72`) — więc pola
**nie da się nawet przepchnąć**. Bez niego kontrakt JSON z `classifier.py:209` opiera się
wyłącznie na prompcie, a fallback „nieparsowalne → cicho neutral" (`:228-234`) **failuje
otwarcie** (przepuszcza komentarz).

**To jest sedno.** §14 #10 uzasadniono abstrakcyjnie („żaden provider nie mapuje się czysto"
— OpenAI chce obiektu, Anthropic `output_config.format`) i wybrano wariant A (usuń z v1).
Ale **pilot — jedyny realny konsument busa, powód jego istnienia — na tym polu stoi.**
Decyzja #10 została podjęta bez sprawdzenia potrzeb pilota. Do rozstrzygnięcia: czy #10
wraca (wariant C: typ strukturalny mapujący się per provider), czy pilot dostaje
obejście (np. `kind`-specyficzny handling po stronie workera), czy `classify` **zostaje**
w hate-mod, a na bus idzie coś innego.

### B2. `moderate()` w ogóle nie mieści się w kontrakcie `Job`
llmbus `Job` jest **chat-only**: `messages: list[Message]` (`src/llmbus/schema.py:75-88`,
zweryfikowane). Endpoint `moderations` to nie chat-completion. §8 mówi „do busa wychodzi
moderation + classify" — **moderation jest dziś niebudowalne** bez rozszerzenia §4.

⚠ **Uwaga bezpieczeństwa (z komentarza w kodzie, `classifier.py:240-248`):** `moderate()`
jest opisany jako **odporny na prompt-injection backstop** — „content classifier, not an
instruction-follower, so it CANNOT be prompt-injected". To nie jest detal wydajnościowy.
Cokolwiek zrobimy, nie wolno tego osłabić przy okazji przenosin.

### B3. Sync/async — hate-mod jest synchroniczny end-to-end
`llmbus.submit` to `async def` (`src/llmbus/client.py:204`). Każdy wywołujący
`process_comment` jest **sync**: webhook BackgroundTask i cron `app/scripts/drain_queue.py`.
llmbus v1 **z definicji nie ma sync-wrapperów** (CLAUDE.md: „Everything async end-to-end;
no sync wrappers in v1"). W całej aplikacji hate-mod są tylko 3 `async def`
(`main.py:221,278`, `routes/webhook.py:37`).

### B4. Nie ma żadnego `/internal/*` ani wewnętrznego HMAC
`grep -rn "internal" app/ --include="*.py"` → **pusto** (zweryfikowane). Jedyny HMAC to
weryfikacja webhooka Meta app-secret. §8 zakłada `/internal/classified` — trzeba
zaprojektować **od zera** uwierzytelnianie callbacku (bus → hate-mod).

### B5. Rozjazd transakcji i kolejności
Hide dzieje się **przed** skonsumowaniem tokenu dedup (`poller.py:473-477`), żeby awarie
się ponawiały. Wyniesienie `classify` na bus rozbija `db.commit()` (`:528-568`) na **dwa
procesy**. Callback musi dostać przeplumbowane: `comment_id`, `media_id`, `text`,
`text_hash`, `user_id`, `commenter_username`, `latency_ms` i liczniki tokenów. Semantyka
retry `_enqueue_pending` (`:406-451`) nie ma oczywistego miejsca w nowym układzie.

### B6. Drobne
`max_tokens` (llmbus) vs `max_completion_tokens=128` (hate-mod) — llmbus już mapuje to w
adapterze OpenAI dla GPT-5 (§14 #9), więc prawdopodobnie OK, ale do potwierdzenia dla
`settings.llm_model`. Guard `finish_reason == "length"` (`:192-207`) potrzebuje
odpowiednika w `Result` — dziś `Result` nie niesie `finish_reason` **(do sprawdzenia)**.

---

## 3. Wsad do otwartych decyzji §14

- **#2 (lokalny cap):** hate-mod ma **dwie** różne rzeczy, nie jedną:
  `BoundedSemaphore(4)` (ochrona *własnego* threadpoola/pamięci) **oraz** rolling-24h cap
  per user (*reguła produktowa*, nie rate-limit). Bus może przejąć co najwyżej to pierwsze;
  drugie to domena hate-moda i §1 mówi, że bus nie zna domeny. Sugestia do decyzji:
  semafor **zostaje** (chroni proces hate-moda, nie providera), cap per user **zostaje**
  (domena).
- **#3 (dystrybucja klienta):** oba repo są lokalne, w `~/Programming/Python/` → editable
  path install jest najprostszy; `py.typed` już jest (§14 #8), więc typy przechodzą.
- **#6 (model):** hate-mod czyta `settings.llm_model` (`config.py`) — model jest **już**
  konfigiem, nie hardcodem. Decyzja #6 to zatem wybór wartości, nie zmiana kodu.
  **(niezweryfikowane: jaka jest dziś realna wartość `llm_model` w prod `.env`)**

---

## 4. Blokery / pytania otwarte

1. **B1 vs §14 #10** — wymaga rozstrzygnięcia PRZED kodem. To jest decyzja, nie implementacja.
2. **B2** — czy `moderate()` w ogóle idzie na bus w v1? Jeśli tak, §4 musi urosnąć o typ
   nie-chatowy. Jeśli nie — §8 jest do przepisania („tylko `classify` wychodzi").
3. **B3** — czy hate-mod dostaje async seam (jaki?), czy llmbus łamie „no sync wrappers"?
4. **B4** — schemat auth dla `/internal/classified` (shared secret? HMAC? mTLS?) — nowy projekt.
5. **B5** — kto jest właścicielem stanu między submit a callback (`PendingComment`?).
6. **Niezweryfikowane:** realna wartość `settings.llm_model` na prod; czy `Result` niesie
   `finish_reason`; czy ~60 testów `process_comment` da się utrzymać przy rozjeździe na dwa procesy.
