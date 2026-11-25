"""Jarvis's tool-calling test set: fixed requests with the tool call that should answer each one.

Every brain (Gemini, Groq, the local models, a future NCM tool brain) is scored on the same cases, so a change of
brain, prompt or tool list can be compared by numbers instead of by feel. Nothing is ever run: the brain only
proposes calls, and they are checked against what the case expects.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from jarvis_books.llm.errors import LLMError
from jarvis_books.llm.models import LLMRequest

CASES_PATH = Path(__file__).with_name("data") / "tool_cases.jsonl"
EVAL_SYSTEM_NOTE = ("(Tool test: answer this one request the way you normally would. Call the tool that does it, "
                    "or just reply in words if no tool is needed.)")


@dataclass(frozen=True)
class ToolCase:
    id: str
    category: str
    request: str
    tools: tuple[Optional[str], ...]  # acceptable first calls; None means "no tool call"
    args: dict = field(default_factory=dict)  # each must match the call (strings: contained, any case)

    @property
    def wants_tool(self) -> bool:
        return any(t is not None for t in self.tools)


@dataclass
class CaseResult:
    id: str
    category: str
    ok: bool
    reason: str
    got_tool: Optional[str]
    got_args: dict
    latency_s: float
    model: str = ""


@dataclass
class ToolEvalReport:
    results: list[CaseResult]
    started: float = 0.0

    @property
    def passed(self) -> int:
        return sum(r.ok for r in self.results)

    @property
    def accuracy(self) -> float:
        return self.passed / len(self.results) if self.results else 0.0

    def by_category(self) -> dict[str, dict]:
        groups: dict[str, list[CaseResult]] = defaultdict(list)
        for r in self.results:
            groups[r.category].append(r)
        return {k: {"passed": sum(r.ok for r in v), "total": len(v)} for k, v in sorted(groups.items())}

    def latency(self) -> dict[str, float]:
        times = sorted(r.latency_s for r in self.results if r.reason != "brain error")
        if not times:
            return {"median_s": 0.0, "p90_s": 0.0}
        return {"median_s": round(statistics.median(times), 3),
                "p90_s": round(times[min(len(times) - 1, int(len(times) * 0.9))], 3)}

    def models(self) -> dict[str, int]:
        return dict(Counter(r.model for r in self.results if r.model))

    def failures(self) -> list[dict]:
        return [{"id": r.id, "why": r.reason, "got": r.got_tool, "args": r.got_args} for r in self.results if not r.ok]

    def summary(self) -> str:
        lat = self.latency()
        return (f"{self.passed} of {len(self.results)} tool tests right ({self.accuracy:.0%}), "
                f"typical answer {lat['median_s']:.1f} s, slowest tenth {lat['p90_s']:.1f} s")

    def to_dict(self) -> dict:
        return {"passed": self.passed, "total": len(self.results), "accuracy": round(self.accuracy, 4),
                "by_category": self.by_category(), "latency": self.latency(), "models": self.models(),
                "failures": self.failures(), "summary": self.summary(), "started": self.started,
                "results": [asdict(r) for r in self.results]}


def load_cases(path: Path = CASES_PATH) -> list[ToolCase]:
    cases = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            tools = raw["tool"] if isinstance(raw["tool"], list) else [raw["tool"]]
            cases.append(ToolCase(id=str(raw["id"]), category=str(raw["category"]), request=str(raw["request"]),
                                  tools=tuple(tools), args=dict(raw.get("args") or {})))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path} line {n}: {exc}") from exc
    return cases


def check_cases(cases: Iterable[ToolCase], declarations: list[dict]) -> list[str]:
    """Problems that would make a case unfair: a tool or argument that doesn't exist, a bad enum value."""
    by_name = {d["name"]: d for d in declarations}
    problems, seen = [], set()
    for case in cases:
        if case.id in seen:
            problems.append(f"{case.id}: duplicate id")
        seen.add(case.id)
        if not case.request.strip():
            problems.append(f"{case.id}: empty request")
        for tool in case.tools:
            if tool is not None and tool not in by_name:
                problems.append(f"{case.id}: unknown tool {tool}")
        main = case.tools[0]
        props = by_name[main]["parameters"].get("properties", {}) if main in by_name else {}
        if case.args and main is None:
            problems.append(f"{case.id}: arguments given for a no-tool case")
        for key, want in case.args.items():
            if main in by_name and key not in props:
                problems.append(f"{case.id}: {main} has no argument {key!r}")
            enum = props.get(key, {}).get("enum")
            if enum and want not in enum:
                problems.append(f"{case.id}: {key}={want!r} is not one of {enum}")
    return problems


def _matches(want: Any, got: Any) -> bool:
    if isinstance(want, bool) or isinstance(got, bool):
        return _truthy(got) == want if isinstance(want, bool) else False
    if isinstance(want, (int, float)):
        try:
            return abs(float(str(got).replace(",", "").replace("$", "")) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    return str(want).lower() in str(got).lower().replace("\\\\", "\\")


def _truthy(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return True if text in ("true", "yes", "on", "1") else False if text in ("false", "no", "off", "0") else None


def score(case: ToolCase, calls: list[dict]) -> tuple[bool, str]:
    first = calls[0] if calls else None
    name = (first or {}).get("name")
    if name not in case.tools:
        if name is None:
            return False, f"no tool call (wanted {case.tools[0]})"
        wanted = " or ".join(t or "no tool" for t in case.tools)
        return False, f"called {name} (wanted {wanted})"
    if name is None:
        return True, "answered in words"
    if name != case.tools[0]:
        return True, f"called {name}, an accepted alternative"
    args = (first or {}).get("args") or {}
    for key, want in case.args.items():
        if key not in args:
            return False, f"{name} without {key}"
        if not _matches(want, args[key]):
            return False, f"{name} with {key}={args[key]!r} (wanted {want!r})"
    return True, "right tool and details"


def route_coverage(cases: Iterable[ToolCase], select: Callable[[str], list[dict]]) -> dict:
    """How often the short tool list given to the small brains still holds the right tool. Needs no brain, so
    upgrades can be compared on it for free."""
    missing = []
    total = 0
    for case in cases:
        if not case.wants_tool or None in case.tools:
            continue
        total += 1
        offered = {d["name"] for d in select(case.request)}
        if not any(t in offered for t in case.tools if t is not None):
            missing.append(case.id)
    covered = total - len(missing)
    return {"covered": covered, "total": total, "share": round(covered / total, 4) if total else 1.0,
            "missing": missing}


def run_tool_eval(engine, declarations: list[dict], cases: Iterable[ToolCase], system: str,
                  select: Optional[Callable[[str], list[dict]]] = None,
                  progress: Callable[[float, str], None] = lambda p, m: None,
                  cancel: Optional[Callable[[], bool]] = None,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep, retries: int = 4) -> ToolEvalReport:
    """Ask the brain each request once with the real tool list and score its first call. No tool runs.
    A brain that is only briefly busy or over its per-minute limit (Groq's free plan) is waited for and asked
    again, so the score measures its answers rather than its rate limit; the wait is not counted as latency."""
    cases = list(cases)
    report = ToolEvalReport(results=[], started=time.time())
    for n, case in enumerate(cases):
        if cancel is not None and cancel():
            break
        progress(n / max(1, len(cases)), f"Tool test {n + 1} of {len(cases)}")
        request = LLMRequest(role="chat", system=system + "\n\n" + EVAL_SYSTEM_NOTE,
                             history=[{"role": "user", "parts": [{"text": case.request}]}],
                             tools=declarations, local_tools=select(case.request) if select else None,
                             temperature=0.0, purpose="tool test")
        resp, error = None, None
        for attempt in range(retries + 1):
            start = clock()
            try:
                resp = engine.generate(request)
                break
            except LLMError as exc:
                error = exc
                if not exc.retryable or exc.daily_quota or attempt == retries or (cancel is not None and cancel()):
                    break
                wait = min(70.0, max(2.0, exc.retry_after_s or 15.0 * (attempt + 1)))
                progress(n / max(1, len(cases)), f"Tool test {n + 1}: the brain is busy, trying again in {wait:.0f} s")
                sleep(wait)
        if resp is None:
            report.results.append(CaseResult(case.id, case.category, False, "brain error", None,
                                             {"error": str(error)[:200]}, clock() - start))
            continue
        ok, why = score(case, resp.function_calls)
        first = resp.function_calls[0] if resp.function_calls else {}
        report.results.append(CaseResult(case.id, case.category, ok, why, first.get("name"),
                                         dict(first.get("args") or {}), clock() - start, resp.model))
    progress(1.0, report.summary())
    return report
