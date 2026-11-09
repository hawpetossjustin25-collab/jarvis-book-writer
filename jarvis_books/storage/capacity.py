"""Storage capacity: the library grows on demand; this watches the disk and keeps the cache sized.

Manuscripts are never deleted automatically. Only regenerable audio is evicted, least recently
used first, when it exceeds its budget (a share of free disk) or the disk gets low.
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis_books.storage.library import Library

log = logging.getLogger(__name__)

GB = 1024 ** 3


@dataclass(frozen=True)
class CapacityReport:
    database_bytes: int
    audio_bytes: int
    disk_free_bytes: int
    disk_total_bytes: int
    audio_budget_bytes: int
    low_disk: bool
    measured_at: float

    @property
    def library_bytes(self) -> int:
        return self.database_bytes + self.audio_bytes

    def describe(self) -> str:
        def fmt(n: int) -> str:
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if n < 1024 or unit == "TB":
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024
            return str(n)
        text = (f"Books use {fmt(self.database_bytes)}, audio cache {fmt(self.audio_bytes)}; "
                f"{fmt(self.disk_free_bytes)} free on disk.")
        if self.low_disk:
            text += " Disk space is low."
        return text


class CapacityMonitor:
    def __init__(self, library: Library, audio_root: Path, audio_share_of_free: float = 0.10,
                 audio_cap_bytes: int = 50 * GB, low_disk_bytes: int = 2 * GB,
                 disk_usage: Callable[[str], tuple] = shutil.disk_usage):
        self.library = library
        self.audio_root = Path(audio_root)
        self.audio_share_of_free = audio_share_of_free
        self.audio_cap_bytes = audio_cap_bytes
        self.low_disk_bytes = low_disk_bytes
        self._disk_usage = disk_usage

    def audio_bytes(self) -> int:
        row = self.library.db.query_one("SELECT COALESCE(SUM(bytes),0) FROM audio_segments")
        return int(row[0])

    def report(self) -> CapacityReport:
        usage = self._disk_usage(str(self.audio_root))
        total, free = int(usage[0]), int(usage[2])
        audio = self.audio_bytes()
        budget = int(min(self.audio_cap_bytes, (free + audio) * self.audio_share_of_free))
        return CapacityReport(self.library.db.size_bytes(), audio, free, total, budget,
                              free < self.low_disk_bytes, time.time())

    def enforce(self) -> int:
        """Evict least-recently-used audio until within budget. Returns bytes freed."""
        report = self.report()
        target = report.audio_budget_bytes
        if report.low_disk:
            target = min(target, report.audio_bytes // 2)
        excess = report.audio_bytes - target
        freed = 0
        if excess <= 0:
            return 0
        for row in self.library.db.query("SELECT id, path, bytes FROM audio_segments ORDER BY last_used"):
            if freed >= excess:
                break
            Path(row["path"]).unlink(missing_ok=True)
            self.library.db.execute("DELETE FROM audio_segments WHERE id=?", (row["id"],))
            freed += int(row["bytes"])
        if freed:
            log.info("evicted %d bytes of regenerable audio cache", freed)
        return freed
