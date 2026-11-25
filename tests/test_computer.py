"""Jarvis's hands on the computer: files with backups and undo, the terminal, and the internet (mocked)."""
import base64
import io
import json
import sys
import threading
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from jarvis_books.computer.files import Computer, diff, is_secret
from jarvis_books.computer.terminal import Terminal, assess
from jarvis_books.computer.web import Web, _repo, hf_url, page_text
from jarvis_books.errors import NotFound


@pytest.fixture
def pc(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Computer(tmp_path / "work", home=home, platform="linux")


# ---- files ----------------------------------------------------------------------------------
def test_write_backs_up_and_restores(pc):
    f = pc.home / "notes.txt"
    f.write_text("one\ntwo\n")
    out = pc.write_file("notes.txt", "one\nTWO\n")
    assert f.read_text() == "one\nTWO\n" and out["backup"] and "-two" in out["diff"] and "+TWO" in out["diff"]
    pc.restore(out["backup"], out["written"])
    assert f.read_text() == "one\ntwo\n"
    made = pc.write_file("new/deep/file.md", "# hi")
    assert made["created"] and made["backup"] is None
    pc.restore(None, made["written"])
    assert not (pc.home / "new/deep/file.md").exists()
    assert [e["action"] for e in pc.recent()] == ["write", "undo", "write", "undo"]


def test_write_retries_while_windows_holds_the_file_then_writes_in_place(pc, monkeypatch):
    import os
    f = pc.home / "held.py"
    f.write_text("old\n")
    real, calls = os.replace, []

    def busy_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr("jarvis_books.computer.files.time.sleep", lambda s: None)
    monkeypatch.setattr("jarvis_books.computer.files.os.replace", busy_once)
    assert pc.write_file("held.py", "new\n")["backup"] and f.read_text() == "new\n" and len(calls) == 2

    def always_busy(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr("jarvis_books.computer.files.os.replace", always_busy)
    out = pc.write_file("held.py", "newer\n")
    assert f.read_text() == "newer\n" and not list(pc.home.glob("*.jarvis-tmp"))
    pc.restore(out["backup"], out["written"])
    assert f.read_text() == "new\n"


def test_edit_needs_an_exact_single_match(pc):
    f = pc.home / "app.py"
    f.write_text("x = 1\ny = 1\n")
    with pytest.raises(NotFound):
        pc.edit_file("app.py", "z = 1", "z = 2")
    with pytest.raises(ValueError):
        pc.edit_file("app.py", " = 1", " = 2")
    pc.edit_file("app.py", "y = 1", "y = 2")
    assert f.read_text() == "x = 1\ny = 2\n"
    out = pc.edit_file("app.py", " = ", " == ", all_matches=True)
    assert out["replaced"] == 2


def test_delete_goes_to_backups_and_comes_back(pc):
    d = pc.home / "old"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.txt").write_text("keep me")
    out = pc.delete("old")
    assert not d.exists()
    pc.restore(out["backup"], out["deleted"])
    assert (d / "sub" / "a.txt").read_text() == "keep me"


def test_move_refuses_to_overwrite(pc):
    (pc.home / "a.txt").write_text("a")
    (pc.home / "b.txt").write_text("b")
    with pytest.raises(ValueError):
        pc.move("a.txt", "b.txt")
    (pc.home / "dest").mkdir()
    out = pc.move("a.txt", "dest")
    assert Path(out["to"]).parts[-2:] == ("dest", "a.txt")


def test_system_folders_drives_secrets_and_home_are_off_limits(tmp_path):
    win = Computer(tmp_path / "w", home=tmp_path, platform="win32")
    assert "Windows system folder" in win.protected(__import__("pathlib").PureWindowsPath("C:/Windows/System32"))
    assert win.protected(__import__("pathlib").PureWindowsPath("C:/")) == "a whole drive"
    pc = Computer(tmp_path / "w2", home=tmp_path / "h", platform="linux")
    assert "a system folder" in pc.protected(PurePosixPath("/etc/passwd"))
    assert pc.protected(PurePosixPath("/")) == "a whole drive"
    for bad in (str(tmp_path / "h" / ".ssh" / "id_rsa"), str(tmp_path / "w2" / "backups" / "x")):
        with pytest.raises(NotFound):
            pc.write_file(bad, "x")
    (tmp_path / "h").mkdir()
    with pytest.raises(NotFound):
        pc.delete(str(tmp_path / "h"))
    for secret in (".env", "id_rsa", "server.pem", "x/.aws/credentials", "gemini.key", "Chrome/Default/Login Data"):
        assert is_secret(__import__("pathlib").Path(secret)), secret
    assert not is_secret(__import__("pathlib").Path("chapter.environment.md"))
    (tmp_path / "h" / ".env").write_text("KEY=1")
    with pytest.raises(NotFound):
        pc.read_file(".env")


def test_reading_files_and_folders(pc):
    (pc.home / "b.txt").write_text("\n".join(f"line {i}" for i in range(1, 101)))
    (pc.home / "a").mkdir()
    (pc.home / "bin.dat").write_bytes(b"\x00\x01\x02")
    listing = pc.list_folder("")
    assert [e["name"] for e in listing["entries"]] == ["a", "b.txt", "bin.dat"] and listing["entries"][0]["folder"]
    part = pc.read_file("b.txt", start_line=50, max_chars=40)
    assert part["text"].startswith("50: line 50") and part["more"]
    assert pc.read_file("bin.dat")["binary"]


def test_diff_is_short():
    d = diff("\n".join(map(str, range(500))), "\n".join(f"x{i}" for i in range(500)), "f", max_lines=10)
    assert d.count("\n") <= 10 and "more diff lines" in d


# ---- terminal --------------------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["Format-Volume -DriveLetter C", "format c:", "diskpart", "rm -rf /", "rm -rf ~",
                                 "Remove-Item -Recurse -Force C:\\Windows\\System32", "reg delete HKLM\\Software\\X",
                                 "bcdedit /set", "Clear-Disk -Number 0", "del /s /q C:\\", "dd if=/dev/zero of=/dev/sda",
                                 "Set-MpPreference -DisableRealtimeMonitoring $true"])
def test_dangerous_commands_are_refused(cmd, tmp_path):
    assert assess(cmd)[0]
    with pytest.raises(ValueError):
        Terminal(tmp_path).run(cmd)


@pytest.mark.parametrize("cmd,warn", [("winget install Git.Git", "installs"), ("Restart-Computer", "restarts"),
                                      ("Start-Process powershell -Verb RunAs", "administrator"),
                                      ("Remove-Item .\\old.txt", "deletes files"),
                                      ("Invoke-WebRequest https://x.org -OutFile a", "internet")])
def test_risky_commands_carry_a_warning(cmd, warn):
    blocked, warnings = assess(cmd)
    assert blocked is None and any(warn in w for w in warnings)
    assert assess("Get-ChildItem C:\\Users") == (None, [])


@pytest.mark.skipif(sys.platform == "win32", reason="bash commands")
def test_runs_a_real_command_and_captures_everything(tmp_path):
    t = Terminal(tmp_path / "t", home=tmp_path)
    out = t.run("echo hello; echo oops >&2; exit 3")
    assert out["output"].strip() == "hello" and out["errors"].strip() == "oops" and out["exit_code"] == 3
    assert not out["ok"] and out["folder"] == str(tmp_path)
    assert json.loads((tmp_path / "t" / "terminal.jsonl").read_text().splitlines()[-1])["exit_code"] == 3
    big = t.run("python3 -c \"print('x' * 50000)\"")
    assert len(big["output"]) < 13_000 and "characters cut" in big["output"]
    with pytest.raises(ValueError):
        t.run("ls", cwd=str(tmp_path / "nope"))


@pytest.mark.skipif(sys.platform == "win32", reason="bash commands")
def test_time_limit_and_cancel_stop_the_command(tmp_path):
    t = Terminal(tmp_path / "t", home=tmp_path)
    out = t.run("sleep 30", timeout_s=1)
    assert "time limit" in out["stopped"] and out["seconds"] < 5 and not out["ok"]
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    out = t.run("sleep 30", cancel=stop)
    assert out["stopped"] == "cancelled" and out["seconds"] < 5


def test_windows_runs_powershell_without_a_window(tmp_path):
    calls = {}

    class P:
        returncode, pid = 0, 1

        def communicate(self, timeout=None):
            return "ok".encode(), b""

    def popen(argv, **kw):
        calls.update(argv=argv, **kw)
        return P()

    t = Terminal(tmp_path / "t", home=tmp_path, platform="win32", popen=popen)
    out = t.run("Get-Date")
    assert calls["argv"][0] == "powershell.exe" and "-NonInteractive" in calls["argv"]
    assert calls["argv"][-1].endswith("Get-Date") and calls["stdin"] is not None
    assert out["ok"] and out["output"] == "ok" and t.shell == "PowerShell"


# ---- internet (mocked) ----------------------------------------------------------------------------------
class Resp:
    def __init__(self, status=200, body=b"", headers=None, url="https://x.org"):
        self.status_code, self._body, self.headers, self.url = status, body, headers or {}, url
        self.raw = io.BytesIO(body)
        self.raw.read = (lambda n=-1, decode_content=True, _b=io.BytesIO(body): _b.read(n))
        self.encoding = "utf-8"

    @property
    def text(self):
        return self._body.decode()

    @property
    def content(self):
        return self._body

    def json(self):
        return json.loads(self._body)

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


class Session:
    def __init__(self, routes):
        self.routes, self.headers, self.seen = routes, {}, []

    def request(self, method, url, **kw):
        self.seen.append((method, url, kw))
        for key, resp in self.routes.items():
            if key in url:
                return resp(method, url, kw) if callable(resp) else resp
        return Resp(404)

    def get(self, url, params=None, **kw):
        self.seen.append((url, params))
        for key, resp in self.routes.items():
            if key in url:
                return resp() if callable(resp) else resp
        return Resp(404)


def web(routes):
    return Web(Session(routes), check_address=lambda u: u)


def test_reads_a_page_as_plain_text():
    page = b"<html><head><title>Hi</title><style>x{}</style></head><body><nav>menu</nav><h1>Title</h1>" \
           b"<p>First &amp; best.</p><script>evil()</script></body></html>"
    out = web({"x.org": Resp(body=page, headers={"Content-Type": "text/html"})}).read_page("https://x.org")
    assert out["title"] == "Hi" and out["text"] == "Title\n\nFirst & best." if "\n\n" in out["text"] else \
        out["text"].split() == ["Title", "First", "&", "best."]
    assert "evil" not in out["text"] and "menu" not in out["text"]


def test_local_and_home_network_addresses_are_refused():
    from jarvis_books.computer.web import _public
    for bad in ("file:///etc/passwd", "http://127.0.0.1:11434/api/tags", "http://localhost/", "ftp://x.org"):
        with pytest.raises((ValueError, NotFound)):
            _public(bad)


def test_scrapes_links_headings_tables_and_lists():
    page = (b'<html><head><title>Catalog</title></head><body>'
            b'<h1>Books</h1><ul><li>Item one</li><li>Item two</li></ul>'
            b'<table><tr><th>Name</th><th>Price</th></tr><tr><td>Desk</td><td>$9</td></tr></table>'
            b'<a href="/about">About us</a><script>evil()</script></body></html>')
    out = web({"x.org": Resp(body=page, headers={"Content-Type": "text/html"})}).scrape("https://x.org/c")
    assert out["title"] == "Catalog" and out["headings"] == ["Books"]
    assert out["tables"] == [[["Name", "Price"], ["Desk", "$9"]]]
    assert out["list_items"] == ["Item one", "Item two"]
    assert out["links"] == [{"text": "About us", "url": "https://x.org/about"}]
    assert "evil" not in out["text"]


def test_scrape_refuses_non_page_and_bad_addresses():
    w = web({"x.org": Resp(body=b"PK\x03\x04zip", headers={"Content-Type": "application/zip"})})
    assert "isn't a web page" in w.scrape("https://x.org/f.zip")["note"]
    with pytest.raises((ValueError, NotFound)):
        web({}).scrape("http://127.0.0.1/x")


def _results(*urls):
    return "".join(f'<a rel="nofollow" class="result__a" href="{u}">Title {i}</a>'
                   f'<a class="result__snippet" href="x">Snippet {i}</a>' for i, u in enumerate(urls)).encode()


def _page(url, title, words="lighthouse keepers trimmed the lamp wicks every night "):
    return Resp(body=f"<html><title>{title}</title><body><p>{words * 8}</p></body></html>".encode(),
                headers={"Content-Type": "text/html"}, url=url)


def test_research_reads_the_best_pages_and_skips_ones_it_cant_read():
    session = Session({"duckduckgo": Resp(body=_results("https://a.org/1", "https://b.org/2", "https://c.org/3",
                                                        "https://d.org/4", "https://e.org/5", "https://a.org/1#x")),
                       "a.org/1": _page("https://a.org/1", "A"), "c.org/3": _page("https://c.org/3", "C", "hi"),
                       "d.org/4": _page("https://d.org/4", "D")})
    out = Web(session, check_address=lambda u: u).research("history of lighthouses", pages=2)
    assert [(x["title"], x["url"]) for x in out["sources"]] == [("A", "https://a.org/1"), ("D", "https://d.org/4")]
    assert "lighthouse keepers" in out["sources"][0]["text"] and len(out["sources"][0]["text"]) <= 4000
    assert [x["url"] for x in out["skipped"]] == ["https://b.org/2", "https://c.org/3"]
    assert "JavaScript" in out["skipped"][1]["why"]
    assert [x["url"] for x in out["other_results"]] == ["https://e.org/5"]  # the #x duplicate isn't read twice
    assert "e.org" not in " ".join(u for u, _ in session.seen)


def test_research_says_when_nothing_could_be_read():
    out = web({"duckduckgo": Resp(body=_results("https://b.org/2"))}).research("anything")
    assert out["sources"] == [] and "other words" in out["note"] and out["skipped"][0]["url"] == "https://b.org/2"
    assert "found nothing" in web({"duckduckgo": Resp(body=b"")}).research("zzz")["note"]


def test_explore_site_follows_same_site_links_that_match_first():
    home = (b'<html><title>Shop</title><body><h1>Welcome</h1><a href="/about">About us</a>'
            b'<a href="/prices">Prices and plans</a><a href="https://other.org/x">Elsewhere</a>'
            b'<a href="/home#top">Top</a><a href="/blog">Blog</a><td>stray cell</td></body></html>')
    session = Session({"s.org/home": Resp(body=home, headers={"Content-Type": "text/html"}, url="https://s.org/home"),
                       "s.org/prices": _page("https://s.org/prices", "Prices", "the basic plan costs nine dollars a month ")})
    out = Web(session, check_address=lambda u: u).explore_site("https://s.org/home", "prices", pages=2)
    assert out["site"] == "s.org" and [x["title"] for x in out["pages"]] == ["Shop", "Prices"]
    assert out["pages"][0]["headings"] == ["Welcome"] and "nine dollars" in out["pages"][1]["text"]
    assert [x["url"] for x in out["more_links"]] == ["https://s.org/about", "https://s.org/blog"]
    assert not any("other.org" in u for u, _ in session.seen)


def test_web_search_parses_results():
    html_page = (b'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fhuggingface.co'
                 b'%2FLiquidAI&rut=1">Liquid <b>AI</b></a><a class="result__snippet" href="x">Small models</a>'
                 b'<a class="result__a" href="https://github.com/x/y">x/y</a><a class="result__snippet" href="z">repo</a>')
    out = web({"duckduckgo": Resp(body=html_page)}).search("liquid ai")
    assert out["results"][0] == {"title": "Liquid AI", "url": "https://huggingface.co/LiquidAI", "snippet": "Small models"}
    assert out["results"][1]["url"] == "https://github.com/x/y"


def test_web_search_falls_back_to_bing_when_duckduckgo_wants_a_robot_check():
    import base64
    target = base64.urlsafe_b64encode(b"https://www.liquid.ai/models").decode().rstrip("=")
    bing = (f'<ol><li class="b_algo" data-id><link rel="stylesheet" href="x.css"/><h2><a target="_blank" '
            f'href="https://www.bing.com/ck/a?!&amp;&amp;p=ab&amp;u=a1{target}&amp;ntb=1">Liquid <strong>Models</strong></a>'
            f'</h2><p class="b_lineclamp2">Small fast models.</p></li>'
            f'<li class="b_algo"><h2><a href="https://plain.org/x">Liquid models compared</a></h2></li>'
            f'<li class="b_algo"><h2><a href="https://game.org/x">Dead Space</a></h2></li></ol>').encode()
    session = Session({"duckduckgo": Resp(202, b"<form>anomaly challenge</form>"), "bing.com/search": Resp(body=bing)})
    out = Web(session, check_address=lambda u: u).search("liquid models")
    assert out["engine"] == "bing"
    assert out["results"] == [{"title": "Liquid Models", "url": "https://www.liquid.ai/models", "snippet": "Small fast models."},
                              {"title": "Liquid models compared", "url": "https://plain.org/x", "snippet": ""}]


def test_a_robot_check_rests_duckduckgo_and_says_the_engines_are_limiting():
    from jarvis_books.computer.web import ROBOT_REST_S
    session = Session({"duckduckgo": Resp(202, b"<form>anomaly challenge</form>"), "bing.com/search": Resp(body=b""),
                       "wikipedia.org/w/api.php": Resp(body=b'{"query": {"search": []}}')})
    w = Web(session, check_address=lambda u: u)
    now = [1000.0]
    w.clock = lambda: now[0]
    out = w.research("solar generator battery life")
    assert out["sources"] == [] and out["blocked"] and "limiting searches" in out["note"]
    asked = sum("duckduckgo" in u for u, _ in session.seen)
    assert w.search("solar generator prices").get("blocked")
    assert sum("duckduckgo" in u for u, _ in session.seen) == asked  # left alone while it rests
    now[0] += ROBOT_REST_S
    session.routes["duckduckgo"] = Resp(body=_results("https://b.org/2"))
    assert not w.search("solar generator prices").get("blocked")


def test_off_topic_bing_results_are_dropped_and_wikipedia_answers_last():
    junk = (b'<li class="b_algo"><h2><a href="https://game.org/dead-space">Dead Space</a></h2>'
            b'<p>A horror game.</p></li>')
    wiki = json.dumps({"query": {"search": [{"title": "Jeddah Light", "snippet": "the <b>tallest</b> lighthouse"}]}})
    session = Session({"duckduckgo": Resp(202, b"anomaly"), "bing.com/search": Resp(body=junk),
                       "wikipedia.org/w/api.php": Resp(body=wiki.encode())})
    out = Web(session, check_address=lambda u: u).search("what is the tallest lighthouse in the world")
    assert out["engine"] == "wikipedia"
    assert out["results"] == [{"title": "Jeddah Light", "url": "https://en.wikipedia.org/wiki/Jeddah_Light",
                               "snippet": "the tallest lighthouse"}]


def test_off_topic_wikipedia_pages_are_dropped_so_a_blocked_search_says_so():
    wiki = json.dumps({"query": {"search": [
        {"title": "Bicycle lighting", "snippet": "lights powered by a <b>portable</b> battery"},
        {"title": "Photovoltaic system", "snippet": "a <b>solar</b> power system for the <b>home</b>"},
        {"title": "Solar-powered generator", "snippet": "<b>portable</b> <b>solar</b> <b>generators</b> for <b>backup</b>"}]}})
    session = Session({"duckduckgo": Resp(202, b"anomaly"), "bing.com/search": Resp(body=b""),
                       "wikipedia.org/w/api.php": Resp(body=wiki.encode())})
    w = Web(session, check_address=lambda u: u)
    out = w.search("cheapest reliable portable solar generators for home backup 2024")
    assert [r["title"] for r in out["results"]] == ["Solar-powered generator"]
    session.routes["wikipedia.org/w/api.php"] = Resp(body=json.dumps({"query": {"search": [
        {"title": "Bicycle lighting", "snippet": "lights powered by a <b>portable</b> battery"}]}}).encode())
    out = w.search("cheapest reliable portable solar generators for home backup 2024")
    assert out["results"] == [] and out["blocked"]


def test_bing_results_sharing_only_a_couple_of_long_question_words_are_dropped():
    from jarvis_books.computer.web import _relevant
    q = "cheapest reliable portable solar generators for home backup 2024"
    assert not _relevant(q, {"title": "Cheap Houses for Sale", "url": "https://www.landbanksearch.com/cheap-houses",
                             "snippet": "Find a cheap home in 2024."})
    assert not _relevant(q, {"title": "GasBuddy", "url": "https://www.gasbuddy.com/home",
                             "snippet": "Find the cheapest gas near home."})
    assert _relevant(q, {"title": "Best Portable Solar Generator for Home Backup (2024)", "url": "https://r.org/best",
                         "snippet": "We tested the most reliable picks."})
    assert _relevant("solar generators", {"title": "Solar generator guide", "url": "https://s.org", "snippet": ""})


def test_requests_name_the_app_and_bing_is_strictly_safe():
    import requests
    assert Web(requests.Session()).session.headers["User-Agent"].startswith("JarvisBookWriter/")
    session = Session({"duckduckgo": Resp(body=b""), "bing.com/search": Resp(body=b"")})
    session.headers = {}
    Web(session, check_address=lambda u: u).search("lighthouses")
    assert [params for url, params in session.seen if "bing" in url][0]["adlt"] == "strict"


def test_search_with_no_internet_says_so():
    import requests

    class Down(Session):
        def get(self, url, params=None, **kw):
            raise requests.ConnectionError("no route")
    with pytest.raises(NotFound, match="internet"):
        Web(Down({}), check_address=lambda u: u).search("anything")


def test_hugging_face_search_and_model_page():
    models = json.dumps([{"id": "LiquidAI/LFM3-1.7B", "downloads": 5, "likes": 2, "pipeline_tag": "text-generation"}])
    info = json.dumps({"id": "LiquidAI/LFM3-1.7B-GGUF", "pipeline_tag": "text-generation", "downloads": 9,
                       "cardData": {"license": "other"}, "siblings": [{"rfilename": "LFM3-1.7B-Q4_K_M.gguf", "size": 1100}]})
    w = web({"/api/models/LiquidAI": Resp(body=info.encode()), "/api/models": Resp(body=models.encode()),
             "README.md": Resp(body=b"# LFM3")})
    assert w.hf_search("lfm3")["results"][0]["id"] == "LiquidAI/LFM3-1.7B"
    m = w.hf_model("https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF")
    assert m["files"] == [{"name": "LFM3-1.7B-Q4_K_M.gguf", "size": 1100}] and m["readme"] == "# LFM3"
    assert hf_url("hf://LiquidAI/LFM3-1.7B-GGUF/LFM3-1.7B-Q4_K_M.gguf") == \
        "https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF/resolve/main/LFM3-1.7B-Q4_K_M.gguf"


def test_github_search_folder_and_file():
    found = json.dumps({"items": [{"full_name": "o/r", "description": "d", "stargazers_count": 3,
                                   "language": "Python", "pushed_at": "2026-11-01T00:00:00Z", "html_url": "u"}]})
    folder = json.dumps([{"name": "src", "type": "dir"}, {"name": "README.md", "type": "file", "size": 5}])
    file = json.dumps({"encoding": "base64", "content": base64.b64encode(b"print('hi')").decode()})
    w = web({"search/repositories": Resp(body=found.encode()), "contents/main.py": Resp(body=file.encode()),
             "contents/": Resp(body=folder.encode())})
    assert w.github_search("book writer")["results"][0]["repo"] == "o/r"
    assert w.github_read("o/r")["entries"][0] == {"name": "src", "folder": True, "size": None}
    assert w.github_read("https://github.com/o/r.git", "main.py")["text"] == "print('hi')"
    assert _repo("github.com/o/r/tree/main", "github.com") == "o/r"
    with pytest.raises(ValueError):
        _repo("not a repo", "github.com")


def test_offline_and_errors_are_plain():
    import requests

    class Down(Session):
        def get(self, url, **kw):
            raise requests.ConnectionError("no route")
    with pytest.raises(NotFound, match="internet"):
        Web(Down({}), check_address=lambda u: u).read_page("https://x.org")
    with pytest.raises(NotFound, match="500"):
        web({"x.org": Resp(500)}).read_page("https://x.org")


def test_download_saves_checks_space_and_never_overwrites(tmp_path):
    body = b"x" * 3_000_000
    w = web({"files.org": lambda: Resp(body=body, headers={"Content-Length": str(len(body))})})
    seen = []
    out = w.download("https://files.org/model.gguf", tmp_path, progress=lambda f, m: seen.append(f))
    assert (tmp_path / "model.gguf").stat().st_size == len(body) and out["sha256"] and seen[-1] == 1.0
    with pytest.raises(ValueError):
        w.download("https://files.org/model.gguf", tmp_path)
    with pytest.raises(NotFound, match="cancelled"):
        w.download("https://files.org/other.bin", tmp_path, cancel=lambda: True)
    assert not list(tmp_path.glob("*.part"))
    huge = web({"files.org": Resp(headers={"Content-Length": str(10 ** 15)})})
    with pytest.raises(NotFound, match="free space"):
        huge.download("https://files.org/huge.bin", tmp_path / "h")


def test_github_download_unzips_safely(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("r-main/README.md", "hello")
    w = web({"codeload.github.com": lambda: Resp(body=buf.getvalue())})
    out = w.github_download("o/r", tmp_path)
    assert (tmp_path / "r" / "README.md").read_text() == "hello" and not list(tmp_path.glob("*.zip"))
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        web({"codeload.github.com": lambda: Resp(body=bad.getvalue())}).github_download("o/s", tmp_path)


def test_page_text_helper():
    assert page_text("<title>T</title><p>a</p><p>b</p>") == ("T", "a\n\nb") or \
        page_text("<title>T</title><p>a</p><p>b</p>")[1].split() == ["a", "b"]


def keys(tmp_path):
    from jarvis_books.computer.apikeys import ApiKeys
    k = ApiKeys(tmp_path)
    k.save("weather", "SECRET-123456")
    return k


def test_api_keys_are_sealed_filled_and_scrubbed(tmp_path):
    from jarvis_books.computer.apikeys import HIDDEN, ApiKeys
    k = keys(tmp_path)
    assert b"SECRET-123456" not in k.path.read_bytes()
    again = ApiKeys(tmp_path)
    assert again.names() == ["weather"]
    assert again.fill({"h": ["Bearer {key:weather}"]}) == {"h": ["Bearer SECRET-123456"]}
    assert again.scrub("token=SECRET-123456&x") == f"token={HIDDEN}&x"
    with pytest.raises(NotFound):
        again.fill("{key:nope}")
    with pytest.raises(ValueError):
        again.save("bad name!", "x")
    assert again.remove("weather") and not again.remove("weather") and again.names() == []


def test_call_api_puts_keys_in_and_never_shows_them(tmp_path):
    k = keys(tmp_path)
    echo = b'{"city": "Portland", "temp": 61, "echo": "SECRET-123456"}'
    s = Session({"api.weather.org": Resp(body=echo, headers={"Content-Type": "application/json"},
                                         url="https://api.weather.org/now?appid=SECRET-123456")})
    out = Web(s, check_address=lambda u: u).call_api("https://api.weather.org/now?appid={key:weather}",
                                                     headers="X-Mode: fast", keys=k)
    method, url, kw = s.seen[-1]
    assert method == "GET" and url.endswith("appid=SECRET-123456") and kw["headers"] == {"X-Mode": "fast"}
    assert out["ok"] and out["json"]["temp"] == 61
    assert "SECRET" not in json.dumps(out)


def test_send_api_sends_json_or_text_and_reports_errors_without_raising():
    s = Session({"api.x.org": Resp(status=422, body=b'{"error": "title missing"}',
                                   headers={"Content-Type": "application/json"})})
    w = Web(s, check_address=lambda u: u)
    out = w.call_api("https://api.x.org/books", "post", body='{"author": "Justin"}')
    assert s.seen[-1][0] == "POST" and s.seen[-1][2]["json"] == {"author": "Justin"}
    assert out["status"] == 422 and not out["ok"] and out["json"]["error"] == "title missing"
    w.call_api("https://api.x.org/notes", "PUT", body="plain words")
    assert s.seen[-1][2]["data"] == b"plain words"
    with pytest.raises(ValueError):
        w.call_api("https://api.x.org", "TRACE")
    with pytest.raises(ValueError):
        w.call_api("https://api.x.org", headers="no colon here")


def test_api_redirects_are_checked_at_every_hop():
    from jarvis_books.computer.web import _public
    hops = {"api.x.org/old": Resp(status=302, headers={"Location": "https://api.x.org/new"}),
            "api.x.org/new": Resp(body=b"[1, 2]", headers={"Content-Type": "application/json"}),
            "api.x.org/sneaky": Resp(status=307, headers={"Location": "http://127.0.0.1:11434/api/tags"})}

    def check(u):
        return u if "api.x.org" in u else _public(u)
    w = Web(Session(hops), check_address=check)
    assert w.call_api("https://api.x.org/old")["json"] == [1, 2]
    with pytest.raises(ValueError):
        w.call_api("https://api.x.org/sneaky")


def test_api_html_and_files_are_handled():
    w = web({"a.org/page": Resp(body=b"<html><title>T</title><p>hello</p></html>", headers={"Content-Type": "text/html"}),
             "a.org/zip": Resp(body=b"PK\x03\x04", headers={"Content-Type": "application/zip"})})
    page = w.call_api("https://a.org/page")
    assert page["title"] == "T" and "hello" in page["text"]
    assert "download_file" in w.call_api("https://a.org/zip")["note"]


FIRST_WORD_ONLY = (b'<li class="b_algo"><h2><a href="https://portableapps.com/">PortableApps.com - Portable software</a>'
                   b'</h2><p>Portable apps.</p></li><li class="b_algo"><h2><a href="https://www.merriam-webster.com/'
                   b'dictionary/portable">PORTABLE Definition</a></h2><p>Portable.</p></li>')


def test_when_the_free_engines_block_this_computer_the_ai_search_finds_the_pages():
    from jarvis_books.computer.web import ROBOT_REST_S
    session = Session({"duckduckgo": Resp(202, b"anomaly"), "bing.com/search": Resp(body=FIRST_WORD_ONLY),
                       "wikipedia.org/w/api.php": Resp(body=b'{"query": {"search": []}}'),
                       "bobvila.com": _page("https://www.bobvila.com/best", "Best solar generators")})
    w = Web(session, check_address=lambda u: u)
    now = [1000.0]
    w.clock = lambda: now[0]
    asked = []

    def ai(query, limit):
        asked.append((query, limit))
        return [{"title": "Best Solar Generators", "url": "https://www.bobvila.com/best", "snippet": ""},
                {"title": "bad", "url": "file:///etc/passwd", "snippet": ""}]
    w.ai_search = ai
    out = w.search("portable solar generator reviews")
    assert out["engine"] == "groq" and not out.get("blocked")
    assert [r["url"] for r in out["results"]] == ["https://www.bobvila.com/best"]
    assert asked == [("portable solar generator reviews", 8)]
    bing_asks = sum("bing" in u for u, _ in session.seen)
    found = w.research("portable solar generator reviews", 2)
    assert [s["url"] for s in found["sources"]] == ["https://www.bobvila.com/best"]
    assert sum("bing" in u for u, _ in session.seen) == bing_asks  # Bing rests after sending only junk
    now[0] += ROBOT_REST_S
    w.search("portable solar generator reviews")
    assert sum("bing" in u for u, _ in session.seen) > bing_asks


def test_bing_isnt_rested_when_its_key_word_search_answers():
    good = (b'<li class="b_algo"><h2><a href="https://s.org/solar">Solar generator guide</a></h2>'
            b'<p>Portable solar generators compared.</p></li>')
    replies = iter([FIRST_WORD_ONLY, good, good])
    session = Session({"duckduckgo": Resp(202, b"anomaly"),
                       "bing.com/search": lambda: Resp(body=next(replies))})
    w = Web(session, check_address=lambda u: u)
    assert w.search("what are the best portable solar generators")["engine"] == "bing"
    assert w._bing_rest_until == 0.0


def test_a_failing_ai_search_falls_back_to_wikipedia():
    wiki = json.dumps({"query": {"search": [{"title": "Solar generator", "snippet": "a portable solar generator"}]}})
    session = Session({"duckduckgo": Resp(202, b"anomaly"), "bing.com/search": Resp(body=b""),
                       "wikipedia.org/w/api.php": Resp(body=wiki.encode())})
    w = Web(session, check_address=lambda u: u)

    def broken(query, limit):
        raise RuntimeError("Groq changed its answer")
    w.ai_search = broken
    out = w.search("portable solar generator")
    assert out["engine"] == "wikipedia" and out["results"][0]["title"] == "Solar generator"


def test_the_app_searches_through_groq_when_the_engines_block_it(tmp_path):
    from fakes import FakeGemini
    from jarvis_books.studio import Studio
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini())
    try:
        assert studio.web.ai_search.__self__.key == studio._groq_key
    finally:
        studio.close()


ARXIV_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>http://arxiv.org/abs/2610.28671v1</id><published>2026-10-17T00:00:00Z</published>
  <title>Accelerating CPU Speculative
   Decoding</title><summary> Faster decoding on plain processors. </summary>
  <author><name>A. Lee</name></author><link title="pdf" href="http://arxiv.org/pdf/2610.28671v1"/></entry>
 <entry><id>http://arxiv.org/abs/2605.06485v2</id><published>2026-05-07T00:00:00Z</published>
  <title>Ternary Models on CPUs</title><summary>1.58-bit.</summary><author><name>B. Kim</name></author></entry>
</feed>"""


def test_paper_search_merges_arxiv_and_semantic_scholar_newest_first():
    scholar = json.dumps({"data": [
        {"title": "Ternary models on CPUs", "year": 2026, "publicationDate": "2026-05-07", "abstract": "dup",
         "url": "https://s2.org/1", "authors": [{"name": "B. Kim"}], "citationCount": 3},
        {"title": "Small Tool-Calling Models", "year": 2025, "publicationDate": None, "abstract": "tools",
         "url": "https://s2.org/2", "authors": [], "openAccessPdf": {"url": "https://s2.org/2.pdf"}}]})
    w = web({"export.arxiv.org": Resp(body=ARXIV_FEED), "semanticscholar.org": Resp(body=scholar.encode())})
    out = w.papers("CPU speculative decoding", limit=5)
    titles = [p["title"] for p in out["papers"]]
    assert titles == ["Accelerating CPU Speculative Decoding", "Ternary Models on CPUs", "Small Tool-Calling Models"]
    first = out["papers"][0]
    assert first["pdf"].endswith("2610.28671v1") and first["authors"] == ["A. Lee"] and first["source"] == "arXiv"
    assert out["papers"][2]["date"] == "2025-01-01" and out["papers"][2]["pdf"] == "https://s2.org/2.pdf"
    query = [params for url, params in w.session.seen if "arxiv" in url][0]["search_query"]
    assert query == "abs:CPU AND abs:speculative AND abs:decoding"
    assert [p["title"] for p in w.papers("x", since_year=2026)["papers"]][-1] == "Ternary Models on CPUs"


def test_paper_search_keeps_working_when_one_source_is_down():
    w = web({"export.arxiv.org": Resp(body=ARXIV_FEED), "semanticscholar.org": Resp(429)})
    out = w.papers("ternary")
    assert len(out["papers"]) == 2 and "Semantic Scholar" in out["problems"][0]
    with pytest.raises(NotFound, match="couldn't search for papers"):
        web({}).papers("ternary")
