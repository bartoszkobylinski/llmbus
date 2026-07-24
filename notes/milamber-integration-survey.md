# milamber_assistant → llmbus — przegląd realnego kodu przed integracją

> **AKTUALIZACJA (2026-07-24).** Rozszerzone i zdecydowane w repo milambera:
> `analysis/milamber-llm-surface.md` (routing per `kind`) + `analysis/milamber-bus-migration-plan.md`
> (fasada + fazy). Decyzje: `ARCHITECTURE.md` §14 #23 (uzupełnienie) i #25 (BYOK/bill-back).
> Kluczowa zmiana wniosku: milamber **niczego nie streamuje**, więc „interaktywne" nie jest
> twardym blokerem — patrz plan.

**Data:** 2026-07-23. **Cel:** ustalić, ile z ruchu LLM milambera da się przepiąć na busa,
żeby strona kosztu (§11) pokazywała *cały* wydatek na tokeny, a nie tylko
`hate-moderator`. Wszystkie cytaty to `plik:linia` w
`~/Programming/Python/milamber_assistant`, sprawdzone 2026-07-23.

**Wniosek w jednym zdaniu:** to **nie** jest podpięcie kabla — z 18 wywołań chat tylko
**mniejszość jest dziś kwalifikowalna**, a trzy niezależne blokery (klucze per-user, vision,
nieznane modele) trzeba rozstrzygnąć **osobno**, bo każdy dotyka innej części kontraktu §4.

---

## 1. Powierzchnia wywołań — co w ogóle jest chatem

| API | Gdzie | Na busa? |
|---|---|---|
| `chat.completions.create` | 18 miejsc: `language/` ×8, `parser/` ×5, `api/` ×2, `knowledge/` ×1, `instagram/` ×1, `bot/` ×1 | **częściowo** — patrz blokery |
| `embeddings.create` | `knowledge/embeddings.py:25` (`text-embedding-3-small`) | **NIE** |
| `audio.transcriptions.create` | `knowledge/whisper.py:44`, `podcast/audio_transcribe.py:119` (`whisper-1`) | **NIE** |
| `responses.create`, `moderations`, `audio.speech`, `images.*` | brak wywołań | — |

`Job` (§4) niesie `messages: list[Message]` — to kontrakt **chat-only**. Embeddingi i
Whisper nie są chat-completion i nie mieszczą się w nim, dokładnie tak jak `moderate()`
w §14 #18. Rozszerzanie kontraktu o typ nie-chatowy to realny scope creep wobec §1 i
**nie wymusza go żaden konsument** — obie ścieżki zostają inline. Konsekwencja dla §11
jest szczera i trzeba ją zapisać: **koszt embeddingów i Whispera nigdy nie pojawi się na
stronie**, dopóki ta decyzja stoi.

## 2. Bloker A — klucze API **per user** (najpoważniejszy)

`language/usage.py:38-68` `resolve_user_client()`: użytkownik może mieć **własny** klucz
OpenAI (`db/models/user.py:45-54`, `openai_api_key_encrypted`), a wtedy
`OpenAI(api_key=decrypt_text(encrypted))` → `KEY_SOURCE_OWN`; brak klucza → `None` →
wspólny `get_client()` → `KEY_SOURCE_SHARED`.

llmbus ma **jeden** `OPENAI_API_KEY` w `Config` (§10), a `Job` (§4) **nie ma pola na
klucz**. Więc każde wywołanie, które ma pójść na kluczu *użytkownika*, dziś przez busa
przejść nie może. To dotyczy **8 z 18** wywołań (cały moduł `language/`) — czyli największej
pojedynczej grupy.

**To nie jest brakująca funkcja, tylko pytanie projektowe**, i trzeba je rozstrzygnąć jako
§14, nie zaimplementować po cichu: topic `llm-jobs` jest **logiem audytowym wszystkich
promptów** (§11), a store trzyma wiersz per job. Wkładanie tam cudzych kluczy API
(albo nawet ich identyfikatorów) to zmiana modelu bezpieczeństwa całego busa, nie detal
w kontrakcie. Wariant „worker trzyma mapę user→klucz" przenosi sekrety użytkowników do
procesu, który ich dziś nie widzi.

**Uwaga rachunkowa, niezależna od implementacji:** wydatek na kluczu użytkownika **nie jest
Twoim wydatkiem**. Nawet gdyby technicznie dało się to przepchnąć przez busa, wrzucenie
tego do tej samej sumy co koszt własny **zafałszowałoby** stronę §11. Jeśli te wywołania
kiedyś wejdą, muszą być na niej rozdzielone (`key_source` jest już w kodzie milambera,
`language/usage.py`), a nie zsumowane.

## 3. Bloker B — vision (`image_url`) nie mieści się w `Message`

`schema.py`: `Message.content: str` + `model_config = ConfigDict(extra="forbid")`. Treść
multimodalna (lista części z `image_url`) **odpada walidacją**, nie „działa gorzej".

Miejsca budujące treść obrazkową: `language/ocr.py`, `parser/label_scan.py`,
`parser/health_prompt.py:120` (PDF → base64), `parser/openai_parser.py:207`
(`parse_label_image`), `:300` (`classify_stone_image`).

To **zmiana kontraktu §4** (`content: str | list[ContentPart]`), więc zgodnie z CLAUDE.md
wymaga aktualizacji ARCHITECTURE.md w tym samym PR, przejścia przez oba adaptery
(OpenAI i Anthropic mapują treść multimodalną **inaczej**) i pozycji w §14. Dokładnie ta
sama pułapka co §14 #10 za pierwszym razem: pole „neutralne", które u każdego providera
znaczy co innego.

## 4. Bloker C — modele: to jest §14 #6, powtórzone

Modele w kodzie milambera (18 różnych): `gpt-5.4`, `gpt-5.5`, `gpt-5.2`, `gpt-5.1`,
`gpt-5.5-pro`, `gpt-5.4-nano`, `gpt-5.4-mini`, `gpt-5-nano`, `gpt-5-mini`, `gpt-5`,
`gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-nano`, `gpt-4.1-mini`, `gpt-4-turbo`,
`whisper-1`, `text-embedding-3-small`.

llmbus zna **7**: `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5.4-mini`, `claude-opus-4-8`,
`claude-haiku-4-5`, `claude-sonnet-5` (`cost.py::PRICING`, `providers/base.py::PROVIDERS`).

Czyli **~11 modeli chatowych milambera nie istnieje w rejestrze busa**. Od PR
`feat/model-registry-fail-loud` `BusClient.submit` waliduje model **przed** wysyłką, więc
takie zlecenie pada w miejscu wywołania — głośno, ale pada. **To jest dosłownie lekcja
§14 #6**: „model jest w configu konsumenta" ≠ „bus go obsłuży"; to dwie osobne tabele.
Każdy dokładany model potrzebuje **zweryfikowanej** ceny z datą (wzór: `notes/model-pricing-openai.md`),
a nie przepisanej z `parser/openai_parser.py:17` — tamta tabela jest drugim źródłem, nie dowodem.

## 5. Rzeczy mniejsze, ale nie darmowe

- **`json_object` → `json_schema`.** `instagram/series_classify.py:104` używa
  `response_format={"type": "json_object"}`. Kontrakt §4 **celowo** nie ma `json_object`
  (§14 #10 — koncept tylko-OpenAI). Każde takie miejsce przechodzi tę samą migrację co
  hate-mod: schemat + `additionalProperties: false`. Dla konsumenta ściśle lepiej
  (odpowiedź walidowana), ale to praca per call-site.
- **Sync → async.** Wszystko jest na synchronicznym `OpenAI` (nie `AsyncOpenAI`).
  Obowiązuje §14 #17: most mieszka po stronie milambera, bus zostaje async-only.
  milamber ma FastAPI (`lifespan` → trwały `BusClient`, jak w §14 #17 ścieżka web)
  **oraz** bota Discord i skrypty (`scripts/prebuild_language_sessions.py`) — czyli
  **więcej niż dwa** procesy sięgające po LLM. Każdy potrzebuje własnej krawędzi
  `asyncio.run`, i każdy dokłada się do globalnej głębokości kolejki (patrz §14 #22 —
  `ttl_s` jest tu obowiązkowy, nie opcjonalny).
- **Podwójna księgowość.** milamber ma **własny** cennik (`parser/openai_parser.py:17
  MODEL_PRICING`) i własne liczenie wydatku (`usage/spend.py`). Po przepięciu na busa
  koszt liczy `cost.py`. Jeśli oba zostaną, ten sam job policzy się dwa razy w dwóch
  miejscach i **rozjadą się** przy zmianie cennika. Trzeba świadomie wybrać źródło prawdy.
- **Współlokacja store'a.** Poll (`await_result`) czyta ten sam plik SQLite, który pisze
  worker (§9b). Na VPS-ie oba procesy są na jednym boxie, więc to działa — ale milamber
  musi mieć prawo **zapisu** do `~/Projects/llmbus/data/llmbus.db` (`insert_pending`), nie
  tylko odczytu. To uprawnienia do pliku innego projektu; do sprawdzenia przy wdrożeniu.

## 6. Co jest kwalifikowalne DZIŚ

Wywołanie kwalifikuje się, jeśli spełnia **wszystkie cztery**: chat • tekst (bez `image_url`)
• wspólny klucz (bez `resolve_user_client`) • model w rejestrze busa.

Najbliższy kandydat i naturalny pierwszy ruch: **`instagram/series_classify.py`** —
`get_client()` (klucz wspólny, `:87`), brak `image_url`, `model=DEFAULT_MODEL` = `gpt-5-nano`
(**jest** w rejestrze), zadanie klasyfikacyjne. To ten sam kształt co pilot hate-mod, więc
ścieżka jest przetarta: jedyna praca poza podmianą wywołania to migracja
`json_object` → `json_schema` (`:104`).

Reszta `parser/` wymaga przejścia miejsce po miejscu — część jest obrazkowa (odpada na
blokerze B), część używa modeli spoza rejestru (bloker C).

## 7. Rekomendacja

**Nie migrować milambera „w całości".** Kolejność, każda pozycja osobno i mierzalna:

1. **Pilot: `instagram/series_classify.py`** na busa (`json_object` → `json_schema`,
   `ttl_s`, poll). Potwierdza ścieżkę drugiego producenta i **od razu** dokłada drugi
   projekt na stronę §11 — czyli daje widoczny wynik za najmniejszą pracę.
2. **Rejestr modeli** — dołożyć te modele chatowe, które faktycznie chcemy routować,
   z cenami zweryfikowanymi wobec cennika OpenAI (nie przepisanymi z milambera).
   Bez tego kroku każdy dalszy call-site pada na `UnknownModelError`.
3. **§14: vision w kontrakcie §4** — decyzja, nie implementacja. Odblokowuje `parser/`.
4. **§14: klucze per-user** — decyzja **projektowa i bezpieczeństwa**, najcięższa;
   odblokowuje `language/` (8 wywołań). Domyślna odpowiedź „nie wchodzi na busa"
   jest całkowicie broniona: to nie jest Twój wydatek.
5. **Embeddingi/Whisper** — zostają inline (§14 #18). Zapisać w §11, że strona ich nie widzi.

**Do rozstrzygnięcia przed 1.:** czy `usage/spend.py` + `MODEL_PRICING` zostają obok
`cost.py`, czy bus przejmuje księgowanie dla tego, co przez niego leci. Nie da się mieć
obu bez rozjazdu.
