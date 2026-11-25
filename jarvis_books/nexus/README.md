# NEXUS OS1: Jarvis's web observation system

OS1 explores the web within set limits and saves what it finds as a persistent, searchable graph. OS2
(intelligence) and OS3 (tool calling) will talk to it through the same contracts. They are not built yet.

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
| `os1.py` | `OS1`: the single entry point. `handle(request)` validates each operation's payload: `search`, `crawl`, `revisit`, `resource`, `status`, `history`, `embed` |

## Safety

- Only public http(s) hosts are fetched. Every redirect is re-checked, and addresses that carry credentials are refused.
- robots.txt is obeyed. A robots.txt that is erroring or unreachable blocks the host for up to an hour.
- No logins, paywall or CAPTCHA bypass, and no form posts. Only GET requests are made.
- Page text is stored and returned as data. The Jarvis tools label it untrusted, and nothing in a page can call a tool.
- All limits are enforced in code: pages, depth, queue size, bytes per page, links per page, concurrency and per-host delay.

## Jarvis tools

`web_index_explore`, `web_index_continue` and `web_index_refresh` run as background jobs (level *safe*).
`web_index_search`, `web_index_page` and `web_index_status` are *read*-level tools. The code is in
`jarvis_books/assistant/nexus_tools.py`.

## Tests

The tests are `tests/test_nexus_core.py`, `tests/test_nexus_os1.py` and `tests/test_nexus_tools.py`. They make real
`httpx` requests to an in-process fake web (`tests/nexus_site.py`), so they run offline.
