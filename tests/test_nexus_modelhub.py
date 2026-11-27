import pytest

from jarvis_books.nexus.deepram import GB
from jarvis_books.nexus.modelhub import HubError, ModelHub, manifest_url, source_of
from test_nexus_deepram import QWEN_BYTES, QWEN_SHOW


class FakeOllama:
    """Ollama's real reply shapes; models appear only after a pull."""

    def __init__(self, installed=("qwen3:4b",), answer="ready"):
        self.installed = list(installed)
        self.answer, self.pulled, self.chats, self.loaded = answer, [], [], []

    def list_models(self):
        return [{"name": n, "size": QWEN_BYTES if "qwen" in n else int(0.9 * GB),
                 "digest": f"sha256-{n}", "details": {"quantization_level": "Q4_K_M"}} for n in self.installed]

    def show(self, name):
        if "qwen" in name:
            return dict(QWEN_SHOW, capabilities=["completion", "tools"], license="Apache License 2.0\n...")
        return {"model_info": {"general.architecture": "nomic-bert", "nomic-bert.block_count": 12,
                               "nomic-bert.attention.head_count": 12, "nomic-bert.embedding_length": 768,
                               "nomic-bert.context_length": 2048}, "capabilities": ["embedding"]}

    def running(self):
        return [{"name": n} for n in self.loaded]

    def pull(self, name, progress=None, cancel=None):
        self.pulled.append(name)
        if progress:
            progress(0.5, "pulling")
        self.installed.append(name)

    def chat(self, payload, timeout=None):
        self.chats.append(payload)
        if self.answer is None:
            raise ConnectionError("model crashed")
        return {"message": {"content": self.answer}, "eval_count": 8, "eval_duration": 2_000_000_000,
                "prompt_eval_count": 20, "prompt_eval_duration": 1_000_000_000, "load_duration": 3_000_000_000}


class Manifest:
    def __init__(self, size=None, status=200):
        self.size, self.status, self.urls = size, status, []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        outer = self

        class R:
            status_code = outer.status

            def json(self):
                return {"layers": [{"size": outer.size}, {"size": 500}]}
        return R()


def hub(tmp_path, client=None, free_gb=9.0, total_gb=16.0, disk_gb=100.0, manifest=None):
    return ModelHub(tmp_path, client or FakeOllama(), memory=lambda: (int(total_gb * GB), int(free_gb * GB)),
                    http=manifest or Manifest(), disk_free=lambda: int(disk_gb * GB))


def test_sync_records_real_metadata_and_checksums(tmp_path):
    h = hub(tmp_path)
    [m] = h.sync()
    assert m["parameters"] == 4022468096 and m["quantization"] == "Q4_K_M" and m["max_context"] == 262144
    assert m["digest"] == "sha256-qwen3:4b" and m["license"] == "Apache License 2.0"
    assert m["source"] == "ollama:qwen3" and "tools" in m["capabilities"]
    h.client.installed = []
    assert h.sync() == [] and not h.models(installed_only=False)[0]["installed"]
    h.close()


def test_plan_uses_current_free_memory_and_offers_a_context_that_fits(tmp_path):
    h = hub(tmp_path, free_gb=5.0)
    h.sync()
    ok = h.plan("qwen3:4b", 4096)
    assert ok["fits"]
    big = h.plan("qwen3:4b", 131072)
    assert not big["fits"] and 2048 <= big["largest_context_that_fits"] < 131072
    with pytest.raises(HubError):
        h.plan("nope:1b")
    h.close()


def test_install_checks_disk_and_memory_downloads_tests_and_benchmarks(tmp_path):
    client = FakeOllama(installed=())
    h = hub(tmp_path, client, manifest=Manifest(size=int(0.9 * GB)))
    out = h.install("nomic-embed-text:latest")
    assert out["state"] == "installed" and client.pulled == ["nomic-embed-text:latest"]
    assert out["output_tps"] == 4.0 and out["prompt_tps"] == 20.0
    assert h.benchmarks("nomic-embed-text:latest")[0]["ok"] == 1
    assert h.installs()[0]["state"] == "installed"
    assert h.http.urls[0].endswith("/library/nomic-embed-text/manifests/latest")
    h.close()


def test_install_refuses_what_cant_fit_and_records_why(tmp_path):
    full_disk = hub(tmp_path / "a", FakeOllama(installed=()), disk_gb=2.0, manifest=Manifest(size=int(3 * GB)))
    with pytest.raises(HubError, match="disk"):
        full_disk.install("qwen3:4b")
    assert full_disk.installs()[0]["state"] == "failed" and not full_disk.client.pulled
    tiny = hub(tmp_path / "b", FakeOllama(installed=()), total_gb=4.0, free_gb=3.0,
               manifest=Manifest(size=int(20 * GB)))
    with pytest.raises(HubError, match="can't fit"):
        tiny.install("llama3:70b")
    with pytest.raises(HubError):
        tiny.install("bad name")


def test_a_model_that_cant_answer_is_never_routed_to(tmp_path):
    client = FakeOllama(installed=(), answer=None)
    h = hub(tmp_path, client, manifest=Manifest(size=QWEN_BYTES))
    out = h.install("qwen3:4b")
    assert out["state"] == "failed_test" and "model crashed" in out["note"]
    r = h.route(4096)
    assert r["model"] is None and "failed its first answer test" in r["considered"][0]["why"]
    h.close()


def test_router_picks_the_best_model_that_fits_now(tmp_path):
    client = FakeOllama(installed=("qwen3:4b", "lfm:1.7b"))
    h = hub(tmp_path, client, free_gb=9.0)
    h.sync()
    for name in ("qwen3:4b", "lfm:1.7b"):
        h.smoke_test(name)
    assert h.route(4096, capability="completion")["model"] in ("qwen3:4b", "lfm:1.7b")
    h.db.execute("UPDATE models SET capabilities='[\"completion\"]', parameters=1700000000, max_context=128000 WHERE name='lfm:1.7b'")
    h.db.execute("UPDATE benchmarks SET output_tps=12 WHERE name='lfm:1.7b'")
    assert h.route(4096)["model"] == "qwen3:4b"
    assert h.route(4096, prefer="speed")["model"] == "lfm:1.7b"
    assert h.route(4096, min_output_tps=10)["model"] == "lfm:1.7b"
    h.memory = lambda: (int(16 * GB), int(3.5 * GB))  # the laptop got busy
    low = h.route(8192)
    assert low["model"] in ("lfm:1.7b", None)
    assert any("only 3.5 GB is free" in " ".join(c["why"]) for c in low["considered"] if c["name"] == "qwen3:4b")
    client.loaded = ["qwen3:4b"]  # already in memory: only its context is needed
    assert h.route(4096)["model"] == "qwen3:4b"
    h.close()


def test_names_map_to_sources_and_manifests():
    assert source_of("hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M") == "huggingface:LiquidAI/LFM3-1.7B-GGUF"
    assert manifest_url("qwen3:4b").endswith("/library/qwen3/manifests/4b")
    assert manifest_url("someone/model").endswith("/someone/model/manifests/latest")
    assert manifest_url("hf.co/a/b:Q4") is None


def test_jarvis_model_tools_read_freely_and_installs_wait_for_a_yes(tmp_path):
    from jarvis_books.assistant.nexus_tools import MODEL_TOOLS, NexusTools
    from jarvis_books.assistant.permissions import NEEDS_YES, OUTSIDE, TOOL_LEVELS
    from jarvis_books.errors import NotFound
    assert set(MODEL_TOOLS) == {"model_hub", "model_plan", "model_route", "model_install", "hardware_advice"}
    assert TOOL_LEVELS["model_install"] == OUTSIDE
    assert not any(TOOL_LEVELS[t] in NEEDS_YES for t in MODEL_TOOLS if t != "model_install")
    tools = NexusTools()
    with pytest.raises(NotFound):
        tools._t_model_hub()
    client = FakeOllama(installed=("qwen3:4b",))
    tools.models = hub(tmp_path, client, manifest=Manifest(size=int(0.9 * GB)))
    assert tools._t_model_hub()["models"][0]["name"] == "qwen3:4b"
    assert tools._t_model_plan("qwen3:4b", 8192)["fits"]
    assert tools._t_model_route(4096)["model"] == "qwen3:4b"
    out = tools._t_model_install(" nomic-embed-text:latest ")
    assert out["state"] == "installed"
    assert tools._t_model_route(1024, capability="embedding")["model"] == "nomic-embed-text:latest"
    tools.models.close()
