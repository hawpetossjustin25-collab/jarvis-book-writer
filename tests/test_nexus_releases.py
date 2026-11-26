import json
import sqlite3
from datetime import datetime

import pytest

from jarvis_books.errors import NotFound
from jarvis_books.nexus.releases import (CYCLE_DAYS, ReleaseCenter, ReleaseError, Running, public_text,
                                         read_running)

SHA_A = "a" * 40
SHA_B = "b" * 40


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 26).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def rc(tmp_path):
    center = ReleaseCenter(tmp_path, clock=Clock())
    yield center
    center.close()


def make_tested(rc, title="Faster web index search", area="search", author="jarvis"):
    i = rc.propose(title, area, "performance", author=author)
    rc.advance(i["id"], "building", author)
    rc.advance(i["id"], "testing", author)
    rc.record(i["id"], "test_suite", "better", "1001 passed", "devin")
    rc.record(i["id"], "benchmark", "better", "search 0.16 s -> 0.09 s", "devin",
              baseline={"seconds": 0.16}, candidate={"seconds": 0.09})
    return rc.advance(i["id"], "tested", author)


def test_cycles_are_four_months_and_numbered(rc):
    first = rc.cycle(objectives=["Collective", "Model Hub"])
    second = rc.cycle()
    third, fourth = rc.cycle(), rc.cycle()
    assert [c["version"] for c in (first, second, third, fourth)] == ["1.0", "1.1", "1.2", "2.0"]
    assert round((first["ends"] - first["starts"]) / 86400) == CYCLE_DAYS
    assert second["starts"] == first["ends"] and rc.active_cycle()["version"] == "1.0"
    with pytest.raises(ReleaseError):
        rc.cycle("1.1")
    with pytest.raises(ReleaseError):
        rc.cycle("v9")


def test_an_improvement_needs_tests_and_no_regression_before_release(rc):
    rc.cycle()
    i = rc.propose("Better crawl politeness", "os1", "quality", author="jarvis")
    with pytest.raises(ReleaseError, match="can go to"):
        rc.advance(i["id"], "released", "jarvis")
    rc.advance(i["id"], "building", "jarvis")
    rc.advance(i["id"], "testing", "jarvis")
    with pytest.raises(ReleaseError, match="passing full test run"):
        rc.advance(i["id"], "tested", "jarvis")
    rc.record(i["id"], "test_suite", "failed", "3 failed", "devin")
    with pytest.raises(ReleaseError, match="passing full test run"):
        rc.advance(i["id"], "tested", "jarvis")
    rc.record(i["id"], "test_suite", "better", "all passed", "devin")
    rc.record(i["id"], "benchmark", "worse", "recall 0.81 -> 0.74", "devin")
    with pytest.raises(ReleaseError, match="measured worse"):
        rc.advance(i["id"], "tested", "jarvis")
    rc.record(i["id"], "benchmark", "same", "recall 0.81 -> 0.81", "devin")
    assert rc.advance(i["id"], "tested", "jarvis")["state"] == "tested"
    assert [e["verdict"] for e in rc.improvement(i["id"])["experiments"]] == ["failed", "better", "worse", "same"]


def test_only_justin_approves_and_never_his_own_work(rc):
    rc.cycle()
    i = make_tested(rc)
    with pytest.raises(ReleaseError, match="only Justin"):
        rc.approve(i["id"], "jarvis")
    mine = make_tested(rc, "Justin's own reminder tweak", "jarvis", author="justin")
    with pytest.raises(ReleaseError, match="own change"):
        rc.approve(mine["id"], "justin")
    ok = rc.approve(i["id"], "justin")
    assert ok["state"] == "approved" and ok["approver"] == "justin" and ok["author"] == "jarvis"


def test_release_ships_only_when_the_running_commit_is_seen(rc):
    rc.cycle(objectives=["search speed"])
    rc.observe(Running(SHA_A, "stable", "installer", None, {"passed": 1001}))
    i = make_tested(rc)
    rc.approve(i["id"], "justin")
    with pytest.raises(ReleaseError, match="Justin's approval"):
        rc.package(SHA_B, "stable", "jarvis")
    rel = rc.package(SHA_B, "stable", "justin")
    assert rel["version"] == "1.0.0" and rel["status"] == "packaged" and rel["verified_at"] is None
    assert rc.improvement(i["id"])["released_in"] == "1.0.0"
    assert rc.releases(public=True) == []  # not shipped until it is really running
    now = rc.observe(Running(SHA_B, "stable", "installer", None, {"passed": 1004}))
    assert now["commit"] == SHA_B and rc.release("1.0.0")["status"] == "deployed"
    assert rc.releases(public=True)[0]["version"] == "1.0.0"
    assert rc.center()["last_major_release"]["version"] == "1.0.0"
    with pytest.raises(ReleaseError, match="nothing is approved"):
        rc.package(SHA_B, "preview", "justin")


def test_urgent_fixes_skip_the_cycle_and_get_patch_versions(rc):
    rc.cycle()
    fix = rc.propose("Patch token leak in logs", "security", "security", author="devin")
    assert fix["urgent"] and fix["cycle"] is None
    for s in ("building", "testing"):
        rc.advance(fix["id"], s, "devin")
    rc.record(fix["id"], "test_suite", "better", "passed", "devin")
    rc.advance(fix["id"], "tested", "devin")
    rc.approve(fix["id"], "justin")
    assert rc.package(SHA_B, "emergency", "justin")["version"] == "1.0.1"
    pv = make_tested(rc, "Preview of the dashboard", "ui")
    rc.approve(pv["id"], "justin")
    assert rc.package(SHA_A, "preview", "jarvis")["version"] == "1.0.2-preview"


def test_public_notes_leave_out_private_details(rc):
    rc.cycle()
    i = make_tested(rc, "Rotate the Groq API key storage", "security")
    rc.approve(i["id"], "justin")
    rel = rc.package(SHA_B, "stable", "justin", notes="Installed to C:\\Users\\hawpe\\AppData\nFaster search")
    assert "Groq API key" not in rel["public_notes"] and "hawpe" not in rel["public_notes"]
    assert "Faster search" in rel["public_notes"] and "Groq API key" in rel["private_notes"]
    assert public_text("server 10.0.0.5 restarted\nok") == "ok"


def test_rejected_ideas_are_remembered_and_not_retried_silently(rc):
    rc.cycle()
    i = rc.propose("Speed up replies with a plus one counter", "models", "performance", author="jarvis")
    with pytest.raises(ReleaseError, match="say why"):
        rc.advance(i["id"], "rejected", "devin")
    rc.advance(i["id"], "rejected", "devin", "counting doesn't move model weights; no speedup measured")
    with pytest.raises(ReleaseError, match="already tried and rejected.*no speedup measured"):
        rc.propose("Speed up replies using a plus one counter", "models", "performance")
    again = rc.propose("Speed up replies with a plus one counter", "models", "performance",
                       despite_rejection="now paired with speculative decoding")
    assert again["state"] == "proposed"
    assert rc.journal(f"improvement:{again['id']}")[0]["detail"].count("speculative") == 1


def test_the_journal_and_experiments_cannot_be_erased(rc, tmp_path):
    rc.cycle()
    make_tested(rc)
    for sql in ("DELETE FROM journal", "UPDATE journal SET detail='x'", "DELETE FROM experiments",
                "UPDATE experiments SET verdict='better'"):
        with pytest.raises(sqlite3.DatabaseError, match="permanent"):
            rc.db.execute(sql)
    rc.close()
    reopened = ReleaseCenter(tmp_path, clock=Clock())
    assert len(reopened.journal()) >= 6
    reopened.close()


def test_moving_between_cycles_is_recorded(rc):
    rc.cycle()
    rc.cycle()
    i = rc.propose("Real-data NEXUS dashboard", "ui")
    assert i["cycle"] == "1.0"
    with pytest.raises(ReleaseError, match="say why"):
        rc.move(i["id"], "1.1", "justin", "")
    assert rc.move(i["id"], "1.1", "justin", "Model Hub first")["cycle"] == "1.1"
    assert json.loads(rc.journal(f"improvement:{i['id']}")[0]["detail"])["to"] == "1.1"
    with pytest.raises(NotFound):
        rc.move(i["id"], "9.9", "justin", "x")


def test_rollback_needs_a_version_that_ran_and_marks_the_release(rc):
    rc.cycle()
    rc.observe(Running(SHA_A, "stable", "installer", None, {}))
    i = make_tested(rc)
    rc.approve(i["id"], "justin")
    rc.package(SHA_B, "stable", "justin")
    rc.observe(Running(SHA_B, "stable", "installer", None, {}))
    with pytest.raises(NotFound):
        rc.rollback("c" * 7, "crashes", "justin")
    out = rc.rollback(SHA_A[:7], "Jarvis froze twice after the update", "justin")
    assert out["to"] == SHA_A and rc.release("1.0.0")["status"] == "rolled_back"
    rc.observe(Running(SHA_A, "stable", "installer", None, {}))
    assert rc.current()["commit"] == SHA_A and rc.center()["rollbacks"][0]["reason"].startswith("Jarvis froze")


def test_self_upgrades_and_search_rounds_feed_the_memory(rc):
    rc.cycle()
    ups = [{"id": 1, "request": "add a word counter", "summary": "Word counter", "status": "installed",
            "files": ["jarvis_books/text.py"], "test_output": ""},
           {"id": 2, "request": "make it purple", "summary": "", "status": "failed", "files": [],
            "test_output": "FAILED tests/test_ui.py::test_theme"},
           {"id": 3, "request": "undo me", "summary": "Bigger font", "status": "undone", "files": [],
            "test_output": ""}]
    assert rc.ingest_upgrades(ups) == 3 and rc.ingest_upgrades(ups) == 0
    by = {i["title"]: i for i in rc.improvements()}
    assert by["Word counter"]["state"] == "tested" and by["Word counter"]["author"] == "jarvis:self-upgrade"
    assert by["make it purple"]["state"] == "rejected" and by["Bigger font"]["state"] == "rejected"
    assert "test_theme" in rc.journal(f"improvement:{by['make it purple']['id']}")[0]["detail"]
    rsi = [{"id": 7, "kind": "search", "params": {"bm25": 0.6}, "metrics": {"ndcg": 0.71}, "status": "champion",
            "reason": "held-out nDCG 0.66 -> 0.71"},
           {"id": 8, "kind": "search", "params": {"bm25": 0.9}, "metrics": {"ndcg": 0.6}, "status": "rejected",
            "reason": "held-out nDCG fell"}]
    assert rc.ingest_rsi(rsi) == 2 and rc.ingest_rsi(rsi) == 0
    states = {i["title"]: i["state"] for i in rc.improvements()}
    assert states["search settings version 8"] == "rejected" and states["search settings version 7"] == "testing"


def test_running_version_comes_from_the_installer_or_git(tmp_path):
    assert read_running(tmp_path) is None
    (tmp_path / "version.json").write_text(json.dumps({"commit": SHA_A, "channel": "preview",
                                                       "tests": {"passed": 10}}), encoding="utf-8-sig")
    r = read_running(tmp_path)
    assert (r.commit, r.channel, r.source, r.tests) == (SHA_A, "preview", "installer", {"passed": 10})
    (tmp_path / "version.json").write_text("{bad")
    (tmp_path / ".git").mkdir()

    class Done:
        returncode, stdout = 0, SHA_B + "\n"
    assert read_running(tmp_path, run=lambda *a, **k: Done()).commit == SHA_B
    rc = ReleaseCenter(tmp_path / "home", clock=Clock())
    assert rc.observe(None)["version"] is None
    rc.close()


def test_jarvis_release_tools_and_justins_decisions_always_ask(rc):
    from jarvis_books.assistant.nexus_tools import RELEASE_TOOLS, NexusTools
    from jarvis_books.assistant.permissions import ALWAYS_ASK, READ, SYSTEM, TOOL_LEVELS
    assert set(RELEASE_TOOLS) == {"release_center", "release_propose", "release_decide"}
    assert TOOL_LEVELS["release_center"] == READ and TOOL_LEVELS["release_decide"] == SYSTEM
    assert "release_decide" in ALWAYS_ASK
    tools = NexusTools()
    with pytest.raises(NotFound):
        tools._t_release_center()
    tools.releases = rc
    assert tools._t_release_decide("open_cycle", goals=["Collective"])["version"] == "1.0"
    idea = tools._t_release_propose("Real-data dashboard", "ui", detail="live workers and tasks")
    assert idea["author"] == "jarvis" and idea["cycle"] == "1.0"
    i = make_tested(rc, "Search API v1", "api")
    assert tools._t_release_center()["waiting_for_approval"][0]["id"] == i["id"]
    assert tools._t_release_decide("approve", i["id"])["approver"] == "justin"
    assert tools._t_release_decide("reject", idea["id"], "not now")["state"] == "rejected"
    assert tools._t_release_center("tried", text="real data dashboard")["rejected_before"][0]["why"] == "not now"
    assert tools._t_release_center("memory", idea["id"])["journal"][0]["event"] == "rejected"
    with pytest.raises(ValueError):
        tools._t_release_decide("ship_it")
