"""The NEXUS dashboard reads real records from every system and shows a problem instead of made-up numbers."""
import time

import pytest

from fakes import FakeGemini
from jarvis_books.nexus.contracts import NexusRequest
from jarvis_books.nexus.dashboard import SECTIONS, render, snapshot
from jarvis_books.studio import Studio
from nexus_site import FakeWeb, page

A = "https://dash.example/"


@pytest.fixture
def studio(tmp_path):
    from conftest import hardware
    s = Studio(home=tmp_path / "home", mirror=tmp_path / "mirror", client=FakeGemini(),
               engine_kwargs={"hardware_probe": hardware, "sleep": lambda _: None})
    s.start(probe=False)
    yield s
    s.close()


def test_every_section_comes_from_the_running_systems(studio):
    studio.nexus.fetcher_factory = FakeWeb({A: page("Dash Robotics", "Dash Robotics was founded in 2001 in "
                                                                        "Atlanta. " * 5)}).factory()
    done = studio.nexus_hub.route(NexusRequest("jarvis", "os1", "crawl", {"seeds": [A], "max_pages": 1}))
    assert done.ok
    studio.nexus.search_engine.search("dash robotics")
    snap = snapshot(studio)
    assert set(SECTIONS) <= set(snap)
    assert not [k for k in SECTIONS if set(snap[k]) == {"error"}]
    assert snap["search"]["searchable"] == 1 and snap["search"]["searches"] == 1
    assert snap["crawl"]["crawls"] == 1 and snap["crawl"]["by_status"] == {"fetched": 1}
    assert snap["api"]["systems"]["os1"]["requests"] >= 1
    assert snap["tools"]["registered"] == len(studio.nexus_os3.registry)
    assert snap["releases"]["cycle"]["name"] == "NEXUS 1.0"
    assert snap["models"]["installed"] == studio.nexus_models.models()
    assert snap["collective"]["workers"] == len(studio.nexus_collective.coordinator.workers(False))
    text = render(snap)
    for title in ("NEXUS API", "Search (OS1)", "Web exploring (OS1)", "Facts (OS2)", "Tools (OS3)", "Collective",
                  "Models on this computer", "Self-improvement", "Release Center"):
        assert f"<h3>{title}</h3>" in text
    assert "1 pages known" in text and "NEXUS 1.0" in text


def test_a_broken_system_shows_its_problem_and_the_rest_still_show(studio, monkeypatch):
    def broken():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(studio.nexus.store, "search_stats", broken)
    snap = snapshot(studio)
    assert snap["search"] == {"error": "RuntimeError: database is locked"}
    assert set(snap["crawl"]) != {"error"} and set(snap["releases"]) != {"error"}
    text = render(snap)
    assert "Couldn't read this:</b> RuntimeError: database is locked" in text and "<h3>Release Center</h3>" in text


def test_render_never_invents_measurements():
    now = time.time()
    snap = {"taken_at": now,
            "models": {"installed": [{"name": "qwen3:4b", "parameters": "4.0B", "quantization": "Q4_K_M",
                                      "size_gb": 2.5, "first_answer_ok": None, "output_tps": None}], "installs": []},
            "search": {"searchable": 0, "dated_pages": 0, "published_last_year": 0, "checked_last_week": 0,
                       "vectors": 0, "searches": 0, "median_ms": None, "p95_ms": None, "cached_share": None,
                       "empty_share": None},
            "collective": {"workers": 0, "healthy_workers": 0, "tasks": {}, "worker_list": []},
            "api": {"running": False, "error": "port 8770 is busy", "uptime_s": 0, "systems": {}}}
    text = render(snap)
    assert "not tested" in text and "not measured" in text and "no searches yet" in text
    assert "No workers have joined." in text and "stopped port 8770 is busy" in text
    assert "<script" not in render({"taken_at": now, "facts": {"error": "<script>x</script>"}})
