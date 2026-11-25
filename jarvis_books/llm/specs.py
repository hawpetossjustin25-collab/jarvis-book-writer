"""What this computer can run a brain on: processor, cores, instruction sets, memory, graphics and disk.

The Brain Lab sets its targets from these numbers, so it reads them itself instead of trusting a guess."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.llm.hardware import available_ram, parse_meminfo, _windows_memory

GB = 1024 ** 3
FAST_FLAGS = ("AVX", "AVX2", "FMA3", "F16C", "AVX512F", "AVX512_VNNI", "AVX_VNNI", "ASIMD", "ASIMDDP")
_PS = ("$c = Get-CimInstance Win32_Processor | Select-Object -First 1; "
       "$g = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }); "
       "@{name=$c.Name; cores=$c.NumberOfCores; threads=$c.NumberOfLogicalProcessors; mhz=$c.MaxClockSpeed; "
       "l3_kb=$c.L3CacheSize; gpus=$g} | ConvertTo-Json -Compress")


def cpu_flags() -> list[str]:
    """The speed-up instruction sets this processor has, as NumPy detected them at import."""
    try:
        from numpy._core._multiarray_umath import __cpu_features__ as feats  # numpy 2
    except ImportError:
        try:
            from numpy.core._multiarray_umath import __cpu_features__ as feats  # numpy 1
        except ImportError:
            return []
    return [f for f in FAST_FLAGS if feats.get(f)]


def _windows_cpu(run: Callable[..., subprocess.CompletedProcess]) -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = run(["powershell", "-NoProfile", "-Command", _PS], capture_output=True, text=True, timeout=60,
              creationflags=flags)
    data = json.loads(out.stdout or "{}")
    gpus = data.get("gpus") or []
    return {"cpu": (data.get("name") or "").strip(), "cores": data.get("cores"), "threads": data.get("threads"),
            "max_mhz": data.get("mhz"), "l3_kb": data.get("l3_kb"),
            "gpus": [gpus] if isinstance(gpus, str) else list(gpus)}


def parse_cpuinfo(text: str) -> dict:
    name = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
    mhz = [float(m) for m in re.findall(r"^cpu MHz\s*:\s*([\d.]+)$", text, re.M)]
    cores = {(p, c) for p, c in re.findall(r"^physical id\s*:\s*(\d+)\n(?:.*\n)*?core id\s*:\s*(\d+)$", text, re.M)}
    threads = len(re.findall(r"^processor\s*:", text, re.M))
    return {"cpu": name.group(1).strip() if name else "", "cores": len(cores) or None, "threads": threads or None,
            "max_mhz": round(max(mhz)) if mhz else None, "l3_kb": None, "gpus": []}


def probe_specs(data_dir: Optional[Path] = None,
                run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """Everything the lab needs, each part measured on its own so one failure doesn't hide the rest."""
    specs: dict = {"cpu": "", "cores": None, "threads": os.cpu_count(), "max_mhz": None, "l3_kb": None,
                   "gpus": [], "problems": []}
    try:
        if sys.platform == "win32":
            specs.update({k: v for k, v in _windows_cpu(run).items() if v not in (None, "")})
        else:
            specs.update({k: v for k, v in parse_cpuinfo(Path("/proc/cpuinfo").read_text()).items()
                          if v not in (None, "", [])})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        specs["problems"].append(f"processor details unreadable: {exc}"[:200])
    specs["cores"] = specs["cores"] or specs["threads"]
    specs["flags"] = cpu_flags()
    if sys.platform == "win32":
        total, free = _windows_memory()
    else:
        try:
            total, free = parse_meminfo(Path("/proc/meminfo").read_text())
        except OSError:
            total, free = None, available_ram()
    specs["ram_gb"] = round(total / GB, 1) if total else None
    specs["ram_free_gb"] = round(free / GB, 1) if free else None
    specs["cuda_gpu"] = shutil.which("nvidia-smi") is not None
    try:
        specs["disk_free_gb"] = round(shutil.disk_usage(str(data_dir or Path.home())).free / GB, 1)
    except OSError:
        specs["disk_free_gb"] = None
    return specs


def describe(specs: dict) -> str:
    gpu = "an NVIDIA graphics card" if specs.get("cuda_gpu") else "no graphics card for AI"
    return (f"{specs.get('cpu') or 'unknown processor'}, {specs.get('cores')} cores / {specs.get('threads')} "
            f"threads, {specs.get('ram_gb')} GB RAM, {gpu}, instruction sets "
            f"{', '.join(specs.get('flags') or []) or 'basic only'}")
