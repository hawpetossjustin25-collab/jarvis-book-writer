"""Files and folders on the author's computer, with a backup before every change so it can be undone."""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from jarvis_books.errors import NotFound

MAX_READ_CHARS = 20_000
MAX_WRITE_BYTES = 5_000_000
# Places a slip could break Windows or programs; Jarvis may read them but never change them.
_PROTECTED_WIN = ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
                  "system volume information", "recovery", "boot", "efi")
_PROTECTED_POSIX = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr", "/var")
# Passwords, keys and logins never go to Gemini, and Jarvis doesn't edit them either.
_SECRET = re.compile(r"(^|[\\/])(\.ssh|\.aws|\.gnupg|\.azure|\.kube|\.docker|\.git-credentials|\.netrc|\.npmrc|\.pypirc|"
                     r"\.env(\.[\w.-]+)?|id_rsa[\w.]*|id_ed25519[\w.]*|[^\\/]*\.(pem|key|pfx|p12|kdbx)|credentials(\.json)?|"
                     r"gemini\.key|accounts\.db|Login Data|Cookies|Local State|Web Data)([\\/]|$)", re.I)


def is_secret(path: Path) -> bool:
    return bool(_SECRET.search(str(path)))


class Computer:
    def __init__(self, work_dir: Path, home: Optional[Path] = None, platform: str = sys.platform):
        self.backups = work_dir / "backups"
        self.log_path = work_dir / "actions.jsonl"
        self.home = home or Path.home()
        self.windows = platform == "win32"
        self.backups.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._n = 0

    # ---- paths ------------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        raw = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
        if not raw:
            raise ValueError("say which file or folder")
        p = Path(raw)
        if not p.is_absolute():
            p = self.home / p
        return Path(os.path.abspath(p))

    def protected(self, p: Path) -> Optional[str]:
        if len(p.parts) <= 1:
            return "a whole drive"
        if self.backups in p.parents or p == self.backups:
            return "Jarvis's own backups"
        if self.windows:
            parts = [x.lower() for x in p.parts[1:]]
            if parts and parts[0] in _PROTECTED_WIN:
                return f"a Windows system folder ({p.parts[0]}{p.parts[1]})"
        elif any(str(p) == d or str(p).startswith(d + "/") for d in _PROTECTED_POSIX):
            return "a system folder"
        if is_secret(p):
            return "a password or key file"
        return None

    def _readable(self, path: str) -> Path:
        p = self.resolve(path)
        if is_secret(p):
            raise NotFound(f"{p} holds passwords or keys, so Jarvis doesn't open it")
        return p

    def _changeable(self, path: str) -> Path:
        p = self.resolve(path)
        why = self.protected(p)
        if why:
            raise NotFound(f"{p} is {why}; Jarvis doesn't change that")
        return p

    # ---- reading -------------------------------------------------------------------------
    def list_folder(self, path: str = "", limit: int = 200) -> dict:
        p = self._readable(path or str(self.home))
        if not p.is_dir():
            raise NotFound(f"no folder at {p}")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if len(entries) >= limit:
                break
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append({"name": child.name, "folder": child.is_dir(),
                            "size": None if child.is_dir() else st.st_size,
                            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
        total = sum(1 for _ in p.iterdir())
        return {"folder": str(p), "entries": entries, "more": max(0, total - len(entries))}

    def read_file(self, path: str, start_line: int = 1, max_chars: int = MAX_READ_CHARS) -> dict:
        p = self._readable(path)
        if not p.is_file():
            raise NotFound(f"no file at {p}")
        data = p.read_bytes()
        if b"\x00" in data[:8192]:
            return {"file": str(p), "binary": True, "size": len(data),
                    "note": "this is not a text file, so it can't be shown as text"}
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(start_line or 1))
        out, used = [], 0
        for i, line in enumerate(lines[start - 1:], start):
            if used + len(line) > max_chars:
                break
            out.append(f"{i}: {line}")
            used += len(line) + 1
        last = start + len(out) - 1
        return {"file": str(p), "lines": len(lines), "showing": f"{start}-{last}", "text": "\n".join(out),
                "more": last < len(lines)}

    # ---- changing (each one backs up first and returns how to undo it) -------------------------
    def _backup(self, p: Path) -> Optional[str]:
        if not p.exists():
            return None
        with self._lock:
            self._n += 1
            bid = time.strftime("%Y%m%d-%H%M%S") + f"-{self._n}"
        target = self.backups / bid
        target.mkdir(parents=True)
        (target / "original.json").write_text(json.dumps({"path": str(p)}), encoding="utf-8")
        if p.is_dir():
            shutil.copytree(p, target / "item")
        else:
            shutil.copy2(p, target / "item")
        return bid

    def restore(self, backup_id: Optional[str], path: str) -> dict:
        p = Path(path)
        if backup_id is None:  # it didn't exist before: undo by removing what was made
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
            self.log("undo", path=str(p), removed=True)
            return {"restored": str(p), "removed": True}
        item = self.backups / backup_id / "item"
        if not item.exists():
            raise NotFound("that backup is gone")
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        p.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, p)
        else:
            shutil.copy2(item, p)
        self.log("undo", path=str(p), backup=backup_id)
        return {"restored": str(p)}

    def plan_write(self, path: str, content: str, append: bool = False) -> dict:
        """What a write would do, for the yes/no question: path, new or replaced, and a short diff."""
        p = self._changeable(path)
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ValueError("that is too big to write in one go (5 MB limit)")
        if p.is_dir():
            raise ValueError(f"{p} is a folder")
        old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
        new = (old or "") + content if append else content
        return {"path": p, "old": old, "new": new, "diff": diff(old or "", new, p.name)}

    def write_file(self, path: str, content: str, append: bool = False) -> dict:
        plan = self.plan_write(path, content, append)
        p = plan["path"]
        backup = self._backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".jarvis-tmp")
        tmp.write_text(plan["new"], encoding="utf-8", newline="")
        os.replace(tmp, p)
        self.log("write", path=str(p), backup=backup, bytes=len(plan["new"]))
        return {"written": str(p), "created": plan["old"] is None, "backup": backup, "diff": plan["diff"]}

    def plan_edit(self, path: str, find: str, replace: str, all_matches: bool = False) -> dict:
        p = self._changeable(path)
        if not p.is_file():
            raise NotFound(f"no file at {p}")
        old = p.read_text(encoding="utf-8", errors="replace")
        count = old.count(find) if find else 0
        if count == 0:
            raise NotFound("that text isn't in the file; read it again and copy the exact text")
        if count > 1 and not all_matches:
            raise ValueError(f"that text appears {count} times; include more of it so it matches once, "
                             f"or set all_matches")
        new = old.replace(find, replace) if all_matches else old.replace(find, replace, 1)
        return {"path": p, "old": old, "new": new, "count": count if all_matches else 1,
                "diff": diff(old, new, p.name)}

    def edit_file(self, path: str, find: str, replace: str, all_matches: bool = False) -> dict:
        plan = self.plan_edit(path, find, replace, all_matches)
        out = self.write_file(str(plan["path"]), plan["new"])
        return {**out, "replaced": plan["count"]}

    def make_folder(self, path: str) -> dict:
        p = self._changeable(path)
        if p.exists():
            return {"folder": str(p), "existed": True}
        p.mkdir(parents=True)
        self.log("mkdir", path=str(p))
        return {"folder": str(p), "existed": False}

    def move(self, source: str, destination: str) -> dict:
        src, dest = self._changeable(source), self._changeable(destination)
        if not src.exists():
            raise NotFound(f"nothing at {src}")
        if dest.is_dir():
            dest = dest / src.name
        if dest.exists():
            raise ValueError(f"{dest} already exists; pick another name or delete it first")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        self.log("move", path=str(src), to=str(dest))
        return {"moved": str(src), "to": str(dest)}

    def delete(self, path: str) -> dict:
        """Moves it into Jarvis's backups instead of erasing it, so 'undo that' brings it back."""
        p = self._changeable(path)
        if not p.exists():
            raise NotFound(f"nothing at {p}")
        if p == self.home or p in self.home.parents:
            raise NotFound(f"{p} is your whole user folder; Jarvis doesn't delete that")
        backup = self._backup(p)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        self.log("delete", path=str(p), backup=backup)
        return {"deleted": str(p), "backup": backup}

    def log(self, action: str, **fields) -> None:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, **fields}
        with self._lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 20) -> list[dict]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(x) for x in lines if x.strip()]


def diff(old: str, new: str, name: str = "file", max_lines: int = 60) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{name} (now)", f"{name} (after)",
                                      lineterm="", n=2))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... {len(lines) - max_lines} more diff lines"]
    return "\n".join(lines)
