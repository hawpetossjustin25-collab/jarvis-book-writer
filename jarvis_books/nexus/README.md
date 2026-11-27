# NEXUS: Jarvis's web observation (OS1), intelligence (OS2) and tool (OS3) systems, and the NEXUS API

OS1 explores the web within set limits and saves what it finds as a persistent, searchable graph. OS2 reads the
saved pages and turns them into sourced facts, entities and relations. OS3 is the strict registry and execution
pipeline for Jarvis's tools. All three talk through the same contracts, and the NEXUS API serves them on this
computer only.

## Modules

| Module | Job |
|---|---|
| `config.py` | `CrawlConfig`: validated limits, loaded from `nexus.json` in Jarvis's home when present |
| `contracts.py` | `NexusRequest` / `NexusResponse`: versioned JSON envelope (request and correlation ids, timeouts), checked with JSON Schema |
| `urls.py` | URL normalization (one spelling per page), site/domain helpers, `NetworkGuard` (public internet addresses only, which blocks SSRF) |
| `fetch.py` | Async `httpx` fetcher: guard checked on every redirect hop, robots.txt (RFC 9309), per-host spacing and Crawl-delay, retries with backoff and Retry-After, size caps, conditional GET (ETag / Last-Modified) |
| `parse.py` | BeautifulSoup parsing: title, description, text (scripts dropped), language, canonical, robots meta / X-Robots-Tag, links with anchor text and rel |
| `dedup.py` | Exact content hash and 64-bit SimHash, with 4x16-bit bands for near-duplicate lookup |
| `store.py` | `GraphStore`: SQLite (WAL) in `nexus.db`. Holds resources, edges, crawl runs, the persistent frontier, visit history, vectors, search history and cache, metrics, FTS5 indexes, plus PageRank |
| `crawler.py` | Bounded concurrent workers over the frontier: one request per host at a time, topic-ranked links, scope rules, stop and resume, revisits with adaptive intervals, measured run reports |
| `search.py` | BM25 (FTS5, porter stemming), meaning search (Gemini embeddings, or offline hashed vectors), rank fusion with link rank, phrase / `-word` / `site:` / `type:` syntax, spelling expansion, dedup, cache and history |
| `intel.py` | OS2's offline readers: sentences, entities (names, organisations, places, dates, amounts, percents, emails), claims stated plainly by a sentence (`X was founded in 1998`, `the population of X is N`, `X's tax rate was 4.2%`, `X is a ...`), value normalization, page category and an extractive summary |
| `os2.py` | `OS2`: reads pages into `intel_*` tables (migration 2 of `nexus.db`; re-read only when a page's hash changes) and judges every (subject, fact): **verified** = 2+ different websites agree and none disagrees, **unverified** = one website, **conflicting** = websites give different values for a one-value fact (founded, population, price...), **missing** = nothing found. Names appearing together are relations labelled **inference**. Operations: `analyze`, `brief`, `facts`, `entity`, `contradictions`, `status` |
| `os1.py` | `OS1`: the single entry point. `handle(request)` validates each operation's payload: `search`, `crawl`, `revisit`, `resource`, `status`, `history`, `embed`, `improve`, `rsi` |
| `rsi.py` | OS1 self-improvement. Search: the index writes its own exam (each page's title, and its rarest words, should find that page), split by URL hash into a tuning half and a sealed half. Challenger `RankWeights` (BM25 column weights, word/meaning fusion, link rank, title boost) must beat the champion on both halves without losing recall or getting 1.5x slower. Each round first re-checks the champion against the settings it replaced and rolls back if it regressed. Crawl: concurrency is tried on real crawls and kept only for 10%+ more pages a minute without more failures. Per-host delay and robots.txt are never tuned. History is in `rsi_versions` (migration 4): champion, retired, rejected, trial, rolled_back |
| `os3.py` | `ToolSpec` (id `jarvis.<name>@<version>`, strict JSON Schema arguments, output schema, permission level, timeout, retries, availability), `ToolRegistry` (unique names, recorded in `os3_tools`, keyword discovery) and `OS3`: validate, check the caller's role, check availability, run with a timeout, retry lookups only, verify the result, audit to `os3_executions` with secrets redacted. A call only counts as `success` when the tool returned a valid result; errors are `failed`, gated tools are `waiting_for_approval`. `plan` picks a tool and arguments with the quick tool brain without running it. `registry_from_toolbox` registers every Jarvis tool to run through `ToolBox.call`, so the yes/no gate still asks |
| `api.py` | `NexusHub` routes contract requests to OS1/OS2/OS3 with each request's `timeout_ms`, checks every reply against the contract, publishes request/response events and keeps per-system health. `NexusServer` serves it on `127.0.0.1:8770` (local host names only): `POST /v1/request`, `GET /v1/events` (Server-Sent Events), `GET /v1/ws` (WebSocket), `GET /v1/health`. Bearer tokens with roles (`reader`: look only; `operator`: also safe tools and crawls; `owner`: everything, through the gate), stored as SHA-256 hashes in `nexus_api.json`. Jarvis's own owner token is in `nexus_api.key`. Rate limit per token. `NexusClient` retries timeouts, busy systems, rate limits and dropped connections with backoff |

## Safety

- Only public http(s) hosts are fetched. Every redirect is re-checked, and addresses that carry credentials are refused.
- robots.txt is obeyed. A robots.txt that is erroring or unreachable blocks the host for up to an hour.
- No logins, paywall or CAPTCHA bypass, and no form posts. Only GET requests are made.
- Page text is stored and returned as data. The Jarvis tools label it untrusted, and nothing in a page can call a tool.
- All limits are enforced in code: pages, depth, queue size, bytes per page, links per page, concurrency and per-host delay.

## Jarvis tools

`web_index_explore`, `web_index_continue` and `web_index_refresh` run as background jobs (level *safe*).
`web_index_search`, `web_index_page` and `web_index_status` are *read*-level tools. A crawl job ends with OS2
reading the new pages. `web_index_brief`, `web_index_facts` and `web_index_conflicts` (*read*) return OS2's
sourced, labelled facts; Jarvis is told never to present an unverified fact as certain. `web_index_improve`
(*safe*) runs a self-improvement round or undoes the last kept change, and every crawl job ends with one. The code is in
`jarvis_books/assistant/nexus_tools.py`.

## Tests

The tests are `tests/test_nexus_core.py`, `tests/test_nexus_os1.py`, `tests/test_nexus_os2.py`,
`tests/test_nexus_os3.py`, `tests/test_nexus_api.py`, `tests/test_nexus_rsi.py` and `tests/test_nexus_tools.py`. They make real
`httpx` requests to an in-process fake web (`tests/nexus_site.py`), so they run offline.

## Search API v1 (Deep Doc 2)

Read-only routes on the NEXUS API (`127.0.0.1:8770`), each sent as a validated OS1 request through the hub, so
they share its contract checks, timeouts, events, bearer tokens (any role), local-host check and rate limit:

| Route | Answer |
|---|---|
| `GET /api/v1/search?q=...` | ranked hits with `document_id`, snippet, source (`source_id`, domain, link rank), published date, author, language and freshness (first seen, last checked, last changed); `page`/`per_page` (1-200, 1-50) and `next_page` |
| `GET /api/v1/documents/{document_id}` | one saved page: metadata, text preview, links in and out, recent visits |
| `GET /api/v1/sources/{source_id}` | one site (e.g. `example.com`): page counts, failures, duplicates, HTTPS share, languages, newest date, top pages |
| `GET /api/v1/search/status` | index size, dated pages, measured search speed (median, p95), cache and empty-result shares, ranking weights |
| `GET /api/v1/crawl/status` | pages by status, queue, revisits due, visits and failures in the last week, last 5 crawls |

Search filters: `site`, `type`, `lang` (`en` also matches `en-us`), `after`/`before` (`YYYY-MM-DD`; undated pages
are left out), `author` (part of the name), `cache=false`. Bad input gets a 400 with
`{"error": {"code": "invalid_request", "message": ...}}`; unknown ids 404, writes 405, no token 401, too fast 429.

The parser reads publication dates and authors from JSON-LD, meta tags (`article:published_time`, `author`,
Dublin Core, citation tags), `<time>` and bylines, and feed dates from their newest entry. Impossible dates (before
1991 or in the future) are dropped. They are stored by migration 5 (`published`, `author`, `meta`). Ranking adds a
freshness signal (halving every 180 days, zero for undated pages) with two weights, `freshness` and
`fresh_intent` (used when the query asks for recent things: "latest", "news", this or last year). Both are
`RankWeights`, so the self-improvement rounds tune them on sealed questions and keep them only when measured better.

## Collective (Deep Doc 2, phase 1)

Computers you own share work with Jarvis. Everything is in `collective.db` (SQLite, migrations in
`collective.py`), so a restarted Jarvis carries on where it stopped.

- `resources.py`: `ResourceProvider` (`detect_cpu/memory/gpu/npu/storage/network`, battery, temperature, CPU load)
  reports each device at four levels: detected, working in the OS, usable by an AI runtime (CUDA, DirectML, ONNX NPU
  providers, Ollama), and available under the owner's `ContributionLimits` (CPU %, memory, GPU/NPU/storage on or off,
  hours, bandwidth, battery, plugged-in only, temperature, max tasks). Limits are checked before every task.
- `collective.py`: `Coordinator`. One-time join codes (hashed, 15 minutes), short-lived worker credentials (hashed,
  refresh, revoke), heartbeats, a durable task queue with states created/queued/assigned/accepted/running/completed/
  failed/cancelled/expired, idempotency keys, priorities, deadlines, retries with attempt history, leases that send
  work from a vanished worker back to the queue, output-schema checks, and an event log. `Scheduler` filters out
  workers that can't run a task (runtime, memory, model, hours, battery, heat, local-only, deadline) and ranks the rest
  from measured history: speed, reliability, load, memory, network and cost. Every placement is saved with its
  scores and the reasons each worker was turned down (`decisions`).
- `worker.py`: `Worker` runtime: heartbeat, limits, claim, validate input, run with timeout and cancellation, deliver
  the measured result. Workloads: `llm.generate` (Ollama), `search.query` (OS1), `embed.text`, `data.digest`.
  Distribution is task-level only; splitting one model across computers is not enabled.
- `collective_os.py`: the `collective` door on the NEXUS contract and API. Readers may only look; joining codes,
  limits and worker status are owner-only; workers authenticate every call with `worker_token`. This computer joins
  its own Collective at startup (setting `collective`).

Jarvis tools: `collective_status`, `collective_task` (read), `collective_run`, `collective_limits`,
`collective_worker` (Change, need a yes) and `collective_join_code` (System).

## Model Hub and DeepRAM (Deep Doc 2, phase 2)

- `deepram.py`: `DeepRAM.plan` adds a model's real weights (file size), its context cache (layers x key/value heads
  x head size x tokens, read from the model's own metadata), runtime overhead and scratch space, and keeps a safety
  margin (1.5 GB or 10% of RAM). It refuses a load that would not fit, finds the longest context that does, and never
  counts another computer's memory as local RAM.
- `modelhub.py`: `ModelHub` (`models.db`) records every installed model from Ollama (digest checksum, parameters,
  compression, context, license, capabilities), checks disk space and memory from the registry manifest before a
  download, lets Ollama resume and verify the download, runs a first-answer test and a speed benchmark, and routes a
  request to the best model that fits right now (quality or speed, with the reason each other model was skipped).
- Jarvis tools: `model_hub`, `model_plan`, `model_route` (read) and `model_install` (Outside world, needs a yes).

## Release Center (platform-wide improvement and four-month releases)

- `releases.py`: `ReleaseCenter` (`releases.db`). The running version is the commit the installer writes to
  `app\version.json` (or the git checkout). Major cycles of about four months (1.0, 1.1, 1.2, 2.0); improvements go
  proposed -> building -> testing -> tested -> approved -> released, with author and approver. "Tested" needs a
  passing full test run and no comparison measuring worse; only Justin approves, never his own change; staged and
  stable releases need him. A release is "packaged" until the running commit is seen, then "deployed". Urgent and
  security fixes skip the cycle as patch releases. Public notes leave out keys, paths and addresses. Rollbacks
  are recorded with their reason. The journal and experiment results can't be edited or deleted (SQLite triggers),
  and a new idea like a rejected one is refused until it says what is different.
- Studio feeds it at start: the running commit, every self-upgrade (with its test result) and every search
  self-improvement round.
- Jarvis tools: `release_center` (read), `release_propose` (Change) and `release_decide` (approve, reject, defer,
  move, open a cycle, record a rollback; always asks Justin, even with Full control on).
