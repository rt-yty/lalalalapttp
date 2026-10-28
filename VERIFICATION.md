# Проверка прототипа — 27 октября 2026

## Behavioral audit — 2026-10-28

- Full step-by-step evidence and current limitations: [audit report](output/agent-audit-2026-10-28/REPORT.md).
- Real Luna checks:63 dialogue turns/126 model calls;25 suggestion contexts/32 context runs,19 service turns including 15 generated-chip submissions;8 browser turns. State campaign:24 sequences×25 steps=600 invariant checks. Three live Tutu searches and selections passed without booking; no Yandex calls in this audit.
- Fixed chat routing, repeated known-slot questions, unsupported-capability execution, incompatible suggestions, stale plans/confirmations, invalid provider snapshots, mixed factual answers and malformed Unicode input. One bounded strict-schema repair was added for invalid model responses; original model failure cause remains unconfirmed. Exact browser comparison retry passed and preserved the 67920 RUB plan.
- Main regression run after core fixes:917 passed. The subsequent shared-workspace run overlapped another chat's backend/identity/bus implementation:897 passed/24 failed. Its frozen evidence is in `shared-workspace-junit.xml` and `shared-workspace-failures.json`; this is not a clean final integration result. Later focused dialogue/suggestions/UI tests:173 passed. State/clarification tests:83 passed/1 failed; all 600 state checks passed, failure is a legacy HTTP fixture assuming `local-web` after cookie-based identity was introduced.
- Main localhost:8765 process and user sessions were left untouched during concurrent development. QA localhost:8766 was stopped. Geographic disambiguation and a successful live concert-schedule repeat remain open. Full backend/new frontend and concurrently added buses were not certified by this audit.

- `uv run pytest -q`: **43 passed**.
- `uv run travel-agent eval --suite core`: **9 passed, 0 failed**, архивные данные и тестовый парсер без сети.
- `uv run travel-agent demo --model-backend codex`: полный сценарий с настоящим Codex прошёл.
- Дополнительный проход через настоящую русскую консоль и Codex: `/пример` → `1` → `Оформить` → `Подтверждаю` → `/выход`. В отдельной временной SQLite-базе появился ровно один demo-заказ; ожидающее подтверждение очищено. Реальных покупок нет.
- Контрольная сумма: **67 920 ₽**, включая исторический авиатарф 35 218 ₽ и проживание 32 702 ₽. Данные Туту записаны 26.10.2026.
- `uv build`: исходный архив и wheel собраны. В wheel включены четыре навыка и очищенная fixture. Сырой архив экспорта, временные ссылки оформления, `.env` и базы поездок не включены в сборку исходников.

Реальный сетевой вызов Туту не проверен: автоматическая проверка разрешений отклонила передачу тестового маршрута и дат внешнему сервису. Адаптер проверялся на записанных ответах через имитацию HTTP-транспорта. Hugging Face не проверен живым вызовом; основное подключение модели — Codex.

## Браузерный интерфейс

- Добавлен локальный интерфейс HTML/CSS/JavaScript и FastAPI API поверх того же AgentService.
- Полный набор: **62 теста прошли**, включая 19 тестов web API, повторов и восстановления.
- В браузере с настоящим Codex проверены: пример запроса → варианты → выбор → обновление страницы с сохранением плана → кнопка оформления → подтверждение → демонстрационный заказ.
- Реальные бронирования и оплата не выполнялись. Адрес запуска: http://127.0.0.1:8765.

## Контекстные саджесты

- Отдельная нода `suggest_next_messages` выполняется после поиска или обработки действия и добавляет до трёх подсказок в `TurnResult.suggestions`.
- Полный набор: **75 тестов прошли**, включая 10 проверок саджестов, повторов, восстановления старой истории и скрытия устаревших подсказок.
- В браузере проверены восстановление текущей переписки после обновления и подстановка текста по нажатию на саджест. Сообщение само не отправляется, поездка не изменяется.
- Прототип формирует саджесты по правилам из состояния поездки без второго вызова модели. Неподтверждённые предложения и команды подтверждения заказа в подсказки не добавляются.

## Исправление качества саджестов — контекст разговора

- Нода `suggest_next_messages` теперь вызывает Codex отдельно и получает недавнюю переписку, точный последний вопрос, текущий ответ и проверенное состояние поездки.
- Реплики сохраняются между ходами и перезапусками; старая веб-переписка подхватывается без изменения ранее сохранённых квитанций. Новая история ограничена 24 сообщениями / 16 000 символами.
- Готовые подсказки проходят проверку формата: шаблоны с многоточиями, пустые значения, дубли и команды оформления отбрасываются. Таймаут подсказок не отменяет основной ответ.
- Полный набор: **111 тестов прошли**, включая точный диалог со скриншота, сбои генерации, восстановление, сравнение предложений и объяснение отсутствия совпадений.
- Живой вызов Codex по диалогу «хочу на концерт моргена» → «я один город и дата любые» → «Из какого города поедете?» успешно завершился за 12,2 секунды. Подсказки: «Из Москвы», «Из Санкт-Петербурга», «Из Казани». Это был модельный ответ, без переключения на запасные правила.
- Поиск концертов и чтение внешних афиш остаются за пределами прототипа; подсказки не обещают эти возможности.

## Яндекс Search API и живой режим Туту

- Добавлен Yandex Search API v2, отдельная нода `web_search`, ответ по найденным источникам и карточки ссылок. Ключ вводится в локальном UI и остаётся только в памяти сервера.
- Проверки: **193 существующих/адаптерных теста + 8 интеграционных тестов = 201 прошедший тест**. HTTP Яндекса в тестах заменён имитацией транспорта; платных запросов без пользовательского ключа не было.
- Реальный Codex по прежнему разговору о концерте и сообщению «Повтори поиск в интернете» выбрал `intent=web_search`, сформировал запрос об афише Моргенштерна после 27 октября 2026 года и не задал дополнительный вопрос вместо поиска.
- В браузере подтверждены запуск live-режима, перенос истории и пожеланий из replay без старых вариантов/подтверждений, показ формы «Ключ API / ID каталога».
- MCP Туту проверен по сети через tools/list: 17 инструментов, ожидаемые схемы поиска авиа/отелей доступны, поиска концертов нет. Текущие цены отдельным запросом в этом изменении не проверялись.
- Живой запрос к Яндексу остаётся пользовательским следующим шагом после ввода ключа; до этого не заявляется проверенным.

## Yandex connection diagnostics

- Full suite: **240 tests passed**; JavaScript syntax and package build passed.
- Covered HTTP 401/403, safe reason classification, delayed auth error bodies, field limits, preservation of the previous adapter after invalid input, and secret redaction in validation responses.
- Authorization diagnostics retain only an HTTP status and an allowlisted reason. Raw responses, credentials and account identifiers are not persisted or returned.
- Browser check: a Yandex access failure is separate from an empty travel result. Saving a key does not claim that access was verified, and an old key's failure does not determine the new key's status.
- The user's live query was denied. The old implementation discarded the exact HTTP status; a successful live search remains unverified. Restarting requires re-entering credentials for a new diagnostic query.

- Live follow-up (2026-10-27): after the user re-entered the credentials, the UI repeated the Morgenshtern schedule query. Yandex returned HTTP 401 / unauthorized; the job completed with a partial result and zero sources. The new diagnostic was visible in the chat. Successful authentication is still required; the HTTP status alone does not establish whether the value, expiry or scope is responsible. No successful live search is claimed.

- Further 401 checks (2026-10-27): code inspection confirms the raw SecretStr value is sent using Authorization: Api-Key; no environment proxy variables were present in the diagnostic process. A request with a deliberately synthetic invalid key reached the official endpoint and returned Yandex JSON UNAUTHENTICATED. Official AI Studio troubleshooting notes that Unknown api key can reflect the key value or scope. The user credential was not inspected, exported or used by this synthetic check; its scope and expiry remain unverified.

## Persistent Yandex setup script

- Added interactive setup with hidden input, atomic owner-only .env (0600), preservation of other configuration, live loopback configuration without restarting, and a credential-free report at data/yandex-connection-check.json. The user explicitly requested local persistence.
- Full suite: 307 passed; shell syntax and package build passed. All credential storage and HTTP tests use synthetic values, temporary folders and mocked transports. No real credentials were saved by the agent.
- The setup check recognizes canonical Yandex RPC error wrappers for unknown and expired keys, while retaining only fixed reason codes. A successful live search still requires the user to run setup and pass the provider check.

- Direct saved-credential checks (2026-10-27): a minimal official web-search request and a generative-search request both returned HTTP401 / code16 / API key not found or invalid. Key type and the visible masked suffix match the user screenshot; the hidden middle was not inspected or reconstructed. Scope yc.search-api.execute is visible in the user screenshot. Exact account-side cause remains unknown.
- Canonical provider error now maps to key_unknown without asserting an input mistake. 310 tests and package build passed. The web server was restarted using the user-authorized .env; no re-entry was needed. A live UI repeat reproduced the same401 and showed the updated diagnostic.

- Further isolation: the documented service-account WebSearch request without folderId also returned the same401. The independent system curl client, with its startup config and proxy variables disabled and the secret supplied over stdin (not argv), reproduced the same provider error. The setup report records both checks; root cause of credential rejection is still not established.

## Confirmed live Yandex success - 2026-10-27

- After the user updated the saved credentials, setup at 19:20 Moscow time reported ok=true, local_app=applied and five search results. No credentials were displayed.
- A real repeat of the Morgenshtern query in the existing web conversation completed as research_ready with five sources and no adapter error. Browser verification confirmed the assistant answer, source cards and next-message suggestion were visible.
- The answer correctly notes that years and ticket availability are not established by the returned snippets; full source pages were not fetched. Flights, hotels and event ticket availability were not verified by this web-search check.
- Successful live Yandex access is now confirmed. Earlier failure notes remain as historical diagnostics, not the current connection status.


## Local Whisper voice input — 2026-10-27

- Added microphone start/stop/cancel, a recording timer, Russian local recognition, and an editable transcript appended to the existing draft. Audio does not submit a turn automatically. Optional read-aloud uses a local Russian browser voice, is off by default, and reads only newly completed replies.
- Downloaded the multilingual whisper.cpp small model from the official model repository: 487601967 bytes, SHA256 `1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b`. The model is excluded from source/wheel packages.
- Audio is bounded to 8 MiB and 60 seconds. Decoding and inference run locally with restricted formats/protocols and an environment without API credentials. Temporary audio is removed on completion, error, timeout, and cancellation. The HTTP route requires the existing local-origin/CSRF checks and leaves sessions/jobs unchanged.
- Full Python suite: **365 passed** (including 25 local Whisper adapter tests and 30 voice HTTP tests). Tests caught and verified a fix for disconnect cancellation under Starlette middleware. JavaScript syntax and package build passed. Eight mocked browser-controller scenarios covered transcript/draft preservation, cancellation, stale responses,403 refresh, denied microphone, size/timeout limits and local-only read-aloud.
- Real audio smoke: local Russian system speech → WebM/Opus → running HTTP endpoint → ffmpeg → Whisper returned HTTP200 in0.96s: “Хочу поехать из Москвы в Сочи на неделю в ноябре, нас двое взрослых, бюджет 100 тысяч рублей.” Real WebM/Ogg/MP4/WAV decoding was checked. One earlier recognition spelled Ереван as Ириван; editable review before sending remains intentional.
- Restarted the server in Codex/live mode; saved Yandex configuration remains loaded. Browser verification confirmed an enabled microphone and an available local Russian read-aloud voice. The user's actual microphone was not activated or permission-tested by the agent.


## Bounded page research and useful concert answers - 2026-10-27

- Reproduced the screenshot: one Yandex query and a snippet-only summary stopped without reading the linked tour schedule. The older response incorrectly treated insufficient snippets as the end of research.
- Added a public HTML/JSON-LD reader, up to two extra Yandex queries, up to four page reads, and a second planning pass when a read-first plan yields no answer. Queries and page work share the original turn deadline. Private/loopback DNS and redirects, ambient credentials, cookies, decompression bombs, and arbitrary model-provided URLs are rejected. Every read target originates in Yandex results.
- Summaries now use the linked year/tour/date context from page text. The model receives at most 40000 serialized context characters; raw pages are not stored in sessions, checkpoints or browser responses. Cards show only cited sources and whether their pages were read. Suggestions offer concrete events from the answer.
- Full suite: **484 passed**. JavaScript syntax and package build passed. Regression coverage includes empty initial search, read-first/old-page/refine behavior, source deduplication, cancellation, per-turn bounds, model context size and source provenance.
- Real direct page checks read ReadyEvents and Showman, preserving tour2026 and the October/November dates. A first live UI retry exposed an additional read-first dead end; fixed and regression-tested before reporting completion.
- Final live UI retry at20:43 Moscow used three Yandex queries and attempted four page reads (three succeeded). Codex returned four2026 options: Haifa28October, Vilnius31October, Bratislava2November and Cologne3November; it disclosed the20:00/20:30 time conflict for Haifa. The browser visibly showed the answer, ReadyEvents/RusEvents source cards, and event-specific suggestions. Ticket availability, fares, hotels and bookings were not claimed or checked.


## Explain empty travel search - 2026-10-27

- Reproduced the screenshot from saved live session11243003-b217-46df-8b07-bd96121c7d0b: Moscow-Haifa28-31October2026, one adult, aviation0/full exact result,20 hotel listings over two pages, detailed tariffs at three hotels. Earlier normalized search produced four lodging rates, not four hotels. The old package combiner had no flight/hotel pairs and returned a generic message; the question handler repeated that message verbatim.
- Added durable, bounded search_diagnostics for missing components, missing usable fares, rejected constraints, invalid prices, provider errors and limited coverage. Changes to the trip clear old diagnostics. Questions use saved facts without re-searching or guessing budget/date constraints. Legacy coverage is used only when its query matches the current trip; the latest old generic reply is repaired for display without mutating durable receipts.
- Suggestions no longer ask to discover an already explained reason or invent dates, budgets and unsupported airport/route changes. Error and missing-component headlines are distinct. Source cards remain hidden as the user requested.
- Full suite: **544 passed**; JavaScript syntax passed. Integration checks cover no new provider calls for why, restart persistence, errors vs empty results, stale evidence, requirements preservation and legacy sessions.
- Fresh read-only Tutu MCP probe confirmed city names are accepted and Moscow-Haifa resolves MOW-HFA:0 offers, exact total0, no upstream_note or reason/message/warning fields. The same dates and party to Tel Aviv resolve MOW-TLV with10 offers on page1 and further pages. No checkout or passenger registration was invoked. This alternative was not substituted into the user's Haifa trip, and transfer details remain unchecked.
- Restarted the local server. A live GET of the screenshot session returned missing_flights, the exact route/date explanation and hotel coverage; the repeated reason suggestion disappeared. A separate active user conversation was left running.


## GPT-6.1 Terra medium - 2026-10-27

- The prior unpinned Codex invocation resolved to gpt-6.1-sol (observed CLI header). Persisted CODEX_MODEL=gpt-6.1-terra and CODEX_REASONING_EFFORT=medium in the owner-only local configuration; credentials retained. The shared gateway now explicitly forwards reasoning effort for interpretation, suggestions, planning and research answers.
- Focused regression suite: 39 passed. Real Terra medium checks passed: trip interpretation 8.54s, suggestions 10.20s, research planning 8.32s, answer over supplied test evidence 6.34s. These are individual samples, not latency guarantees. No live travel search or booking was performed for this configuration check.


## One clarification at a time - 2026-10-27

- Interpreter and trip skills now ask one parameter per turn and retain all volunteered fields. The fallback asks one missing parameter; recognized compound slot questions are reduced before saving the answer and generating suggestions. Ambiguity, child-age and unsupported-route blockers remain intact.
- Pending legacy compound questions use the same normalized question in the UI and next interpretation context, without rewriting immutable turn receipts. Suggestions for dates reject adult-only answers; returning from a concert may be suggested while the agent is explicitly asking for a return date.
- Real Terra medium replay: Moscow answer produced only “Когда хотите вылететь?” (9.18s); 28October answer produced only “Когда хотите вернуться?” (6.13s); all three generated chips answered the return-date question. Focused regression cases also cover the persisted HTTP view and next-turn context.


## Concise empty results and other-city follow-up - 2026-10-27

- Customer replies omit diagnostic counts and coverage while keeping structured evidence. Empty flight results ask about another city; retry is offered only for retryable provider errors. The unrelated Yandex repeat button is hidden outside a web-search error. Saved current replies and next-turn context are compacted consistently without rewriting receipts.
- Suggestions allow evidence-backed other concerts and reject same-condition retries or unverified availability claims. Choosing a different concert can reset old travel dates before applying explicitly supplied new dates, preserving origin, party and budget.
- Full suite: 592 passed; JavaScript syntax passed. Real Terra medium checks used entirely synthetic dialogue: other-city request became web_search, choosing another concert became replan with reset_dates and a single departure question; generated suggestions named the other evidenced concerts. No real user dialogue was sent for this verification.


## Overnight flight and hotel date alignment - 2026-10-27

- Fixed arrival-day equality against requested departure: verify outbound departure and return departure against user dates, and hotel check-in against airport-local arrival. Hotel search/details now use the earliest valid local arrival date from the bounded flight results; packages pair only flights matching those hotel stay dates. Other arrival dates are not silently mixed into the same hotel price. Cards show actual check-in/check-out.
- Removed raw boolean date rejection wording and offer a new selection without asking to relax an internal constraint. Synthetic end-to-end adapter test confirms departure7May, arrival8May, return10May uses hotel nights8-10May and remains selectable; wrong hotel dates, missing/naive times, and timezone-local dates are checked. No live provider request was made for this fix.


## Independent components, rail and Luna — 2026-10-28

- Added independent flight/train/hotel cards and selection, transport alternatives, hotel-only and transport-only requests, partial provider results, per-product errors, compatibility checks and partial plans. Replanning can preserve the selected transport or hotel. A changed trip invalidates old cards before clarification; undo restores partial as well as complete plans and clears newer offers. Questions and comparisons use the relevant product rather than an unrelated selected component. Hotel cards show different properties before additional tariffs of the same property.
- Live rail uses two one-way searches for a direct return trip, with checked fare prices multiplied by the number of adults and sufficient reported seats in each direction. Current scope is 1–6 adults, no children, no seat selection or connection planning. Bounded coverage remains explicit. Flight and hotel handling stays independent of rail errors.
- Event research uses Yandex for the schedule; answers and suggestions do not discuss event ticket inventory. Confirmed event dates remain distinct from travel dates. Future-event lists require an evidenced year, and the fallback no longer repeats raw snippets or rejected dates. No artist/venue catalogue was added.
- Suggestions distinguish departure, return and duration questions, refer to existing cards by product, account for partial selection and remain editable drafts. Actual browser verification on a separate temporary replay server covered separate lists, suggestion-to-draft behavior, independent selection, the 67920 RUB total and restoration after reload. An unavailable checkout action is hidden. The temporary server was stopped after testing.
- **Final full suite: 736 passed in 5.57s.** JavaScript syntax passed. `examples/integration.py` passed with temporary SQLite: exact retries, separate selection, restart/restoration, tariff questions, removal and reselection. The demo-order variant was also checked earlier in this change set. No real orders were created.
- Four synthetic interpretation tasks per configuration all passed with real Codex authentication and no model tools: Luna none median 7.785s, Luna low 7.35s, Terra medium 7.925s. This small noisy sample does not establish a global speed ranking. Persisted `gpt-6-luna` with `low` reasoning in the local configuration and example configuration. With Luna, a separate replay dialogue passed initial search, individual selections, breakfast question plus hotel replacement while keeping the flight, and baggage question; turns took approximately 5–14s.
- Read-only live Luna/Tutu smoke checks succeeded: Moscow–Saint Petersburg rail, 10–17 November 2026, two adults, six cards in 21.38s; hotel-only Kazan, 10–12 November, one adult, eight tariff cards in 20.72s. A later regression ensures distinct hotels precede multiple tariffs of the same property. Live searches are bounded samples, not validation of every route.
- An initial live Weeknd research completed in 42.82s but included dates without a clear year. After tightening the prompt, real Luna checks over synthetic evidence passed future/past/unknown-year separation, year inherited from tour sections and no-future-results cases (6–9s). The final live Yandex repeat returned `search_rate_limited` in 12.05s, correctly marked retryable; the revised final answer could not be rechecked on live search results. Further Yandex retries were stopped. The empty-citation negative answer is checked through rendering and saved dialogue, not just raw model output.
- Restarted the main localhost:8765 service in Codex/live mode. Bootstrap reports ready and Yandex configured. There were no active jobs before restart; the digest and count of persisted sessions were unchanged afterwards. Browser inspection confirmed the existing conversation still opens. Historical messages and legacy plans remain preserved; fresh searches use the new component contract.
- `AGENT_BACKEND_CONTRACT.md` and the integration example document agent → backend → frontend responsibilities, IDs, state versions, partial plans and progress events. Full backend, authentication, Postgres/Redis deployment and a cross-trip preference profile remain outside this change. SQLite still persists current trips; it is not a permanent personalization profile.
