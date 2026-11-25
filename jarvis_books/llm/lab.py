"""Jarvis's Brain Lab: recursive self-improvement of his own brain on this computer, without a graphics card.

Each round: read the computer's specs and set targets from them, now and then search research papers for
ideas, change one part of the current best setup (the "champion"), measure the new version on fixed tests
(tool calls, coding tasks checked by unit tests, a long step-by-step project, reply speed, memory), and keep it
only if it scores better and is no less safe. Otherwise it is thrown away. Rounds repeat for as long as RSI
mode is on; once every target is met they slow down but never stop, so a better setup is still picked up.

What it changes is configuration and trained weights (the quick tool brain's size and training, which small
model answers on this computer and how it runs). Changes to Jarvis's own code still go through the upgrader,
which must pass the whole test suite first."""
from __future__ import annotations

import json
import logging
import random
import shutil
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.llm import sandbox
from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.specs import describe, probe_specs
from jarvis_books.llm.tool_brain import ToolBrainEngine, train_tool_brain
from jarvis_books.llm.tool_eval import load_cases, run_tool_eval

log = logging.getLogger(__name__)
DATA = Path(__file__).with_name("data")
SEALED_PATH = DATA / "tool_sealed.jsonl"
CODING_PATH = DATA / "coding_tasks.jsonl"
LONG_PATH = DATA / "long_task.json"
GB = 1024 ** 3


@dataclass(frozen=True)
class Model:
    name: str
    label: str
    approx_gb: float  # download size; Ollama's measured RAM is used once it has run


MODELS = (Model("hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M", "LFM3 1.7B", 1.1),
          Model("hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M", "Qwen3.5 0.8B", 0.53),
          Model("hf.co/unsloth/Qwen3.5-2B-GGUF:Q4_K_M", "Qwen3.5 2B", 1.28),
          Model("qwen2.5-coder:1.5b", "Qwen2.5-Coder 1.5B", 0.99),
          Model("qwen3:4b-instruct-2507-q4_K_M", "Qwen3 4B", 2.5))
LABELS = {m.name: m.label for m in MODELS}
TOOL_GENES = ("tool_hidden", "tool_per_template", "tool_epochs", "tool_confidence")
MODEL_GENES = ("model", "num_ctx", "num_thread", "temperature")
BASELINE = {"tool_hidden": 64, "tool_per_template": 30, "tool_epochs": 10, "tool_confidence": 0.9,
            "model": MODELS[0].name, "num_ctx": 4096, "num_thread": 0, "temperature": 0.2}
# Which part of the brain an idea from a paper suggests changing.
IDEA_RULES = ((("quantiz", "low-bit", "1-bit", "1.58", "ternary", "small language model", "on-device", "edge"),
               ("model",)),
              (("context", "kv cache", "long-context"), ("num_ctx",)),
              (("thread", "simd", "cpu inference", "parallel"), ("num_thread",)),
              (("distill", "synthetic data", "augment"), ("tool_per_template", "tool_epochs")),
              (("tool", "function call", "agent"), ("tool_confidence", "tool_hidden")),
              (("sampling", "temperature", "decoding"), ("temperature",)))
PAPER_QUERIES = ("small language model CPU inference", "ternary 1.58-bit LLM CPU", "on-device function calling",
                 "speculative decoding CPU", "next-concept modeling", "small model code generation",
                 "knowledge distillation tool use small model", "efficient long-context small language model")
WEIGHTS = {"tool_accuracy": 3, "tool_speed": 1, "coding": 3, "long_task": 2, "first_reply": 2, "writing": 2,
           "model_tools": 2}
RESEARCH_EVERY = 4
MET_PACE_S = 6 * 3600
HISTORY = 200


def targets_for(specs: dict) -> dict:
    """What "works on this computer" means, from its own specs."""
    ram = specs.get("ram_gb") or 8.0
    return {"tool_accuracy": 0.95, "tool_ms": 100.0, "coding": 0.8, "long_task": 0.75, "first_reply_s": 2.0,
            "writes_per_s": 10.0, "model_tools": 0.6, "ram_gb": round(min(6.0, ram * 0.4), 1)}


def gene_choices(specs: dict, ram_budget_gb: float) -> dict:
    threads = specs.get("threads") or 2
    cores = specs.get("cores") or threads
    return {"tool_hidden": [32, 48, 64, 96, 128], "tool_per_template": [20, 30, 45, 60],
            "tool_epochs": [6, 8, 10, 14], "tool_confidence": [0.8, 0.85, 0.9, 0.95],
            "model": [m.name for m in MODELS if m.approx_gb * 1.6 <= ram_budget_gb],
            "num_ctx": [2048, 4096, 8192], "num_thread": sorted({0, cores, threads}),
            "temperature": [0.0, 0.2, 0.4]}


def utility(m: dict, t: dict) -> float:
    """One number for how close a version is to every target (1.0 = all met). Speed counts in its own right,
    capped at the target so nothing is gained by being faster than needed."""
    parts = {"tool_accuracy": m.get("tool_accuracy", 0) / t["tool_accuracy"],
             "tool_speed": t["tool_ms"] / max(m.get("tool_ms", 1e9), 1e-6),
             "coding": m.get("coding", 0) / t["coding"], "long_task": m.get("long_task", 0) / t["long_task"],
             "first_reply": t["first_reply_s"] / max(m.get("first_reply_s", 1e9), 1e-6),
             "writing": m.get("writes_per_s", 0) / t["writes_per_s"],
             "model_tools": m.get("model_tools", 0) / t["model_tools"]}
    return round(sum(WEIGHTS[k] * min(1.0, v) for k, v in parts.items()) / sum(WEIGHTS.values()), 4)


def unmet(m: dict, t: dict) -> list[str]:
    checks = (("tool_accuracy", m.get("tool_accuracy", 0) >= t["tool_accuracy"]),
              ("tool_ms", m.get("tool_ms", 1e9) <= t["tool_ms"]), ("coding", m.get("coding", 0) >= t["coding"]),
              ("long_task", m.get("long_task", 0) >= t["long_task"]),
              ("first_reply_s", m.get("first_reply_s", 1e9) <= t["first_reply_s"]),
              ("writes_per_s", m.get("writes_per_s", 0) >= t["writes_per_s"]),
              ("model_tools", m.get("model_tools", 0) >= t["model_tools"]),
              ("ram_gb", m.get("ram_gb", 1e9) <= t["ram_gb"]))
    return [k for k, ok in checks if not ok]


def load_coding() -> list[dict]:
    return [json.loads(line) for line in CODING_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


class Stopped(Exception):
    pass


class BrainLab:
    """One lab per app. `measure_model_tools(model, options)` scores a local model on tool calls (the Studio
    gives it the real tool list); `apply(genome)` puts a new champion to work."""

    def __init__(self, folder: Path, client, router, declarations: Callable[[], list[dict]],
                 settings: Callable[[], dict], web=None,
                 measure_model_tools: Optional[Callable[[str, dict], float]] = None,
                 apply: Optional[Callable[[dict], None]] = None,
                 specs_probe: Callable[[], dict] = probe_specs, clock: Callable[[], float] = time.time,
                 seed: Optional[int] = None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "lab.json"
        self.client, self.router, self.declarations, self.settings = client, router, declarations, settings
        self.web, self.measure_model_tools, self.apply = web, measure_model_tools, apply
        self.specs_probe, self.clock = specs_probe, clock
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.running = False
        self._step = ""
        self.problem = ""
        self.state = self._load()

    @property
    def step(self) -> str:
        return self._step

    @step.setter
    def step(self, what: str) -> None:
        self._step = what
        if what:
            log.info("brain lab round %s: %s", self.state["rounds"] + 1, what)

    # ---- state ---------------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and "rounds" in state:
                return state
        except (OSError, ValueError):
            pass
        return {"rounds": 0, "champion": None, "history": [], "papers": [], "boosts": {}, "specs": {},
                "targets": {}, "met": False}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def enabled(self) -> bool:
        return self.settings().get("rsi", "0") == "1"

    def pace_s(self) -> float:
        return MET_PACE_S if self.state.get("met") else 60.0

    def champion_genome(self) -> dict:
        champ = self.state.get("champion")
        return dict(BASELINE, **(champ["genome"] if champ else {}))

    def tool_kwargs(self) -> dict:
        g = self.champion_genome()
        return {"hidden": g["tool_hidden"], "per_template": g["tool_per_template"], "epochs": g["tool_epochs"]}

    def status(self) -> dict:
        champ = self.state.get("champion")
        t = self.state.get("targets") or {}
        out = {"on": self.enabled(), "running_now": self.running, "step": self.step, "rounds": self.state["rounds"],
               "computer": describe(self.state["specs"]) if self.state.get("specs") else "not measured yet",
               "targets": t, "all_targets_met": self.state.get("met", False),
               "kept": sum(1 for h in self.state["history"] if h.get("kept")),
               "last_rounds": [{k: h.get(k) for k in ("round", "changed", "why", "score", "kept", "note")}
                               for h in self.state["history"][-5:]],
               "papers_read": [{k: p[k] for k in ("title", "date", "url")} for p in self.state["papers"][-5:]]}
        if champ:
            m = champ["measures"]
            out["best"] = {"model": LABELS.get(champ["genome"]["model"], champ["genome"]["model"]),
                           "score": champ["score"], "measures": m, "not_met_yet": unmet(m, t) if t else []}
        if self.problem:
            out["problem"] = self.problem
        return out

    # ---- one round ---------------------------------------------------------------------------------
    def round(self, cancel: Callable[[], bool] = lambda: False) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"started": False, "note": "a lab round is already running"}
        self.running, self.problem = True, ""
        try:
            return self._round(cancel)
        except Stopped:
            return {"stopped": True}
        finally:
            self.running, self.step = False, ""
            self.lock.release()

    def _check(self, cancel) -> None:
        if cancel():
            raise Stopped()

    def _round(self, cancel) -> dict:
        self.step = "reading this computer's specs"
        specs = self.specs_probe()
        self.state["specs"] = specs
        t = self.state["targets"] = targets_for(specs)
        choices = gene_choices(specs, t["ram_gb"])
        n = self.state["rounds"] + 1
        if self.web is not None and (n == 1 or n % RESEARCH_EVERY == 0):
            self._research(n)
        self._check(cancel)
        champ = self.state.get("champion")
        if champ is None:
            genome, changed, why = dict(BASELINE), [], "measuring the starting brain"
            if genome["model"] not in choices["model"] and choices["model"]:
                genome["model"] = min(choices["model"], key=lambda m: LABELS[m])
            measures = self._measure(genome, None, set(TOOL_GENES + MODEL_GENES), cancel)
        else:
            genome, changed, why = self._mutate(champ["genome"], choices)
            measures = self._measure(genome, champ, set(changed), cancel)
        self._check(cancel)
        score = utility(measures, t)
        entry = {"round": n, "at": self.clock(), "changed": {k: genome[k] for k in changed}, "why": why,
                 "score": score, "measures": measures, "kept": False}
        verdict = self._judge(measures, score, champ, t)
        if verdict == "":
            entry["kept"] = True
            self.state["champion"] = {"genome": genome, "measures": measures, "score": score, "round": n}
            self._promote(genome, changed or TOOL_GENES)
            entry["note"] = "better: now in use" if champ else "starting point"
        else:
            entry["note"] = verdict
        best = self.state["champion"]
        self.state["met"] = bool(best) and not unmet(best["measures"], t)
        self.state["rounds"] = n
        self.state["history"] = (self.state["history"] + [entry])[-HISTORY:]
        self._save()
        log.info("brain lab round %s: %s score %.3f %s", n, entry["changed"], score, entry["note"])
        return entry

    def _judge(self, m: dict, score: float, champ: Optional[dict], t: dict) -> str:
        """"" to keep the new version, else why it was thrown away."""
        if m.get("ram_gb", 0) > t["ram_gb"]:
            return f"uses {m['ram_gb']} GB, over the {t['ram_gb']} GB budget"
        if champ is None:
            return ""
        old = champ["measures"]
        if m.get("unsafe_fast", 0) > old.get("unsafe_fast", 0):
            return "the quick tool brain acted wrongly on more requests it should have left alone"
        if score <= champ["score"] + 0.005:
            return f"not better ({score:.3f} vs {champ['score']:.3f})"
        return ""

    def _mutate(self, genome: dict, choices: dict) -> tuple[dict, list[str], str]:
        new = dict(BASELINE, **genome)
        genes = [g for g in TOOL_GENES + MODEL_GENES if len(choices[g]) > 1]
        boosts = self.state.get("boosts", {})
        weights = [1.0 + len(boosts.get(g, [])) for g in genes]
        gene = self.rng.choices(genes, weights)[0]
        options = [v for v in choices[gene] if v != new[gene]]
        new[gene] = self.rng.choice(options)
        papers = boosts.get(gene) or []
        why = f"idea from \"{papers[-1]}\"" if papers and self.rng.random() < 0.7 else "trying a nearby setting"
        return new, [gene], why

    # ---- research -------------------------------------------------------------------------------------
    def _research(self, n: int) -> None:
        self.step = "searching research papers"
        query = PAPER_QUERIES[(n // RESEARCH_EVERY) % len(PAPER_QUERIES)]
        try:
            found = self.web.papers(query, limit=6, since_year=time.gmtime(self.clock()).tm_year - 2)["papers"]
        except Exception as exc:
            self.problem = f"paper search failed: {exc}"[:200]
            return
        known = {p["url"] for p in self.state["papers"]}
        for p in found:
            if p["url"] in known:
                continue
            text = (p["title"] + " " + p.get("summary", "")).lower()
            genes = sorted({g for words, gs in IDEA_RULES if any(w in text for w in words) for g in gs})
            self.state["papers"].append({"title": p["title"], "date": p["date"], "url": p["url"],
                                         "query": query, "genes": genes})
            for g in genes:
                self.state["boosts"].setdefault(g, []).append(p["title"])
                self.state["boosts"][g] = self.state["boosts"][g][-5:]
        self.state["papers"] = self.state["papers"][-60:]

    # ---- measuring -------------------------------------------------------------------------------------
    def _measure(self, genome: dict, champ: Optional[dict], changed: set, cancel) -> dict:
        m = dict(champ["measures"]) if champ else {}
        if champ is None or changed & set(TOOL_GENES):
            m.update(self._measure_tools(genome, cancel))
        self._check(cancel)
        if champ is None or changed & set(MODEL_GENES):
            m.update(self._measure_model(genome, cancel))
        return m

    def _candidate_dir(self) -> Path:
        return self.folder / "candidate"

    def _measure_tools(self, g: dict, cancel) -> dict:
        self.step = "training a quick tool brain"
        fixed, sealed = load_cases(), load_cases(SEALED_PATH)
        decls = self.declarations()
        start = time.perf_counter()
        brain = train_tool_brain(decls, held_out=fixed + sealed, hidden=g["tool_hidden"],
                                 per_template=g["tool_per_template"], epochs=g["tool_epochs"], cancel=cancel)
        self._check(cancel)
        train_s = time.perf_counter() - start
        folder = self._candidate_dir()
        folder.mkdir(exist_ok=True)
        brain.save(folder / "tool_brain.npz")
        self.step = "testing the quick tool brain"
        raw = run_tool_eval(ToolBrainEngine(brain, 0.0), decls, fixed + sealed, "")
        gated = run_tool_eval(ToolBrainEngine(brain, g["tool_confidence"]), decls, sealed, "")
        # A wrong call made on its own counts against safety; passing a request on to the big brain does not.
        unsafe = sum(1 for r in gated.results if not r.ok and r.got_tool is not None)
        times = [r.latency_s * 1000 for r in raw.results]
        return {"tool_accuracy": round(raw.accuracy, 4), "tool_sealed": round(gated.accuracy, 4),
                "unsafe_fast": unsafe, "tool_ms": round(statistics.quantiles(times, n=10)[-1], 3),
                "tool_train_s": round(train_s, 1)}

    def _options(self, g: dict) -> dict:
        opts = {"num_ctx": g["num_ctx"], "temperature": g["temperature"], "num_predict": 900}
        if g["num_thread"]:
            opts["num_thread"] = g["num_thread"]
        return opts

    def _ask(self, model: str, opts: dict, messages: list[dict]) -> tuple[str, float, float]:
        """(answer, seconds to the first word, words written per second)."""
        start = time.perf_counter()
        first: list[float] = []
        r = self.client.chat({"model": model, "think": False, "options": opts, "messages": messages},
                             timeout=1800, idle_s=600, first_token=lambda: first.append(time.perf_counter()))
        wrote = r.get("eval_count", 0) / max(r.get("eval_duration", 0) / 1e9, 1e-9)
        answer = (r.get("message") or {}).get("content") or ""
        return answer, (first[0] if first else time.perf_counter()) - start, wrote

    def _ensure(self, model: str, cancel) -> None:
        names = {m.get("name") or m.get("model") for m in self.client.list_models()}
        if model in names or f"{model}:latest" in names:
            return
        self.step = f"downloading {LABELS.get(model, model)}"
        self.client.pull(model, cancel=cancel)

    def _measure_model(self, g: dict, cancel) -> dict:
        model, opts = g["model"], self._options(g)
        label = LABELS.get(model, model)
        try:
            self._ensure(model, cancel)
        except LLMError as exc:
            self._check(cancel)
            return {"coding": 0.0, "long_task": 0.0, "first_reply_s": 1e9, "writes_per_s": 0.0, "model_tools": 0.0,
                    "ram_gb": 0.0, "model_problem": str(exc)[:200]}
        firsts, speeds, passed, total = [], [], 0, 0
        for task in load_coding():
            self._check(cancel)
            self.step = f"{label}: coding task {task['id']}"
            try:
                ask = task["prompt"] + "\nReply with only the Python code."
                answer, first, wrote = self._ask(model, opts, [{"role": "user", "content": ask}])
            except LLMError as exc:
                log.info("lab coding task %s failed: %s", task["id"], exc)
                total += 1
                continue
            firsts.append(first)
            speeds.append(wrote)
            out = sandbox.run_tests(sandbox.extract_code(answer), task["tests"])
            passed, total = passed + (1 if out.ok else 0), total + 1
        ram = 0.0
        try:
            running = [m for m in self.client.running() if (m.get("name") or m.get("model")) == model]
            ram = round(running[0].get("size", 0) / GB, 2) if running else 0.0
        except LLMError:
            pass
        long_score = self._long_task(model, opts, label, cancel)
        tools = 0.0
        if self.measure_model_tools is not None:
            self._check(cancel)
            self.step = f"{label}: tool calls"
            try:
                tools = self.measure_model_tools(model, opts)
            except LLMError as exc:
                log.info("lab tool test failed: %s", exc)
        return {"coding": round(passed / total, 4) if total else 0.0, "long_task": long_score,
                "first_reply_s": round(statistics.median(firsts), 2) if firsts else 1e9,
                "writes_per_s": round(statistics.median(speeds), 1) if speeds else 0.0,
                "model_tools": round(tools, 4), "ram_gb": ram}

    def _long_task(self, model: str, opts: dict, label: str, cancel) -> float:
        """A multi-step project done the way a small model can: one small step at a time, each checked by
        tests before the next, carrying the module forward. Score: share of steps whose tests all pass."""
        task = json.loads(LONG_PATH.read_text(encoding="utf-8"))
        code, tests, ok = "", [], 0
        for i, step in enumerate(task["steps"], 1):
            self._check(cancel)
            self.step = f"{label}: long project step {i} of {len(task['steps'])}"
            ask = f"{task['intro']}\n\n" + (f"The module so far:\n```python\n{code}\n```\n\n" if code else "") + \
                f"Step {i}: {step['ask']}\nReply with only the complete module as Python code."
            try:
                answer = self._ask(model, opts, [{"role": "user", "content": ask}])[0]
            except LLMError:
                break
            new = sandbox.extract_code(answer)
            tests = tests + step["tests"]
            if sandbox.run_tests(new, tests).ok:
                ok += 1
                code = new
            elif not code:
                code = new  # carry on from its attempt so later steps are still tried
        return round(ok / len(task["steps"]), 4)

    # ---- putting the champion to work ------------------------------------------------------------------
    def _promote(self, genome: dict, changed) -> None:
        if set(changed) & set(TOOL_GENES):
            src = self._candidate_dir() / "tool_brain.npz"
            if src.exists() and self.router is not None:
                self.router.folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, self.router.folder / "tool_brain.npz")
                from jarvis_books.llm.tool_brain import ToolBrain
                self.router.brain = ToolBrain.load(self.router.folder / "tool_brain.npz")
            if self.router is not None:
                self.router.min_confidence = genome["tool_confidence"]
        if self.apply is not None:
            try:
                self.apply(genome)
            except Exception:
                log.exception("putting the new brain to work failed")


WAIT_STEPS = (60, 300, 900, 1800)


class LabClock:
    """Runs lab rounds on their own thread while RSI mode is on, only while Jarvis is otherwise idle, and
    stops a round in the middle when the author needs him or the app closes."""

    def __init__(self, lab: BrainLab, busy: Callable[[], bool] = lambda: False, start_delay: float = 300.0,
                 defer: Callable[[], bool] = lambda: False):
        # busy: the author needs Jarvis (a running round stops); defer: other background work, so a round
        # waits to start but isn't stopped by it
        self.lab, self.busy, self.start_delay, self.defer = lab, busy, start_delay, defer
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.failures = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="brain-lab", daemon=True)
        self._thread.start()

    def poke(self) -> None:
        self._wake.set()

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _cancel(self) -> bool:
        return self._stop.is_set() or self.busy() or not self.lab.enabled()

    def _run(self) -> None:
        self._wait(self.start_delay)
        while not self._stop.is_set():
            delay = self.lab.pace_s()
            if self.lab.enabled() and not self.busy() and not self.defer():
                try:
                    self.lab.round(self._cancel)
                    self.failures = 0
                except Exception as exc:
                    delay = WAIT_STEPS[min(self.failures, len(WAIT_STEPS) - 1)]
                    self.failures += 1
                    self.lab.problem = f"{type(exc).__name__}: {exc}"[:300]
                    log.exception("brain lab round failed")
            elif self.lab.enabled():
                delay = 60.0
            if self._stop.is_set():
                break
            self._wait(delay)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
