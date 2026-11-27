"""Search API v1 (/api/v1/...) and the page metadata behind it: dates, authors, languages and freshness."""
import json
import sqlite3
import time

import httpx
import pytest

from jarvis_books.nexus.api import NexusHub, NexusServer, RateLimit, TokenBook, search_api, systems_for
from jarvis_books.nexus.os1 import OS1
from jarvis_books.nexus.os2 import OS2
from jarvis_books.nexus.os3 import OS3, ToolRegistry
from jarvis_books.nexus.parse import parse_date, parse_feed, parse_html
from jarvis_books.nexus.search import freshness, wants_recent
from jarvis_books.nexus.store import GraphStore
from nexus_site import FAST, FakeWeb, page

OLD = "https://old.example/solar"
NEW = "https://news.example/solar"
UNDATED = "https://plain.example/solar"
GERMAN = "https://de.example/solar"


def _day(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def _ld(**fields) -> str:
    return f'<script type="application/ld+json">{json.dumps(fields)}</script>'


NEW_DATE = _day(10)
SITE = {
    OLD: page("Solar panels guide", "Solar panels turn sunlight into electricity for homes. " * 8,
              head=_ld(**{"@context": "https://schema.org", "@type": "Article", "datePublished": "2015-03-02",
                          "author": {"@type": "Person", "name": "Ann Lee"}})),
    NEW: page("Solar panels news", "Solar panels turn sunlight into electricity for homes. " * 8,
              head=f"<meta property='article:published_time' content='{NEW_DATE}'>"
                   "<meta name='author' content='Bob Ray'><meta property='og:site_name' content='Sun News'>"),
    UNDATED: page("Solar panels basics", "Solar panels turn sunlight into electricity for homes. " * 8),
    GERMAN: page("Solarmodule", "Solar panels Solarmodule wandeln Sonnenlicht in Strom um. " * 8).replace(
        "lang='en'", "lang='de-DE'"),
}


@pytest.fixture
def nexus(tmp_path):
    os1 = OS1(tmp_path, FAST, fetcher_factory=FakeWeb(dict(SITE)).factory())
    os3 = OS3(ToolRegistry(os1.store.db))
    hub = NexusHub(systems_for(os1, OS2(os1), os3))
    tokens = TokenBook(tmp_path)
    server = NexusServer(hub, tokens, port=0)
    assert server.start()
    from jarvis_books.nexus.contracts import NexusRequest
    done = hub.route(NexusRequest("jarvis", "os1", "crawl", {"seeds": list(SITE), "max_pages": 10}))
    assert done.ok and done.results[0]["pages_fetched"] == 4
    yield server, tokens, hub, os1
    server.stop()
    hub.close()
    os3.close()
    os1.close()


def get(server, token, path):
    return httpx.get(f"http://127.0.0.1:{server.port}{path}", timeout=30, trust_env=False,
                     headers={"Authorization": f"Bearer {token}"} if token else {})


# ---- metadata extraction

def test_dates_are_parsed_and_impossible_ones_dropped():
    assert parse_date("2024-05-06") == pytest.approx(1714953600)
    assert parse_date("2024-05-06T10:00:00Z") == pytest.approx(1714989600)
    assert parse_date("Mon, 06 May 2024 10:00:00 GMT") == pytest.approx(1714989600)
    assert parse_date("2024-05-06T10:00:00+02:00") == pytest.approx(1714982400)
    for bad in ("", "yesterday", "1970-01-01", "2024-13-40", "0000-00-00"):
        assert parse_date(bad) is None
    assert parse_date(_day(-30)) is None  # a month in the future is a wrong clock, not a date


def test_html_metadata_from_json_ld_meta_tags_and_time():
    ld = parse_html(SITE[OLD], OLD)
    assert ld.author == "Ann Lee" and ld.published == pytest.approx(parse_date("2015-03-02"))
    assert ld.meta["type"] == "Article" and ld.lang == "en"
    meta = parse_html(SITE[NEW], NEW)
    assert meta.author == "Bob Ray" and meta.meta["site_name"] == "Sun News"
    assert meta.published == pytest.approx(parse_date(NEW_DATE))
    graph = parse_html("<html><head>" + _ld(**{"@graph": [{"@type": "WebSite", "name": "X"}, {
        "@type": "NewsArticle", "datePublished": "2023-01-02", "dateModified": "2023-02-03",
        "author": [{"name": "A One"}, {"name": "B Two"}], "keywords": ["sun", "power"]}]}) +
        "<meta property='og:locale' content='pt_BR'></head><body><p>x</p></body></html>", "https://g.example/")
    assert graph.author == "A One, B Two" and graph.meta["type"] == "NewsArticle"
    assert graph.meta["keywords"] == ["sun", "power"] and graph.meta["modified"] == parse_date("2023-02-03")
    assert graph.lang == "pt-br" and graph.published == parse_date("2023-01-02")
    timed = parse_html("<html><body><article><p class='byline'>By Cara Diaz</p>"
                       "<time datetime='2022-07-08'>July 8</time><p>text</p></article></body></html>", "https://t.example/")
    assert timed.published == parse_date("2022-07-08") and timed.author == "Cara Diaz"
    broken = parse_html("<html><head><script type='application/ld+json'>{not json</script>"
                        "<meta name='author' content='https://x.example/me'></head><body>hi</body></html>",
                        "https://b.example/")
    assert broken.published is None and broken.author == "" and broken.meta == {}


def test_feed_date_is_its_newest_entry():
    feed = ("<rss><channel><title>Sun</title><language>en-US</language><managingEditor>Ed Itor</managingEditor>"
            "<item><title>One</title><pubDate>Mon, 06 May 2024 10:00:00 GMT</pubDate></item>"
            "<item><title>Two</title><pubDate>Tue, 07 May 2024 10:00:00 GMT</pubDate></item></channel></rss>")
    parsed = parse_feed(feed, "https://f.example/rss")
    assert parsed.published == parse_date("Tue, 07 May 2024 10:00:00 GMT")
    assert parsed.lang == "en-us" and parsed.author == "Ed Itor"


def test_an_old_index_gets_the_new_columns_and_keeps_its_pages(tmp_path):
    path = tmp_path / "nexus.db"
    store = GraphStore(path)
    rid = store.ensure("https://keep.example/")
    store.db.execute("UPDATE resources SET title='kept' WHERE id=?", (rid,))
    store.close()
    con = sqlite3.connect(path)  # pretend this index was made before migration 5
    con.executescript("DROP INDEX idx_res_published; DROP INDEX idx_res_lang;"
                      "ALTER TABLE resources DROP COLUMN published; ALTER TABLE resources DROP COLUMN author;"
                      "ALTER TABLE resources DROP COLUMN meta; PRAGMA user_version=4;")
    con.commit()
    con.close()
    store = GraphStore(path)
    node = store.resource(rid)
    assert node["title"] == "kept" and node["published"] is None and node["author"] == "" and node["meta"] == {}
    store.close()


# ---- ranking signals

def test_freshness_and_recent_intent():
    now = time.time()
    assert freshness(None, now) == 0.0 and freshness(now, now) == 1.0
    assert freshness(now - 180 * 86400, now) == pytest.approx(0.5)
    assert wants_recent("latest solar news") and wants_recent(f"solar {time.gmtime().tm_year}")
    assert not wants_recent("solar panels") and not wants_recent("solar 1999")


def test_crawled_pages_keep_their_metadata_and_search_filters(nexus):
    _, _, _, os1 = nexus
    engine = os1.search_engine
    node = os1.store.by_url(OLD)
    assert node["author"] == "Ann Lee" and node["meta"]["type"] == "Article"
    everything = engine.search("solar panels", 10, use_cache=False)["results"]
    assert {r["url"] for r in everything} == set(SITE)
    english = {r["url"] for r in engine.search("solar panels", 10, use_cache=False, lang="en")["results"]}
    assert english == {OLD, NEW, UNDATED}
    assert [r["url"] for r in engine.search("solar panels", 10, use_cache=False, lang="de")["results"]] == [GERMAN]
    since = time.time() - 365 * 86400
    assert [r["url"] for r in engine.search("solar panels", 10, use_cache=False, after=since)["results"]] == [NEW]
    assert [r["url"] for r in engine.search("solar panels", 10, use_cache=False,
                                            before=since)["results"]] == [OLD]
    assert [r["author"] for r in engine.search("solar", 10, use_cache=False, author="ann")["results"]] == ["Ann Lee"]
    news = engine.search("latest solar panels news", 10, use_cache=False)
    assert news["recent_intent"] and news["results"][0]["url"] == NEW


def test_pages_of_results_do_not_overlap(nexus):
    engine = nexus[3].search_engine
    first = engine.search("solar panels", 2, use_cache=False)
    second = engine.search("solar panels", 2, use_cache=False, offset=2)
    assert first["more"] and not second["more"]
    urls = [r["url"] for r in first["results"] + second["results"]]
    assert len(urls) == 4 and set(urls) == set(SITE)
    cached = engine.search("solar panels", 2, offset=2)
    assert [r["url"] for r in cached["results"]] == [r["url"] for r in second["results"]]


# ---- the HTTP routes

def test_search_route_with_attribution_paging_and_filters(nexus):
    server, tokens, _, _ = nexus
    reader = tokens.issue("reader", "test")
    r = get(server, reader, "/api/v1/search?q=solar+panels&per_page=3")
    body = r.json()
    assert r.status_code == 200 and body["api"] == "v1" and body["page"] == 1 and body["next_page"] == 2
    assert len(body["results"]) == 3 and body["matched"] == 4 and body["request_id"]
    hit = next(h for h in body["results"] if h["url"] == OLD)
    assert hit["source"]["source_id"] == "old.example" and hit["author"] == "Ann Lee"
    assert hit["published"] == "2015-03-02T00:00:00Z" and hit["language"] == "en"
    assert isinstance(hit["document_id"], int) and hit["freshness"]["last_checked"].endswith("Z")
    last = get(server, reader, "/api/v1/search?q=solar+panels&per_page=3&page=2").json()
    assert len(last["results"]) == 1 and last["next_page"] is None
    assert {h["url"] for h in body["results"] + last["results"]} == set(SITE)
    filtered = get(server, reader, "/api/v1/search?q=solar&lang=de").json()
    assert [h["url"] for h in filtered["results"]] == [GERMAN]
    dated = get(server, reader, "/api/v1/search?q=solar&before=2016-01-01&after=2015-03-02").json()
    assert [h["url"] for h in dated["results"]] == [OLD]
    site = get(server, reader, "/api/v1/search?q=solar&site=news.example").json()
    assert [h["url"] for h in site["results"]] == [NEW]


@pytest.mark.parametrize("query, message", [
    ("", "'q' (what to search for) is required"),
    ("q=" + "x" * 501, "longer than 500"),
    ("q=a&page=0", "between 1 and 200"),
    ("q=a&per_page=51", "between 1 and 50"),
    ("q=a&per_page=ten", "whole number"),
    ("q=a&after=2024-02-30", "not a real date"),
    ("q=a&after=May+5", "date like"),
    ("q=a&after=2024-02-01&before=2024-01-01", "not be later"),
    ("q=a&site=bad_host!", "host name"),
    ("q=a&q=b", "give 'q' once"),
    ("q=a&sort=new", "unknown parameter: sort"),
    ("q=a&cache=maybe", "true or false"),
])
def test_search_route_rejects_bad_input_with_structured_errors(nexus, query, message):
    server, tokens, _, _ = nexus
    r = get(server, tokens.owner_token(), "/api/v1/search?" + query)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "invalid_request" and message in err["message"]


def test_document_and_source_routes(nexus):
    server, tokens, _, os1 = nexus
    token = tokens.issue("reader", "test")
    rid = os1.store.by_url(NEW)["id"]
    doc = get(server, token, f"/api/v1/documents/{rid}").json()["document"]
    assert doc["document_id"] == rid and doc["url"] == NEW and doc["author"] == "Bob Ray"
    assert doc["source_id"] == "news.example" and doc["meta"]["site_name"] == "Sun News"
    assert doc["published"].endswith("Z") and "Solar panels" in doc["text_preview"] and "text" not in doc
    missing = get(server, token, "/api/v1/documents/999999")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert missing.json()["request_id"]
    assert get(server, token, "/api/v1/documents/abc").status_code == 400
    assert get(server, token, "/api/v1/documents/0").status_code == 400
    src = get(server, token, "/api/v1/sources/old.example").json()["source"]
    assert src["source_id"] == "old.example" and src["pages"] == 1 and src["searchable"] == 1
    assert src["https_share"] == 1.0 and src["languages"] == {"en": 1} and src["dated_pages"] == 1
    assert src["newest_published"] == "2015-03-02T00:00:00Z" and src["top_pages"][0]["url"] == OLD
    assert get(server, token, "/api/v1/sources/nowhere.example").status_code == 404
    assert get(server, token, "/api/v1/sources/bad_host!").status_code == 400


def test_status_routes_report_real_counts(nexus):
    server, tokens, _, _ = nexus
    token = tokens.issue("reader", "test")
    get(server, token, "/api/v1/search?q=solar")
    search = get(server, token, "/api/v1/search/status").json()["status"]
    assert search["searchable"] == 4 and search["dated_pages"] == 2 and search["published_last_year"] == 1
    assert search["searches"] >= 1 and search["median_ms"] is not None and "freshness" in search["ranking"]
    crawl = get(server, token, "/api/v1/crawl/status").json()["status"]
    assert crawl["by_status"]["fetched"] == 4 and crawl["crawls"] == 1 and crawl["crawl_running"] is False
    assert crawl["recent_runs"][0]["status"] == "done" and crawl["visits_last_week"] == 4
    assert crawl["failed_visits_last_week"] == 0
    assert get(server, token, "/api/v1/crawl/status?x=1").status_code == 400


def test_routes_need_a_token_are_local_only_read_only_and_rate_limited(nexus, tmp_path):
    server, tokens, hub, _ = nexus
    assert get(server, None, "/api/v1/search?q=solar").status_code == 401
    assert get(server, "nx_wrong", "/api/v1/search/status").status_code == 401
    token = tokens.owner_token()
    assert get(server, token, "/api/v1/nothing").status_code == 404
    posted = httpx.post(f"http://127.0.0.1:{server.port}/api/v1/search?q=solar", content="{}",
                        headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=30)
    assert posted.status_code == 405 and posted.json()["error"]["code"] == "method_not_allowed"
    foreign = httpx.get(f"http://127.0.0.1:{server.port}/api/v1/search?q=solar", trust_env=False, timeout=30,
                        headers={"Authorization": f"Bearer {token}", "Host": "evil.example"})
    assert foreign.status_code == 403
    tight = NexusServer(hub, tokens, port=0, limit=RateLimit(per_minute=60, burst=2))
    assert tight.start()
    try:
        codes = [get(tight, token, "/api/v1/search/status").status_code for _ in range(4)]
        assert codes[:2] == [200, 200] and codes[-1] == 429
    finally:
        tight.stop()


def test_search_api_is_a_validated_os1_request(nexus):
    _, _, hub, _ = nexus
    events = hub.subscribe()
    status, body = search_api(hub, "GET", "/api/v1/search?q=solar", "reader")
    assert status == 200 and body["results"]
    seen = [events.get_nowait() for _ in range(events.qsize())]
    assert any(e["event"] == "request" and e["operation"] == "search" and e["destination"] == "os1" for e in seen)
    assert search_api(hub, "DELETE", "/api/v1/search?q=solar", "owner")[0] == 405
