import json
import threading
import time

import pytest

from jarvis_books.llm import lab as labmod
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.lab import BASELINE, BrainLab, LabClock, targets_for, unmet, utility
from jarvis_books.llm.tool_brain import FastRouter
from jarvis_books.assistant.tools import DECLARATIONS

SPECS = {"cpu": "Intel Core i3-7100U", "cores": 2, "threads": 4, "ram_gb": 15.9, "flags": ["AVX2", "FMA3"],
         "cuda_gpu": False}
GOOD = {"add": "```python\ndef add(a, b):\n    return a + b\n```"}
STEP1 = ("class Box:\n    def __init__(self):\n        self.items = []\n"
         "    def put(self, x):\n        self.items.append(x)\n")
STEP2 = STEP1 + "    def count(self):\n        return len(self.items)\n"


class Client:
    """Ollama as the lab sees it: 'bad' models write wrong code; the starting model is right but slow."""

    def __init__(self, ram_gb=1.0):
        self.installed, self.pulls, self.asked, self.ram_gb = {BASELINE["model"]}, [], [], ram_gb

    def list_models(self):
        return [{"name": n} for n in self.installed]

    def pull(self, model, cancel=None):
        if "missing" in model:
            raise LLMError("download failed: not found", kind="unknown")
        self.pulls.append(model)
        self.installed.add(model)

    def running(self):
        return [{"name": n, "size": int(self.ram_gb * 1024 ** 3)} for n in self.installed]

    def chat(self, payload, timeout=None, idle_s=None, first_token=None):
        model, text = payload["model"], payload["messages"][-1]["content"]
        self.asked.append((model, payload["options"]))
        if first_token:
            first_token()
        if "Step 1" in text:
            answer = STEP1
        elif "Step 2" in text:
            answer = STEP2
        else:
            answer = GOOD["add"] if "bad" not in model else "def add(a, b):\n    return 0\n"
        speed = 4 if model == BASELINE["model"] else 20  # the starting model writes below the target
        return {"message": {"content": answer}, "eval_count": 100, "eval_duration": int(100 / speed * 1e9)}


@pytest.fixture
def quick(monkeypatch, tmp_path):
    """Small, fast stand-ins for the lab's fixed tests so a round takes seconds."""
    real = labmod.train_tool_brain
    monkeypatch.setattr(labmod, "train_tool_brain", lambda decls, **kw: real(
        decls, **dict(kw, per_template=3, epochs=2)))
    monkeypatch.setattr(labmod, "load_coding", lambda: [{"id": "add", "prompt": "Write add(a, b).",
                                                         "tests": ["assert add(2, 3) == 5"]}])
    long = tmp_path / "long.json"
    long.write_text(json.dumps({"name": "box", "intro": "Build a box.", "steps": [
        {"ask": "Box with put.", "tests": ["b = Box()\nb.put(1)\nassert b.items == [1]"]},
        {"ask": "Add count.", "tests": ["b = Box()\nb.put(1)\nassert b.count() == 1"]}]}))
    monkeypatch.setattr(labmod, "LONG_PATH", long)


def make(tmp_path, client=None, web=None, rsi="1", **kw):
    router = FastRouter(tmp_path / "tool_brain")
    applied = []
    lab = BrainLab(tmp_path / "lab", client or Client(), router, lambda: DECLARATIONS, lambda: {"rsi": rsi},
                   web=web, measure_model_tools=lambda model, opts: 0.5 if "bad" in model else 0.7,
                   apply=applied.append, specs_probe=lambda: dict(SPECS), seed=3, **kw)
    return lab, router, applied


def test_targets_come_from_the_computers_specs():
    assert targets_for(SPECS)["ram_gb"] == 6.0
    assert targets_for(dict(SPECS, ram_gb=8))["ram_gb"] == 3.2
    t = targets_for(SPECS)
    perfect = {"tool_accuracy": 1, "tool_ms": 0.1, "coding": 1, "long_task": 1, "first_reply_s": 0.5,
               "writes_per_s": 30, "model_tools": 0.9, "ram_gb": 2}
    assert utility(perfect, t) == 1.0 and unmet(perfect, t) == []
    slow = dict(perfect, writes_per_s=5, first_reply_s=4)
    assert utility(slow, t) < 1.0 and unmet(slow, t) == ["first_reply_s", "writes_per_s"]


def test_first_round_measures_the_starting_brain_and_puts_it_to_work(tmp_path, quick):
    lab, router, applied = make(tmp_path)
    entry = lab.round()
    assert entry["kept"] and entry["note"] == "starting point"
    m = entry["measures"]
    assert m["coding"] == 1.0 and m["long_task"] == 1.0 and m["writes_per_s"] == 4.0 and m["model_tools"] == 0.7
    assert 0 < m["tool_accuracy"] <= 1 and m["tool_ms"] < 100 and m["ram_gb"] == 1.0
    assert router.brain is not None and (tmp_path / "tool_brain" / "tool_brain.npz").exists()
    assert applied[-1]["model"] == BASELINE["model"]
    status = BrainLab(tmp_path / "lab", Client(), router, lambda: DECLARATIONS, lambda: {"rsi": "1"}).status()
    assert status["rounds"] == 1 and status["best"]["model"] == "LFM3 1.7B" and "i3-7100U" in status["computer"]


def test_a_worse_version_is_thrown_away_and_a_better_one_kept(tmp_path, quick, monkeypatch):
    client = Client()
    lab, router, applied = make(tmp_path, client)
    lab.round()
    first = lab.state["champion"]
    for model, kept in (("bad-model", False), ("good-fast", True)):
        monkeypatch.setattr(lab, "_mutate", lambda g, c, m=model: (dict(g, model=m), ["model"], "test"))
        entry = lab.round()
        assert entry["kept"] is kept, entry
    assert lab.state["champion"]["genome"]["model"] == "good-fast" and lab.state["champion"] is not first
    assert applied[-1]["model"] == "good-fast" and client.pulls == ["bad-model", "good-fast"]
    assert "not better" in lab.state["history"][1]["note"]


def test_too_much_memory_or_a_failed_download_never_wins(tmp_path, quick, monkeypatch):
    client = Client()
    lab, _, _ = make(tmp_path, client)
    lab.round()
    client.ram_gb = 9.0
    monkeypatch.setattr(lab, "_mutate", lambda g, c: (dict(g, model="good-big"), ["model"], "test"))
    assert "over the 6.0 GB budget" in lab.round()["note"]
    client.ram_gb = 1.0
    monkeypatch.setattr(lab, "_mutate", lambda g, c: (dict(g, model="missing-model"), ["model"], "test"))
    entry = lab.round()
    assert not entry["kept"] and "download failed" in entry["measures"]["model_problem"]
    assert lab.state["champion"]["genome"]["model"] == BASELINE["model"]


def test_only_the_changed_part_is_measured_again(tmp_path, quick, monkeypatch):
    client = Client()
    lab, _, _ = make(tmp_path, client)
    lab.round()
    asked = len(client.asked)
    monkeypatch.setattr(lab, "_mutate", lambda g, c: (dict(g, tool_confidence=0.8), ["tool_confidence"], "t"))
    entry = lab.round()
    assert len(client.asked) == asked  # no model questions for a tool-brain change
    monkeypatch.setattr(lab, "_mutate", lambda g, c: (dict(g, num_thread=2), ["num_thread"], "t"))
    lab.round()
    assert client.asked[-1][1]["num_thread"] == 2 and entry["measures"]["tool_train_s"] >= 0


class Papers:
    def __init__(self, fail=False):
        self.fail, self.asked = fail, []

    def papers(self, query, limit=8, since_year=0):
        self.asked.append(query)
        if self.fail:
            raise RuntimeError("offline")
        return {"papers": [{"title": "Ternary 1.58-bit models for CPU inference", "date": "2026-05-07",
                            "url": "https://arxiv.org/abs/2605.06485", "summary": "fast on plain processors"}]}


def test_paper_ideas_steer_what_the_lab_tries_next(tmp_path, quick):
    web = Papers()
    lab, _, _ = make(tmp_path, web=web)
    lab.round()
    assert web.asked and lab.state["papers"][0]["genes"] == ["model", "num_thread"]
    assert lab.state["boosts"]["model"] == ["Ternary 1.58-bit models for CPU inference"]
    picks = [lab._mutate(BASELINE, labmod.gene_choices(SPECS, 6.0))[1][0] for _ in range(300)]
    assert picks.count("model") > picks.count("tool_epochs")
    broken, _, _ = make(tmp_path / "b", web=Papers(fail=True))
    assert broken.round()["kept"] and "paper search failed" in broken.problem


def test_a_round_stops_cleanly_when_jarvis_is_needed(tmp_path, quick):
    lab, router, _ = make(tmp_path)
    assert lab.round(cancel=lambda: True) == {"stopped": True}
    assert lab.state["rounds"] == 0 and lab.state["champion"] is None and router.brain is None
    assert not lab.running


def test_the_clock_runs_rounds_only_while_rsi_is_on_and_jarvis_is_idle(tmp_path):
    settings = {"rsi": "0"}
    lab = BrainLab(tmp_path, Client(), None, lambda: DECLARATIONS, lambda: settings, specs_probe=lambda: SPECS)
    rounds = []
    lab.round = lambda cancel: rounds.append(cancel()) or {}
    lab.pace_s = lambda: 0.05
    busy = threading.Event()
    clock = LabClock(lab, busy=busy.is_set, start_delay=0)
    clock.start()
    time.sleep(0.3)
    assert rounds == []
    settings["rsi"] = "1"
    busy.set()
    clock.poke()
    time.sleep(0.3)
    assert rounds == []
    busy.clear()
    clock.poke()
    deadline = time.time() + 3
    while not rounds and time.time() < deadline:
        time.sleep(0.05)
    clock.stop()
    assert rounds and rounds[0] is False and not clock.running


def test_studio_wires_rsi_mode_its_tools_and_the_idle_check(tmp_path):
    from jarvis_books.studio import Studio
    studio = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror")
    try:
        assert studio.settings()["rsi"] == "1" and studio.tools.lab is studio.lab is studio.war_tools.lab
        status = studio.tools.call("brain_lab_status", {}, source="ui")
        assert status["rounds"] == 0 and status["on"] is True and status["computer"] == "not measured yet"
        assert studio.tools._t_brain_lab_switch(False)["on"] is False and studio.settings()["rsi"] == "0"
        assert "RSI mode is off" in studio.tools._t_brain_lab_round()["note"]
        assert studio.tools._setting_value("rsi", "on") == "1"
        studio.assistant.last_used = time.time()
        assert studio._lab_busy()
        studio.assistant.last_used = 0.0
        assert not studio._lab_busy()
        studio.tools._t_brain_lab_switch(True)
        go, done = threading.Event(), threading.Event()
        studio.jobs.submit("upgrade", "Building app upgrade #1", lambda progress, cancel: go.wait(5) and done.set())
        assert not studio._lab_busy()
        assert studio._lab_defer() and studio.upgrader.hold()  # the lab asks for the first turn
        studio.upgrader.resting = 1  # the upgrade parks after its try
        assert not studio._lab_defer()
        studio.lab_clock.attempts += 1  # a round ran
        studio.upgrader.resting = 0
        assert studio._lab_defer() and not studio.upgrader.hold()  # now the upgrade's turn
        studio.upgrader.tries_done += 1
        assert studio._lab_defer() and studio.upgrader.hold()  # one try done: the lab's turn again
        studio.upgrader.resting = 1
        assert not studio._lab_defer()
        studio.upgrader.resting = 0
        go.set()
        assert done.wait(5)
        studio._apply_lab(dict(BASELINE, model="hf.co/unsloth/Qwen3.5-2B-GGUF:Q4_K_M"))
        assert studio.local.label == "Qwen3.5 2B"
        assert studio.library.get_meta("lab_model", "").endswith("2B-GGUF:Q4_K_M")
    finally:
        studio.close()
    again = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror")
    try:
        assert again.local.model == "hf.co/unsloth/Qwen3.5-2B-GGUF:Q4_K_M"  # the lab's pick survives a restart
    finally:
        again.close()


def test_the_quick_tool_brain_knows_the_lab_and_paper_tools():
    from jarvis_books.llm.tool_data import load_templates
    tools = {t.tool for t in load_templates()}
    assert {"brain_lab_status", "brain_lab_switch", "brain_lab_round", "search_papers"} <= tools
    from jarvis_books.assistant.permissions import TOOL_LEVELS as LEVELS
    assert LEVELS["search_papers"] == LEVELS["brain_lab_status"] == "read"
    assert LEVELS["brain_lab_switch"] == "safe"


def test_other_background_work_delays_a_round_without_stopping_one(tmp_path):
    lab = BrainLab(tmp_path, Client(), None, lambda: DECLARATIONS, lambda: {"rsi": "1"}, specs_probe=lambda: SPECS)
    rounds = []
    lab.round = lambda cancel: rounds.append(cancel()) or {}
    lab.pace_s = lambda: 0.05
    researching = threading.Event()
    researching.set()
    clock = LabClock(lab, start_delay=0, defer=researching.is_set)
    clock.start()
    time.sleep(0.3)
    assert rounds == [] and clock.waiting == "another background job is using the computer"
    researching.clear()
    clock.poke()
    deadline = time.time() + 3
    while not rounds and time.time() < deadline:
        time.sleep(0.05)
    researching.set()
    clock.stop()
    assert rounds and rounds[0] is False  # the researcher starting again doesn't cancel a running round


def test_the_researcher_waits_while_the_lab_measures(tmp_path):
    from jarvis_books.research import ResearchClock

    class Res:
        ticks = 0
        problem = ""

        def enabled(self):
            return True

        def pace_s(self):
            return 0.05

        def tick(self):
            Res.ticks += 1
    clock = ResearchClock(Res(), start_delay=0)
    clock.hold = lambda: True
    clock.start()
    time.sleep(0.3)
    assert Res.ticks == 0 and not clock.working
    clock.hold = lambda: False
    clock.poke()
    deadline = time.time() + 3
    while not Res.ticks and time.time() < deadline:
        time.sleep(0.05)
    clock.stop()
    assert Res.ticks >= 1 and not clock.working
