"""NEXUS Model Hub: the models this computer has, what each really is (from its own metadata), whether it fits
in memory right now (DeepRAM), and a router that picks the best model that fits a request.

Installing a model: compatibility and disk check from the registry manifest, download through Ollama (which
resumes a partial download and verifies every layer's sha256), then a first answer test and a speed benchmark,
then registration. A model that fails its first answer is kept on disk but never routed to.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from jarvis_books.llm.hardware import available_ram
from jarvis_books.nexus.deepram import GB, DeepRAM, ModelShape
from jarvis_books.storage.db import Database

log = logging.getLogger(__name__)
REGISTRY = "https://registry.ollama.ai/v2"
SMOKE_PROMPT = "Reply with exactly one word: ready"

MIGRATIONS = {1: """
CREATE TABLE models (
    name TEXT PRIMARY KEY, digest TEXT NOT NULL, source TEXT NOT NULL, license TEXT NOT NULL DEFAULT '',
    architecture TEXT NOT NULL DEFAULT '', parameters INTEGER, quantization TEXT NOT NULL DEFAULT '',
    runtimes TEXT NOT NULL DEFAULT '["ollama"]', size_bytes INTEGER NOT NULL, max_context INTEGER,
    shape TEXT NOT NULL DEFAULT '{}', capabilities TEXT NOT NULL DEFAULT '[]', installed INTEGER NOT NULL DEFAULT 1,
    smoke_ok INTEGER, smoke_note TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE benchmarks (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL, prompt_tps REAL, output_tps REAL,
    load_s REAL, context INTEGER, ok INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', at REAL NOT NULL);
CREATE INDEX benchmarks_by_model ON benchmarks(name, id);
CREATE TABLE installs (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '', estimate_bytes INTEGER, started_at REAL NOT NULL, finished_at REAL);
"""}


class HubError(ValueError):
    pass


def source_of(name: str) -> str:
    if name.startswith("hf.co/"):
        return "huggingface:" + name[len("hf.co/"):].split(":")[0]
    return "ollama:" + name.split(":")[0]


def manifest_url(name: str) -> Optional[str]:
    """Registry manifest for an Ollama library model (e.g. qwen3:4b -> library/qwen3, tag 4b)."""
    if name.startswith("hf.co/") or name.count("/") > 1:
        return None
    repo, _, tag = name.partition(":")
    repo = repo if "/" in repo else f"library/{repo}"
    return f"{REGISTRY}/{repo}/manifests/{tag or 'latest'}"


class ModelHub:
    def __init__(self, home: Path | str, client, ram: Optional[DeepRAM] = None,
                 memory: Optional[Callable[[], tuple[Optional[int], Optional[int]]]] = None,
                 http: Optional[requests.Session] = None, clock=time.time,
                 disk_free: Optional[Callable[[], int]] = None):
        self.home = Path(home)
        self.db = Database(self.home / "models.db", migrations=MIGRATIONS)
        self.client, self.ram, self.clock = client, ram or DeepRAM(), clock
        self.memory = memory or _memory
        self.http = http or requests.Session()
        self.disk_free = disk_free or (lambda: shutil.disk_usage(str(self.home)).free)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.db.close()

    # ---- registry of what is installed
    def sync(self) -> list[dict]:
        """Reads every installed model's real metadata (Ollama /api/tags + /api/show) into the registry."""
        now = self.clock()
        seen = set()
        for m in self.client.list_models():
            name = m.get("name") or m.get("model")
            if not name:
                continue
            seen.add(name)
            try:
                show = self.client.show(name)
            except Exception as exc:
                log.info("model hub: couldn't read %s (%s)", name, exc)
                show = {}
            shape = ModelShape.from_ollama(name, show, int(m.get("size") or 0))
            caps = show.get("capabilities") or (["embedding"] if "embed" in name.lower() else ["completion"])
            lic = (show.get("license") or "").strip().splitlines()
            self.db.execute(
                "INSERT INTO models(name, digest, source, license, architecture, parameters, quantization, size_bytes, "
                "max_context, shape, capabilities, installed, first_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(name) DO UPDATE SET digest=excluded.digest, license=excluded.license, "
                "architecture=excluded.architecture, parameters=excluded.parameters, quantization=excluded.quantization, "
                "size_bytes=excluded.size_bytes, max_context=excluded.max_context, shape=excluded.shape, "
                "capabilities=excluded.capabilities, installed=1, updated_at=excluded.updated_at, "
                "smoke_ok=CASE WHEN models.digest=excluded.digest THEN models.smoke_ok END",
                (name, m.get("digest") or "", source_of(name), lic[0][:200] if lic else "", shape.architecture,
                 shape.parameters, shape.quantization or ((m.get("details") or {}).get("quantization_level") or ""),
                 shape.weights_bytes, shape.max_context, json.dumps(shape.__dict__), json.dumps(caps), now, now))
        for row in self.db.query("SELECT name FROM models WHERE installed=1"):
            if row["name"] not in seen:
                self.db.execute("UPDATE models SET installed=0, updated_at=? WHERE name=?", (now, row["name"]))
        return self.models()

    def _model(self, row) -> dict:
        bench = self.db.query_one("SELECT prompt_tps, output_tps, at FROM benchmarks WHERE name=? AND ok=1 "
                                  "ORDER BY id DESC LIMIT 1", (row["name"],))
        return {"name": row["name"], "digest": row["digest"], "source": row["source"], "license": row["license"],
                "architecture": row["architecture"], "parameters": row["parameters"],
                "quantization": row["quantization"], "runtimes": json.loads(row["runtimes"]),
                "size_gb": round(row["size_bytes"] / GB, 2), "max_context": row["max_context"],
                "capabilities": json.loads(row["capabilities"]), "installed": bool(row["installed"]),
                "first_answer_ok": None if row["smoke_ok"] is None else bool(row["smoke_ok"]),
                "output_tps": bench["output_tps"] if bench else None,
                "prompt_tps": bench["prompt_tps"] if bench else None}

    def models(self, installed_only: bool = True) -> list[dict]:
        rows = self.db.query("SELECT * FROM models" + (" WHERE installed=1" if installed_only else "") +
                             " ORDER BY size_bytes DESC")
        return [self._model(r) for r in rows]

    def shape(self, name: str) -> ModelShape:
        row = self.db.query_one("SELECT shape FROM models WHERE name=? AND installed=1", (name,))
        if row is None:
            raise HubError(f"{name} isn't installed")
        return ModelShape(**json.loads(row["shape"]))

    def benchmarks(self, name: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM benchmarks WHERE name=? ORDER BY id DESC LIMIT ?",
                                               (name, limit))]

    # ---- memory
    def _loaded(self) -> tuple[str, ...]:
        try:
            return tuple(m.get("name") or m.get("model") for m in self.client.running())
        except Exception:
            return ()

    def plan(self, name: str, context: int = 4096) -> dict:
        total, free = self.memory()
        plan = self.ram.plan(self.shape(name), context, free, total, name in self._loaded())
        out = plan.to_dict()
        if not plan.fits:
            best = self.ram.largest_context(self.shape(name), free, total, already_loaded=name in self._loaded())
            out["largest_context_that_fits"] = best
        return out

    # ---- install
    def estimate(self, name: str) -> Optional[int]:
        url = manifest_url(name)
        if url is None:
            return None
        try:
            r = self.http.get(url, timeout=20, headers={
                "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
            if r.status_code != 200:
                return None
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        return sum(int(layer.get("size") or 0) for layer in data.get("layers", [])) or None

    def install(self, name: str, context: int = 4096, progress: Callable[[float, str], None] = lambda p, m: None,
                cancel: Optional[Callable[[], bool]] = None) -> dict:
        if not name or len(name) > 200 or any(c.isspace() for c in name):
            raise HubError("give a model name like qwen3:4b")
        with self._lock:
            now = self.clock()
            size = self.estimate(name)
            iid = self.db.execute("INSERT INTO installs(name, state, estimate_bytes, started_at) VALUES (?,?,?,?)",
                                  (name, "checking", size, now)).lastrowid
            try:
                if size is not None:
                    free_disk = self.disk_free()
                    if size * 1.1 + 1 * GB > free_disk:
                        raise HubError(f"{name} needs about {size / GB:.1f} GB of disk; {free_disk / GB:.1f} GB is free")
                    total, free_ram = self.memory()
                    rough = self.ram.plan(ModelShape(name, size), context, total or free_ram, total)
                    if not rough.fits and total is not None:
                        raise HubError(f"{name} is about {size / GB:.1f} GB; with its context and a safety margin it "
                                       f"can't fit in this computer's {total / GB:.0f} GB of memory")
                self._install_state(iid, "downloading", 0.0, "")
                self.client.pull(name, progress=lambda p, m: (self._install_state(iid, "downloading", p, m),
                                                              progress(p * 0.8, m)), cancel=cancel)
                progress(0.85, "reading the model")
                self.sync()
                if not any(m["name"] == name for m in self.models()):
                    raise HubError(f"{name} downloaded but Ollama doesn't list it")
                self._install_state(iid, "testing", 0.9, "first answer test")
                result = self.smoke_test(name)
                state = "installed" if result["ok"] else "failed_test"
                self._install_state(iid, state, 1.0, result["note"], finished=True)
                progress(1.0, state)
                return {"name": name, "state": state, **result, "model": self._model(
                    self.db.query_one("SELECT * FROM models WHERE name=?", (name,)))}
            except Exception as exc:
                self._install_state(iid, "failed", 0.0, str(exc)[:500], finished=True)
                raise

    def _install_state(self, iid: int, state: str, p: float, msg: str, finished: bool = False) -> None:
        self.db.execute("UPDATE installs SET state=?, progress=?, message=?, finished_at=? WHERE id=?",
                        (state, round(p, 3), msg[:300], self.clock() if finished else None, iid))

    def installs(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM installs ORDER BY id DESC LIMIT ?", (limit,))]

    def smoke_test(self, name: str, context: int = 2048) -> dict:
        """A first real answer plus a speed measurement. The plan is checked first so the test can't freeze it."""
        plan = self.plan(name, context)
        digest = self.db.query_one("SELECT digest FROM models WHERE name=?", (name,))["digest"]
        if not plan["fits"]:
            note = "not tested: " + plan["reason"]
            self._bench(name, digest, None, None, None, context, False, note)
            self.db.execute("UPDATE models SET smoke_note=? WHERE name=?", (note, name))
            return {"ok": False, "note": note}
        started = time.perf_counter()
        try:
            reply = self.client.chat({"model": name, "messages": [{"role": "user", "content": SMOKE_PROMPT}],
                                      "think": False, "options": {"num_predict": 16, "num_ctx": context}},
                                     timeout=900)
        except Exception as exc:
            note = f"no answer: {type(exc).__name__}: {exc}"[:300]
            self._bench(name, digest, None, None, None, context, False, note)
            self.db.execute("UPDATE models SET smoke_ok=0, smoke_note=? WHERE name=?", (note, name))
            return {"ok": False, "note": note}
        text = ((reply.get("message") or {}).get("content") or "").strip()
        ok = bool(text)
        p_tps = _tps(reply.get("prompt_eval_count"), reply.get("prompt_eval_duration"))
        o_tps = _tps(reply.get("eval_count"), reply.get("eval_duration"))
        load = (reply.get("load_duration") or 0) / 1e9 or None
        note = f"answered {text[:40]!r}" if ok else "the answer was empty"
        self._bench(name, digest, p_tps, o_tps, load, context, ok, note)
        self.db.execute("UPDATE models SET smoke_ok=?, smoke_note=? WHERE name=?", (int(ok), note, name))
        return {"ok": ok, "note": note, "output_tps": o_tps, "prompt_tps": p_tps,
                "seconds": round(time.perf_counter() - started, 2)}

    def _bench(self, name, digest, p_tps, o_tps, load, context, ok, note) -> None:
        self.db.execute("INSERT INTO benchmarks(name, digest, prompt_tps, output_tps, load_s, context, ok, note, at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (name, digest, p_tps, o_tps, load, context, int(ok), note,
                                                       self.clock()))

    # ---- routing
    def route(self, context: int = 4096, capability: str = "completion", prefer: str = "quality",
              min_output_tps: Optional[float] = None, exclude: tuple[str, ...] = ()) -> dict:
        """Picks the model for a request: installed, passed its first answer, has the capability, fits in memory
        now with this context, and meets the speed floor; then the biggest (quality) or the fastest (speed)."""
        if prefer not in ("quality", "speed"):
            raise HubError("prefer is quality or speed")
        total, free = self.memory()
        loaded = self._loaded()
        chosen, considered = None, []
        candidates = []
        for m in self.models():
            why = []
            if m["name"] in exclude:
                why.append("excluded")
            if capability not in m["capabilities"]:
                why.append(f"can't do {capability}")
            if m["first_answer_ok"] is False:
                why.append("failed its first answer test")
            if m["max_context"] and context > m["max_context"]:
                why.append(f"context is at most {m['max_context']}")
            if min_output_tps and (m["output_tps"] or 0) < min_output_tps:
                why.append(f"measured {m['output_tps'] or 'no'} tokens/s, need {min_output_tps:g}")
            plan = self.ram.plan(self.shape(m["name"]), context, free, total, m["name"] in loaded)
            if not plan.fits:
                why.append(plan.reason)
            considered.append({"name": m["name"], "ok": not why, "why": why})
            if not why:
                candidates.append(m)
        if candidates:
            key = (lambda m: (m["parameters"] or 0, m["output_tps"] or 0)) if prefer == "quality" else \
                (lambda m: (m["output_tps"] or 0, -(m["parameters"] or 0)))
            chosen = max(candidates, key=key)["name"]
        return {"model": chosen, "context": context, "prefer": prefer, "considered": considered,
                "advice": None if chosen else "no installed model fits now: use a smaller or more compressed model, "
                                              "a shorter context, the cloud brain, or another Collective computer"}


def _tps(count, duration_ns) -> Optional[float]:
    if not count or not duration_ns:
        return None
    return round(count / (duration_ns / 1e9), 2)


def _memory() -> tuple[Optional[int], Optional[int]]:
    import sys
    if sys.platform == "win32":
        from jarvis_books.llm.hardware import _windows_memory
        return _windows_memory()
    try:
        from jarvis_books.llm.hardware import parse_meminfo
        return parse_meminfo(Path("/proc/meminfo").read_text())
    except OSError:
        return None, available_ram()
