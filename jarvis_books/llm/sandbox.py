"""Checks code a model wrote against unit tests without letting it touch the computer.

The code is read first and refused if it imports anything outside a short list of pure-computation modules,
calls anything that reaches files, processes or the interpreter itself, or uses underscore attributes. What
passes runs in a separate isolated Python with an empty environment, in an empty folder, under a time limit."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ALLOWED_IMPORTS = frozenset({"math", "re", "json", "collections", "itertools", "functools", "string", "datetime",
                             "typing", "dataclasses", "decimal", "fractions", "heapq", "bisect", "statistics",
                             "csv", "io", "enum", "operator", "__future__"})
BLOCKED_NAMES = frozenset({"open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "vars",
                           "getattr", "setattr", "delattr", "breakpoint", "exit", "quit", "help", "memoryview",
                           "__builtins__", "__loader__", "__spec__"})
TIME_LIMIT_S = 10.0
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


@dataclass
class Outcome:
    passed: int
    total: int
    problem: str = ""

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total


def extract_code(answer: str) -> str:
    """The code in a model's answer: its fenced blocks joined, or the whole answer when it has none."""
    blocks = _FENCE.findall(answer or "")
    return "\n\n".join(b.strip() for b in blocks) if blocks else (answer or "").strip()


def unsafe(code: str) -> str:
    """Why the code may not run, or "" when it only computes."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"not valid Python: {exc.msg} (line {exc.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if a.name.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            bad = [] if (node.module or "").split(".")[0] in ALLOWED_IMPORTS and node.level == 0 else \
                [node.module or "."]
        elif isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            return f"uses {node.id}"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return f"uses .{node.attr}"
        else:
            continue
        if bad:
            return f"imports {', '.join(bad)}"
    return ""


def python_exe() -> str:
    """A console Python (pythonw.exe can't report results)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def run_tests(code: str, tests: list[str], time_limit_s: float = TIME_LIMIT_S) -> Outcome:
    """Each test is a snippet of asserts run after the code; counts how many pass."""
    why = unsafe(code) or next((unsafe(t) for t in tests if unsafe(t)), "")
    if why:
        return Outcome(0, len(tests), f"refused to run: {why}")
    runner = ("import sys\nsrc = sys.stdin.read().split('\\0')\nns = {}\n"
              "exec(compile(src[0], 'answer', 'exec'), ns)\n"
              "ok = 0\nfor t in src[1:]:\n    try:\n        exec(compile(t, 'test', 'exec'), dict(ns))\n        ok += 1\n"
              "    except BaseException as e:\n        print('FAIL', type(e).__name__, str(e)[:200])\n"
              "print('PASSED', ok)\n")
    with tempfile.TemporaryDirectory(prefix="jarvis-sandbox-") as folder:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run([python_exe(), "-I", "-S", "-c", runner], input="\0".join([code] + tests),
                                 capture_output=True, text=True, timeout=time_limit_s, cwd=folder, env={},
                                 creationflags=flags)
        except subprocess.TimeoutExpired:
            return Outcome(0, len(tests), f"took longer than {time_limit_s:g} s")
    found = re.search(r"^PASSED (\d+)$", out.stdout, re.M)
    if not found:
        return Outcome(0, len(tests), (out.stderr or out.stdout).strip()[-300:] or "the code didn't run")
    fails = [line for line in out.stdout.splitlines() if line.startswith("FAIL")]
    return Outcome(int(found.group(1)), len(tests), "; ".join(fails)[:300])
