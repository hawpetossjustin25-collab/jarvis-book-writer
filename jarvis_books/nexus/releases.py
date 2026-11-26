"""NEXUS Release Center: Jarvis's permanent improvement memory and release system.

- Versions: what is really running (the git commit the installer wrote to version.json), on which channel, with
  the test result it was installed with, and every rollback with its reason.
- Cycles: a major release about every four months (1.0, 1.1, ... by default). Each cycle has objectives and the
  improvements planned for it; an improvement can move to another cycle. Urgent fixes and security patches are
  released on their own and never wait for a cycle.
- Improvements: proposed -> researching -> building -> testing -> tested -> approved -> released (or rejected or
  deferred), each with who proposed it (Jarvis's self-upgrader, his search self-improvement, Justin, Devin) and
  who approved it. A release needs a passing full test run and a comparison that is no worse than the current
  version. Stable releases need a person's approval; an AI can't approve its own work.
- Memory: every proposal, experiment, decision and outcome is kept in an append-only journal. The database refuses
  to edit or delete journal rows, so a newer agent can't quietly erase why something was built or rejected. A new
  proposal that matches one already rejected is flagged with the old reasons.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.errors import NotFound
from jarvis_books.storage.db import Database

CHANNELS = ("development", "internal", "preview", "staged", "stable", "emergency")
STATES = ("proposed", "researching", "building", "testing", "tested", "approved", "released", "rejected", "deferred")
KINDS = ("feature", "fix", "security", "performance", "quality", "docs")
AREAS = ("jarvis", "books", "voice", "phone", "search", "os1", "os2", "os3", "api", "collective", "models",
         "security", "ui", "research", "computer", "upgrades", "platform")
VERDICTS = ("better", "same", "worse", "failed")
HUMANS = ("justin",)
CYCLE_DAYS = 122  # about four months
NEXT = {"proposed": {"researching", "building", "rejected", "deferred"},
        "researching": {"building", "rejected", "deferred"},
        "building": {"testing", "rejected", "deferred"},
        "testing": {"tested", "building", "rejected"},
        "tested": {"approved", "building", "rejected", "deferred"},
        "approved": {"released", "building", "rejected"},
        "released": set(), "rejected": {"proposed"}, "deferred": {"proposed"}}
PRIVATE = re.compile(r"(key|token|secret|password|credential|api[_ -]?key|\\users\\|/home/|appdata|\b\d{1,3}(\.\d{1,3}){3}\b)",
                     re.I)

MIGRATIONS = {1: """
CREATE TABLE versions (
    id INTEGER PRIMARY KEY, version TEXT NOT NULL, commit_sha TEXT NOT NULL, channel TEXT NOT NULL,
    source TEXT NOT NULL, tests TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL, ended_at REAL);
CREATE UNIQUE INDEX versions_commit ON versions(commit_sha, channel);
CREATE TABLE cycles (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, version TEXT NOT NULL UNIQUE, starts REAL NOT NULL,
    ends REAL NOT NULL, objectives TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'planning',
    notes TEXT NOT NULL DEFAULT '');
CREATE TABLE improvements (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', area TEXT NOT NULL,
    kind TEXT NOT NULL, urgent INTEGER NOT NULL DEFAULT 0, cycle_id INTEGER REFERENCES cycles(id),
    state TEXT NOT NULL, author TEXT NOT NULL, approver TEXT, origin TEXT UNIQUE, fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, released_in TEXT);
CREATE TABLE experiments (
    id INTEGER PRIMARY KEY, improvement_id INTEGER NOT NULL REFERENCES improvements(id), kind TEXT NOT NULL,
    baseline TEXT NOT NULL DEFAULT '{}', candidate TEXT NOT NULL DEFAULT '{}', verdict TEXT NOT NULL,
    reason TEXT NOT NULL, by TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE releases (
    id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE, channel TEXT NOT NULL, commit_sha TEXT NOT NULL,
    cycle_id INTEGER REFERENCES cycles(id), approver TEXT, public_notes TEXT NOT NULL, private_notes TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, verified_at REAL);
CREATE TABLE rollbacks (
    id INTEGER PRIMARY KEY, from_version TEXT NOT NULL, to_version TEXT NOT NULL, reason TEXT NOT NULL,
    by TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE journal (
    id INTEGER PRIMARY KEY, subject TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL, by TEXT NOT NULL,
    at REAL NOT NULL);
CREATE INDEX journal_subject ON journal(subject, id);
CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'the journal is permanent'); END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT, 'the journal is permanent'); END;
CREATE TRIGGER experiments_no_update BEFORE UPDATE ON experiments
BEGIN SELECT RAISE(ABORT, 'experiment results are permanent'); END;
CREATE TRIGGER experiments_no_delete BEFORE DELETE ON experiments
BEGIN SELECT RAISE(ABORT, 'experiment results are permanent'); END;
CREATE TRIGGER rollbacks_no_delete BEFORE DELETE ON rollbacks BEGIN SELECT RAISE(ABORT, 'rollbacks are permanent'); END;
"""}


class ReleaseError(ValueError):
    pass


def fingerprint(title: str) -> str:
    words = sorted({w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2})
    return " ".join(words)


def similarity(a: str, b: str) -> float:
    x, y = set(a.split()), set(b.split())
    return len(x & y) / len(x | y) if x and y else 0.0


def public_text(text: str) -> str:
    """Release notes for everyone: lines that mention keys, private paths or addresses are left out."""
    return "\n".join(line for line in text.splitlines() if not PRIVATE.search(line)).strip()


@dataclass
class Running:
    commit: str
    channel: str
    source: str
    installed_at: Optional[float]
    tests: dict


def read_running(app_root: Path, run: Callable = subprocess.run) -> Optional[Running]:
    """What is installed: version.json written by the installer, or the git checkout this runs from."""
    meta = app_root / "version.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8-sig"))
            commit = str(data["commit"]).strip().lower()
            if re.fullmatch(r"[0-9a-f]{7,40}", commit):
                return Running(commit, data.get("channel") or "stable", "installer", data.get("installed_at"),
                               data.get("tests") or {})
        except (ValueError, KeyError, OSError):
            pass
    if (app_root / ".git").exists():
        try:
            out = run(["git", "-C", str(app_root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
            commit = out.stdout.strip()
            if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", commit):
                return Running(commit, "development", "git", None, {})
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class ReleaseCenter:
    def __init__(self, home: Path | str, clock=time.time, first_version: str = "1.0"):
        self.db = Database(Path(home) / "releases.db", migrations=MIGRATIONS)
        self.clock = clock
        self.first_version = first_version

    def close(self) -> None:
        self.db.close()

    def _note(self, subject: str, event: str, detail, by: str) -> None:
        self.db.execute("INSERT INTO journal(subject, event, detail, by, at) VALUES (?,?,?,?,?)",
                        (subject, event, detail if isinstance(detail, str) else json.dumps(detail), by, self.clock()))

    def journal(self, subject: str = "", limit: int = 50) -> list[dict]:
        q = "SELECT * FROM journal" + (" WHERE subject=?" if subject else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.db.query(q, ((subject, limit) if subject else (limit,)))]

    # ---- what is running
    def observe(self, running: Optional[Running], version: str = "") -> dict:
        """Records the version that is really running. A new commit ends the previous one's run."""
        if running is None:
            return {"version": None, "note": "this copy of Jarvis doesn't know its commit (no version.json or git)"}
        row = self.db.query_one("SELECT * FROM versions WHERE commit_sha=? AND channel=?",
                                (running.commit, running.channel))
        if row is None:
            now = self.clock()
            with self.db.transaction() as c:
                c.execute("UPDATE versions SET status='superseded', ended_at=? WHERE status='running'", (now,))
                c.execute("INSERT INTO versions(version, commit_sha, channel, source, tests, status, started_at) "
                          "VALUES (?,?,?,?,?,?,?)", (version or running.commit[:7], running.commit, running.channel,
                                                     running.source, json.dumps(running.tests), "running",
                                                     running.installed_at or now))
            self._note(f"version:{running.commit[:12]}", "running", {"channel": running.channel,
                                                                     "tests": running.tests}, "installer")
            for rel in self.db.query("SELECT version FROM releases WHERE commit_sha=? AND status='packaged'",
                                     (running.commit,)):
                self.db.execute("UPDATE releases SET status='deployed', verified_at=? WHERE version=?",
                                (self.clock(), rel["version"]))
                self._note(f"release:{rel['version']}", "deployed", "the running commit matches this release",
                           "release-center")
            row = self.db.query_one("SELECT * FROM versions WHERE commit_sha=? AND channel=?",
                                    (running.commit, running.channel))
        elif row["status"] != "running":
            self.db.execute("UPDATE versions SET status='superseded', ended_at=? WHERE status='running'",
                            (self.clock(),))
            self.db.execute("UPDATE versions SET status='running', ended_at=NULL WHERE id=?", (row["id"],))
        return self._version(self.db.query_one("SELECT * FROM versions WHERE id=?", (row["id"],)))

    def _version(self, row) -> dict:
        return {"version": row["version"], "commit": row["commit_sha"], "channel": row["channel"],
                "source": row["source"], "tests": json.loads(row["tests"]), "status": row["status"],
                "since": row["started_at"]}

    def current(self) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM versions WHERE status='running' ORDER BY id DESC LIMIT 1")
        return self._version(row) if row else None

    # ---- cycles
    def cycle(self, version: str = "", objectives: Optional[list[str]] = None, starts: Optional[float] = None,
              by: str = "justin") -> dict:
        """Opens the next four-month cycle (or a named one)."""
        last = self.db.query_one("SELECT * FROM cycles ORDER BY ends DESC LIMIT 1")
        if not version:
            version = self.first_version if last is None else _bump(last["version"])
        if not re.fullmatch(r"\d+\.\d+", version):
            raise ReleaseError("a major release version looks like 1.1 or 2.0")
        start = starts if starts is not None else (last["ends"] if last else self.clock())
        ends = (datetime.fromtimestamp(start) + timedelta(days=CYCLE_DAYS)).timestamp()
        name = f"NEXUS {version}"
        try:
            cid = self.db.execute("INSERT INTO cycles(name, version, starts, ends, objectives) VALUES (?,?,?,?,?)",
                                  (name, version, start, ends, json.dumps(objectives or []))).lastrowid
        except Exception as exc:
            raise ReleaseError(f"{name} already exists") from exc
        self._note(f"cycle:{version}", "opened", {"objectives": objectives or []}, by)
        return self.cycle_info(cid)

    def active_cycle(self) -> Optional[dict]:
        now = self.clock()
        row = self.db.query_one("SELECT id FROM cycles WHERE starts<=? AND ends>? AND status!='released' "
                                "ORDER BY starts LIMIT 1", (now, now)) or \
            self.db.query_one("SELECT id FROM cycles WHERE status!='released' ORDER BY starts LIMIT 1")
        return self.cycle_info(row["id"]) if row else None

    def cycle_info(self, cycle_id: int) -> dict:
        c = self.db.query_one("SELECT * FROM cycles WHERE id=?", (cycle_id,))
        if c is None:
            raise NotFound(f"no release cycle #{cycle_id}")
        items = [self.improvement(r["id"]) for r in self.db.query(
            "SELECT id FROM improvements WHERE cycle_id=? ORDER BY id", (cycle_id,))]
        counts = {s: sum(1 for i in items if i["state"] == s) for s in STATES}
        return {"id": c["id"], "name": c["name"], "version": c["version"], "status": c["status"],
                "starts": c["starts"], "ends": c["ends"],
                "days_left": max(0, round((c["ends"] - self.clock()) / 86400)),
                "objectives": json.loads(c["objectives"]), "counts": {k: v for k, v in counts.items() if v},
                "improvements": items}

    # ---- improvements
    def propose(self, title: str, area: str, kind: str = "feature", detail: str = "", author: str = "jarvis",
                urgent: bool = False, origin: Optional[str] = None, cycle_version: str = "",
                despite_rejection: str = "") -> dict:
        title = " ".join(title.split())
        if not 4 <= len(title) <= 200:
            raise ReleaseError("give the improvement a short title (4-200 characters)")
        if area not in AREAS:
            raise ReleaseError(f"area is one of: {', '.join(AREAS)}")
        if kind not in KINDS:
            raise ReleaseError(f"kind is one of: {', '.join(KINDS)}")
        if origin:
            found = self.db.query_one("SELECT id FROM improvements WHERE origin=?", (origin,))
            if found:
                return self.improvement(found["id"])
        fp = fingerprint(title)
        before = self.tried(title)
        if before and not despite_rejection:
            reasons = "; ".join(f"#{b['id']} {b['title']}: {b['why']}" for b in before[:3])
            raise ReleaseError(f"this was already tried and rejected ({reasons}). Say what is different to try again.")
        cycle_id = None
        if not urgent and kind not in ("security",):
            c = (self.db.query_one("SELECT id FROM cycles WHERE version=?", (cycle_version,)) if cycle_version
                 else None) or self.active_cycle()
            cycle_id = c["id"] if c else None
        now = self.clock()
        iid = self.db.execute(
            "INSERT INTO improvements(title, detail, area, kind, urgent, cycle_id, state, author, origin, fingerprint, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, detail, area, kind, int(urgent or kind == "security"), cycle_id, "proposed", author, origin, fp,
             now, now)).lastrowid
        self._note(f"improvement:{iid}", "proposed", {"title": title, "area": area, "kind": kind,
                                                      "despite_rejection": despite_rejection or None}, author)
        return self.improvement(iid)

    def tried(self, title: str, threshold: float = 0.6) -> list[dict]:
        """Rejected improvements like this one, with the reason they were rejected."""
        fp = fingerprint(title)
        out = []
        for r in self.db.query("SELECT id, title, fingerprint FROM improvements WHERE state='rejected'"):
            if similarity(fp, r["fingerprint"]) >= threshold:
                why = self.db.query_one("SELECT detail FROM journal WHERE subject=? AND event='rejected' "
                                        "ORDER BY id DESC LIMIT 1", (f"improvement:{r['id']}",))
                out.append({"id": r["id"], "title": r["title"], "why": why["detail"] if why else ""})
        return out

    def improvement(self, iid: int) -> dict:
        r = self.db.query_one("SELECT * FROM improvements WHERE id=?", (iid,))
        if r is None:
            raise NotFound(f"no improvement #{iid}")
        cyc = self.db.query_one("SELECT version FROM cycles WHERE id=?", (r["cycle_id"],)) if r["cycle_id"] else None
        exps = [dict(e, baseline=json.loads(e["baseline"]), candidate=json.loads(e["candidate"]))
                for e in self.db.query("SELECT * FROM experiments WHERE improvement_id=? ORDER BY id", (iid,))]
        return {"id": r["id"], "title": r["title"], "detail": r["detail"], "area": r["area"], "kind": r["kind"],
                "urgent": bool(r["urgent"]), "cycle": cyc["version"] if cyc else None, "state": r["state"],
                "author": r["author"], "approver": r["approver"], "released_in": r["released_in"],
                "experiments": exps}

    def improvements(self, state: str = "", limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT id FROM improvements" + (" WHERE state=?" if state else "") +
                             " ORDER BY id DESC LIMIT ?", ((state, limit) if state else (limit,)))
        return [self.improvement(r["id"]) for r in rows]

    def advance(self, iid: int, state: str, by: str, reason: str = "") -> dict:
        cur = self.improvement(iid)
        if state not in STATES:
            raise ReleaseError(f"state is one of: {', '.join(STATES)}")
        if state not in NEXT[cur["state"]]:
            raise ReleaseError(f"#{iid} is {cur['state']}; it can go to {', '.join(sorted(NEXT[cur['state']]))}")
        if state in ("rejected", "deferred") and not reason:
            raise ReleaseError("say why, so it is remembered")
        if state == "tested":
            self._require_evidence(cur)
        if state == "approved":
            return self.approve(iid, by)
        self.db.execute("UPDATE improvements SET state=?, updated_at=? WHERE id=?", (state, self.clock(), iid))
        self._note(f"improvement:{iid}", state, reason or state, by)
        return self.improvement(iid)

    def _require_evidence(self, cur: dict) -> None:
        exps = cur["experiments"]
        suite = [e for e in exps if e["kind"] == "test_suite"]
        if not suite or suite[-1]["verdict"] == "failed":
            raise ReleaseError("it needs a passing full test run first")
        compared = [e for e in exps if e["kind"] != "test_suite"]
        if compared and compared[-1]["verdict"] == "worse":
            raise ReleaseError("its last comparison measured worse than the current version")

    def record(self, iid: int, kind: str, verdict: str, reason: str, by: str,
               baseline: Optional[dict] = None, candidate: Optional[dict] = None) -> dict:
        """One experiment result (a full test run, a benchmark, a search-quality exam ...). Permanent."""
        self.improvement(iid)
        if verdict not in VERDICTS:
            raise ReleaseError(f"verdict is one of: {', '.join(VERDICTS)}")
        if not reason:
            raise ReleaseError("an experiment needs its reason or measurement")
        self.db.execute("INSERT INTO experiments(improvement_id, kind, baseline, candidate, verdict, reason, by, at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (iid, kind, json.dumps(baseline or {}),
                                                     json.dumps(candidate or {}), verdict, reason, by, self.clock()))
        self._note(f"improvement:{iid}", f"experiment:{kind}", {"verdict": verdict, "reason": reason}, by)
        return self.improvement(iid)

    def approve(self, iid: int, by: str) -> dict:
        cur = self.improvement(iid)
        if cur["state"] != "tested":
            raise ReleaseError(f"#{iid} is {cur['state']}; only tested improvements can be approved")
        if by not in HUMANS:
            raise ReleaseError("only Justin can approve a change for release")
        if by == cur["author"]:
            raise ReleaseError("the author can't approve their own change")
        self._require_evidence(cur)
        self.db.execute("UPDATE improvements SET state='approved', approver=?, updated_at=? WHERE id=?",
                        (by, self.clock(), iid))
        self._note(f"improvement:{iid}", "approved", "approved for release", by)
        return self.improvement(iid)

    def move(self, iid: int, cycle_version: str, by: str, reason: str) -> dict:
        cur = self.improvement(iid)
        if cur["state"] == "released":
            raise ReleaseError("it is already released")
        c = self.db.query_one("SELECT id FROM cycles WHERE version=?", (cycle_version,))
        if c is None:
            raise NotFound(f"no release cycle {cycle_version}")
        if not reason:
            raise ReleaseError("say why it moves")
        self.db.execute("UPDATE improvements SET cycle_id=?, updated_at=? WHERE id=?", (c["id"], self.clock(), iid))
        self._note(f"improvement:{iid}", "moved", {"from": cur["cycle"], "to": cycle_version, "why": reason}, by)
        return self.improvement(iid)

    # ---- releases
    def package(self, commit: str, channel: str, by: str, version: str = "", notes: str = "") -> dict:
        """Bundles the approved improvements into a release of `commit`. It counts as shipped only once the
        running version is seen on that commit (observe)."""
        if channel not in CHANNELS:
            raise ReleaseError(f"channel is one of: {', '.join(CHANNELS)}")
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
            raise ReleaseError("a release is built from a git commit")
        if channel in ("staged", "stable") and by not in HUMANS:
            raise ReleaseError("a stable or staged release needs Justin's approval")
        approved = self.improvements("approved", 500)
        cyc = self.active_cycle()
        if channel == "emergency":
            items = [i for i in approved if i["urgent"]]
        else:
            items = approved
        if not items:
            raise ReleaseError("nothing is approved for release yet")
        if not version:
            version = self._next_version(channel, cyc, items)
        private = "\n".join([f"Release {version} ({channel}) from {commit[:12]}"] +
                            [f"- [{i['area']}] {i['title']} (#{i['id']}, by {i['author']}, approved by "
                             f"{i['approver']}): {i['detail']}" for i in items] + ([notes] if notes else []))
        public = public_text("\n".join([f"NEXUS {version}"] + [f"- {i['title']}" for i in items] +
                                       ([notes] if notes else [])))
        now = self.clock()
        try:
            with self.db.transaction() as c:
                c.execute("INSERT INTO releases(version, channel, commit_sha, cycle_id, approver, public_notes, "
                          "private_notes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (version, channel, commit, cyc["id"] if cyc and channel != "emergency" else None, by,
                           public, private, "packaged", now))
                for i in items:
                    c.execute("UPDATE improvements SET state='released', released_in=?, updated_at=? WHERE id=?",
                              (version, now, i["id"]))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ReleaseError(f"release {version} already exists") from exc
            raise
        self._note(f"release:{version}", "packaged", {"channel": channel, "commit": commit,
                                                      "improvements": [i["id"] for i in items]}, by)
        if channel == "stable" and cyc and all(i["state"] in ("released", "rejected", "deferred")
                                               for i in self.cycle_info(cyc["id"])["improvements"]):
            self.db.execute("UPDATE cycles SET status='released' WHERE id=?", (cyc["id"],))
        return self.release(version)

    def _next_version(self, channel: str, cyc: Optional[dict], items: list[dict]) -> str:
        base = cyc["version"] if cyc else self.first_version
        if channel in ("stable", "staged") and not self.db.query_one(
                "SELECT 1 FROM releases WHERE version=?", (f"{base}.0",)):
            return f"{base}.0"
        patches = [r["version"] for r in self.db.query("SELECT version FROM releases WHERE version LIKE ?",
                                                       (f"{base}.%",))]
        n = max([int(v.split(".")[2].split("-")[0]) for v in patches if v.count(".") >= 2] + [0]) + 1
        suffix = {"development": "-dev", "internal": "-internal", "preview": "-preview"}.get(channel, "")
        return f"{base}.{n}{suffix}"

    def release(self, version: str) -> dict:
        r = self.db.query_one("SELECT * FROM releases WHERE version=?", (version,))
        if r is None:
            raise NotFound(f"no release {version}")
        return {"version": r["version"], "channel": r["channel"], "commit": r["commit_sha"], "status": r["status"],
                "approver": r["approver"], "public_notes": r["public_notes"], "private_notes": r["private_notes"],
                "created_at": r["created_at"], "verified_at": r["verified_at"]}

    def releases(self, public: bool = False, limit: int = 30) -> list[dict]:
        rows = self.db.query("SELECT version FROM releases ORDER BY id DESC LIMIT ?", (limit,))
        out = [self.release(r["version"]) for r in rows]
        if public:
            out = [{"version": r["version"], "notes": r["public_notes"], "shipped": r["verified_at"]}
                   for r in out if r["status"] == "deployed" and r["channel"] in ("stable", "emergency")]
        return out

    def rollback(self, to_commit: str, reason: str, by: str) -> dict:
        """Records a rollback. The reinstall itself is done by the installer; observe() then sees the old commit."""
        if not reason:
            raise ReleaseError("a rollback needs its reason")
        cur = self.current()
        if cur is None:
            raise ReleaseError("the running version isn't known")
        target = self.db.query_one("SELECT * FROM versions WHERE commit_sha LIKE ? ORDER BY id DESC LIMIT 1",
                                   (to_commit + "%",))
        if target is None:
            raise NotFound(f"{to_commit} never ran here, so it can't be the version to go back to")
        self.db.execute("INSERT INTO rollbacks(from_version, to_version, reason, by, at) VALUES (?,?,?,?,?)",
                        (cur["commit"], target["commit_sha"], reason, by, self.clock()))
        for r in self.db.query("SELECT version FROM releases WHERE commit_sha=?", (cur["commit"],)):
            self.db.execute("UPDATE releases SET status='rolled_back' WHERE version=?", (r["version"],))
        self._note(f"version:{cur['commit'][:12]}", "rollback", {"to": target["commit_sha"], "why": reason}, by)
        return {"from": cur["commit"], "to": target["commit_sha"], "reason": reason}

    def rollbacks(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM rollbacks ORDER BY id DESC LIMIT ?", (limit,))]

    # ---- the self-improvement systems feed the memory
    def ingest_upgrades(self, upgrades: list[dict]) -> int:
        """Jarvis's self-upgrades: each becomes an improvement with its test runs, keyed so a re-read adds nothing."""
        added = 0
        for up in sorted(upgrades, key=lambda u: u["id"]):
            origin = f"upgrade:{up['id']}"
            known = self.db.query_one("SELECT id, state FROM improvements WHERE origin=?", (origin,))
            if known is None:
                title = (up.get("summary") or up["request"])[:200]
                if len(title) < 4:
                    title = f"self-upgrade {up['id']}"
                iid = self.propose(title, "upgrades", "feature", up["request"][:2000], "jarvis:self-upgrade",
                                   origin=origin, despite_rejection="re-requested")["id"]
                added += 1
                state = "proposed"
            else:
                iid, state = known["id"], known["state"]
            status = up.get("status")
            if status in ("ready", "installed", "undone") and state in ("proposed", "building"):
                self._force(iid, "testing", "jarvis:self-upgrade")
                self.record(iid, "test_suite", "better", "the full test suite passed on a copy before installing",
                            "jarvis:self-upgrade", candidate={"files": up.get("files", [])})
                self._force(iid, "tested", "jarvis:self-upgrade")
                state = "tested"
            if status == "undone" and state != "rejected":
                self._force(iid, "rejected", "justin", "the installed upgrade was undone")
            elif status == "failed" and state not in ("rejected", "released"):
                self._force(iid, "rejected", "jarvis:self-upgrade",
                            (up.get("test_output") or "it never passed the tests")[-400:])
        return added

    def ingest_rsi(self, history: list[dict]) -> int:
        """OS1 search/crawl self-improvement rounds: kept, rejected and rolled-back settings with their scores."""
        added = 0
        for v in sorted(history, key=lambda h: h["id"]):
            origin = f"rsi:{v['id']}"
            if self.db.query_one("SELECT 1 FROM improvements WHERE origin=?", (origin,)):
                continue
            metrics = v.get("metrics") if isinstance(v.get("metrics"), dict) else json.loads(v.get("metrics") or "{}")
            iid = self.propose(f"{v['kind']} settings version {v['id']}", "search", "performance",
                               json.dumps(v.get("params")), "jarvis:rsi", origin=origin,
                               despite_rejection="new measured settings")["id"]
            added += 1
            status = v.get("status")
            verdict = {"champion": "better", "retired": "better", "rejected": "worse", "trial": "same"}.get(status,
                                                                                                         "same")
            self._force(iid, "testing", "jarvis:rsi")
            self.record(iid, "search_exam", verdict, v.get("reason") or status or "", "jarvis:rsi",
                        candidate=metrics)
            if status == "rejected":
                self._force(iid, "rejected", "jarvis:rsi", v.get("reason") or "measured worse")
        return added

    def _force(self, iid: int, state: str, by: str, reason: str = "") -> None:
        self.db.execute("UPDATE improvements SET state=?, updated_at=? WHERE id=?", (state, self.clock(), iid))
        self._note(f"improvement:{iid}", state, reason or state, by)

    # ---- the Release Center view
    def center(self) -> dict:
        cur = self.current()
        last_major = self.db.query_one("SELECT version, verified_at FROM releases WHERE channel='stable' AND "
                                       "status='deployed' ORDER BY id DESC LIMIT 1")
        cyc = self.active_cycle()
        nxt = self.db.query_one("SELECT version, ends FROM cycles WHERE status!='released' ORDER BY starts LIMIT 1")
        return {"running": cur, "last_major_release": dict(last_major) if last_major else None,
                "next_major_release": {"version": nxt["version"], "target": nxt["ends"]} if nxt else None,
                "cycle": {k: cyc[k] for k in ("name", "days_left", "objectives", "counts")} if cyc else None,
                "proposed": len(self.improvements("proposed", 500)), "waiting_for_approval": [
                    {"id": i["id"], "title": i["title"]} for i in self.improvements("tested", 50)],
                "approved": [{"id": i["id"], "title": i["title"]} for i in self.improvements("approved", 50)],
                "recent_releases": [{k: r[k] for k in ("version", "channel", "status")} for r in self.releases()[:5]],
                "rollbacks": self.rollbacks(5)}


def _bump(version: str) -> str:
    major, minor = (int(x) for x in version.split("."))
    return f"{major + 1}.0" if minor >= 2 else f"{major}.{minor + 1}"
