"""Jarvis's hands on the computer: files with backups and undo, the terminal, and the internet (mocked)."""
import base64
import io
import json
import sys
import threading
import zipfile

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
    assert pc.move("a.txt", "dest")["to"].endswith("dest/a.txt")


def test_system_folders_drives_secrets_and_home_are_off_limits(tmp_path):
    win = Computer(tmp_path / "w", home=tmp_path, platform="win32")
    assert "Windows system folder" in win.protected(__import__("pathlib").PureWindowsPath("C:/Windows/System32"))
    assert win.protected(__import__("pathlib").PureWindowsPath("C:/")) == "a whole drive"
    pc = Computer(tmp_path / "w2", home=tmp_path / "h", platform="linux")
    for bad in ("/etc/passwd", "/", str(tmp_path / "h" / ".ssh" / "id_rsa"), str(tmp_path / "w2" / "backups" / "x")):
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


def test_web_search_parses_results():
    html_page = (b'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fhuggingface.co'
                 b'%2FLiquidAI&rut=1">Liquid <b>AI</b></a><a class="result__snippet" href="x">Small models</a>'
                 b'<a class="result__a" href="https://github.com/x/y">x/y</a><a class="result__snippet" href="z">repo</a>')
    out = web({"duckduckgo": Resp(body=html_page)}).search("liquid ai")
    assert out["results"][0] == {"title": "Liquid AI", "url": "https://huggingface.co/LiquidAI", "snippet": "Small models"}
    assert out["results"][1]["url"] == "https://github.com/x/y"


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
