"""The author's terminal: PowerShell on Windows, bash elsewhere. Every command is shown and asked about first
(the ToolBox gate); this runs it with a time limit, captures its output and logs it."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

MAX_OUTPUT = 12_000
MAX_TIMEOUT_S = 1800

# Commands that could wipe a drive or break Windows. Jarvis refuses them outright, even with a yes.
_BLOCKED = [
    (r"\bformat(-volume)?\s+[a-z]:", "formats a drive"), (r"\bformat-volume\b", "formats a drive"),
    (r"\b(clear|initialize)-disk\b", "wipes a disk"), (r"\bdiskpart\b", "edits disk partitions"),
    (r"\bremove-partition\b", "deletes a partition"), (r"\bcipher\s+/w", "wipes free space"),
    (r"\bvssadmin\s+delete\b", "deletes restore points"), (r"\bbcdedit\b", "edits how Windows boots"),
    (r"\breg(\.exe)?\s+delete\s+hklm", "deletes system registry keys"),
    (r"remove-item\s+(-\w+\s+)*['\"]?hklm:", "deletes system registry keys"),
    (r"(remove-item|rm|del|rmdir|rd)\b[^|;&]*\b[a-z]:\\(windows|program files|programdata)\b", "deletes Windows or program files"),
    (r"(remove-item|rm|del|rmdir|rd)\b[^|;&]*\s['\"]?[a-z]:\\?['\"]?(\s|$)", "deletes a whole drive"),
    (r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|/\*)(\s|$)", "deletes everything"),
    (r"\bmkfs(\.\w+)?\b", "formats a drive"), (r"\bdd\s+[^|;&]*of=/dev/", "overwrites a disk"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "crashes the computer"),
    (r"\bset-mppreference\b[^|;&]*disable", "turns off virus protection"),
]
# Allowed after a yes, but the question carries a warning.
_WARN = [
    (r"\bstart-process\b[^|;&]*-verb\s+runas|\bsudo\b|\brunas\b", "needs administrator rights"),
    (r"\b(stop-computer|restart-computer|shutdown(\.exe)?)\b", "shuts down or restarts the computer"),
    (r"\b(winget|choco|scoop)\s+(install|uninstall|upgrade)|\bpip\s+(install|uninstall)|\bnpm\s+(i|install)\b",
     "installs or removes software"),
    (r"\b(remove-item|rm|del|rmdir|rd)\b", "deletes files (not backed up)"),
    (r"\b(invoke-webrequest|iwr|curl|wget|invoke-restmethod|irm)\b", "uses the internet"),
    (r"\bset-executionpolicy\b|\bnew-itemproperty\b|\bset-itemproperty\b|\breg(\.exe)?\s+add\b", "changes Windows settings"),
    (r"\bgit\s+push\b", "pushes code to the internet"),
]


def assess(command: str) -> tuple[Optional[str], list[str]]:
    """(why it is refused or None, warnings)."""
    c = command.lower()
    blocked = next((why for pat, why in _BLOCKED if re.search(pat, c)), None)
    warnings = list(dict.fromkeys(why for pat, why in _WARN if re.search(pat, c)))
    return blocked, warnings


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    head = MAX_OUTPUT // 3
    return text[:head] + f"\n... ({len(text) - MAX_OUTPUT:,} characters cut) ...\n" + text[-(MAX_OUTPUT - head):]


class Terminal:
    def __init__(self, work_dir: Path, home: Optional[Path] = None, platform: str = sys.platform,
                 popen: Callable = subprocess.Popen):
        self.log_path = work_dir / "terminal.jsonl"
        work_dir.mkdir(parents=True, exist_ok=True)
        self.home = home or Path.home()
        self.windows = platform == "win32"
        self.popen = popen
        self._lock = threading.Lock()

    @property
    def shell(self) -> str:
        return "PowerShell" if self.windows else "bash"

    def argv(self, command: str) -> list[str]:
        if self.windows:
            return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
        return ["bash", "-c", command]

    def folder(self, cwd: str = "") -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(cwd.strip().strip('"')))) if cwd and cwd.strip() else self.home
        if not p.is_absolute():
            p = self.home / p
        if not p.is_dir():
            raise ValueError(f"no folder at {p}")
        return p

    def run(self, command: str, cwd: str = "", timeout_s: int = 120,
            cancel: Optional[threading.Event] = None) -> dict:
        command = (command or "").strip()
        if not command:
            raise ValueError("no command given")
        blocked, _ = assess(command)
        if blocked:
            raise ValueError(f"Jarvis won't run that: it {blocked}")
        folder = self.folder(cwd)
        timeout_s = max(1, min(int(timeout_s or 120), MAX_TIMEOUT_S))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.windows else 0
        start = time.monotonic()
        proc = self.popen(self.argv(command), cwd=str(folder), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=not self.windows)
        stopped = None
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    stopped = "cancelled"
                elif time.monotonic() - start > timeout_s:
                    stopped = f"stopped after {timeout_s}s (time limit)"
                if stopped:
                    self._kill(proc)
                    out, err = proc.communicate()
                    break
        seconds = round(time.monotonic() - start, 1)
        result = {"command": command, "folder": str(folder), "exit_code": proc.returncode,
                  "ok": proc.returncode == 0 and not stopped, "seconds": seconds,
                  "output": _clip(_text(out)), "errors": _clip(_text(err))}
        if stopped:
            result["stopped"] = stopped
        self._log(result)
        return result

    def _kill(self, proc) -> None:
        try:
            if self.windows:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()

    def _log(self, result: dict) -> None:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "command": result["command"],
                 "folder": result["folder"], "exit_code": result["exit_code"], "seconds": result["seconds"],
                 "stopped": result.get("stopped")}
        with self._lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _text(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
