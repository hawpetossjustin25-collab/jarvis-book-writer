"""NEXUS Collective resource intelligence: what a worker computer really has, and whether it may take work now.

Each part of the hardware is reported at four separate levels, because they are different facts:
  detected        the operating system lists the device
  os_supported    the device is working (a driver is loaded / the status is OK)
  runtime         an AI runtime on this machine can actually run work on it (e.g. CUDA for an NVIDIA card)
  available       the owner allows it to be contributed and it passes the live limits right now
Nothing is assumed: a graphics card without a supported runtime is detected but not usable for AI work.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.llm.hardware import available_ram, parse_meminfo
from jarvis_books.llm.specs import cpu_flags, parse_cpuinfo

log = logging.getLogger(__name__)
GB = 1024 ** 3
NPU_PROVIDERS = ("QNNExecutionProvider", "OpenVINOExecutionProvider", "VitisAIExecutionProvider")
GPU_PROVIDERS = ("CUDAExecutionProvider", "DmlExecutionProvider", "ROCMExecutionProvider")


def _hidden() -> dict:
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW


class ResourceProvider:
    """Measures this computer. `run` and `which` are injectable so every adapter is testable offline."""

    def __init__(self, data_dir: Optional[Path] = None, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 which: Callable[[str], Optional[str]] = shutil.which, system: Optional[str] = None):
        self.data_dir = Path(data_dir) if data_dir else Path.home()
        self.run, self.which = run, which
        self.system = system or sys.platform
        self._cpu_sample: Optional[tuple[float, float]] = None

    # ---- helpers
    def _cmd(self, args: list[str], timeout: float = 15.0) -> str:
        try:
            out = self.run(args, capture_output=True, text=True, timeout=timeout, **_hidden())
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("resource probe %s failed: %s", args[0], exc)
            return ""
        return out.stdout if out.returncode == 0 else ""

    def _ps_json(self, script: str) -> list[dict]:
        text = self._cmd(["powershell", "-NoProfile", "-Command", script + " | ConvertTo-Json -Compress"])
        if not text.strip():
            return []
        try:
            data = json.loads(text)
        except ValueError:
            return []
        return data if isinstance(data, list) else [data]

    # ---- the six detectors the spec names
    def detect_cpu(self) -> dict:
        info = {"name": platform.processor() or "", "cores": None, "threads": os.cpu_count(), "max_mhz": None,
                "flags": cpu_flags(), "load_percent": self.cpu_load()}
        if self.system == "win32":
            rows = self._ps_json("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
                                 "NumberOfLogicalProcessors,MaxClockSpeed")
            if rows:
                r = rows[0]
                info.update(name=(r.get("Name") or "").strip(), cores=r.get("NumberOfCores"),
                            threads=r.get("NumberOfLogicalProcessors") or info["threads"],
                            max_mhz=r.get("MaxClockSpeed"))
        else:
            try:
                parsed = parse_cpuinfo(Path("/proc/cpuinfo").read_text())
                info.update({k: v for k, v in parsed.items() if k in ("cpu", "cores", "threads", "max_mhz") and v})
                info["name"] = parsed.get("cpu") or info["name"]
                info.pop("cpu", None)
            except OSError:
                pass
        info["cores"] = info["cores"] or info["threads"]
        return {"detected": True, "os_supported": True, "runtime": ["cpu"], **info}

    def detect_memory(self) -> dict:
        total = free = None
        if self.system == "win32":
            from jarvis_books.llm.hardware import _windows_memory
            total, free = _windows_memory()
        else:
            try:
                total, free = parse_meminfo(Path("/proc/meminfo").read_text())
            except OSError:
                free = available_ram()
        return {"total_gb": round(total / GB, 2) if total else None, "free_gb": round(free / GB, 2) if free else None}

    def detect_gpu(self) -> list[dict]:
        gpus: list[dict] = []
        if self.which("nvidia-smi"):
            text = self._cmd(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,temperature.gpu",
                              "--format=csv,noheader,nounits"])
            for line in text.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3 and parts[0]:
                    gpus.append({"name": parts[0], "vendor": "NVIDIA", "detected": True, "os_supported": True,
                                 "runtime": ["cuda"], "vram_gb": _num(parts[1], 1 / 1024),
                                 "vram_free_gb": _num(parts[2], 1 / 1024),
                                 "temperature_c": _num(parts[3]) if len(parts) > 3 else None})
        if self.system == "win32":
            for r in self._ps_json("Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,Status"):
                name = (r.get("Name") or "").strip()
                if not name or any(g["name"] == name for g in gpus) or "basic display" in name.lower():
                    continue
                gpus.append({"name": name, "vendor": _vendor(name), "detected": True,
                             "os_supported": (r.get("Status") or "").upper() == "OK",
                             "runtime": self._directml(), "vram_gb": _num(r.get("AdapterRAM"), 1 / GB),
                             "vram_free_gb": None, "temperature_c": None})
        elif self.which("lspci"):
            for line in self._cmd(["lspci"]).splitlines():
                if any(k in line for k in ("VGA compatible", "3D controller", "Display controller")):
                    name = line.split(": ", 1)[-1].strip()
                    if not any(g["name"] in name or name in g["name"] for g in gpus):
                        gpus.append({"name": name, "vendor": _vendor(name), "detected": True, "os_supported": True,
                                     "runtime": [], "vram_gb": None, "vram_free_gb": None, "temperature_c": None})
        return gpus

    def detect_npu(self) -> list[dict]:
        found: list[dict] = []
        if self.system == "win32":
            rows = self._ps_json("Get-PnpDevice -PresentOnly | Where-Object { $_.Class -eq 'ComputeAccelerator' -or "
                                 "$_.FriendlyName -match 'NPU|Neural|AI Boost' } | Select-Object FriendlyName,Status")
            for r in rows:
                name = (r.get("FriendlyName") or "").strip()
                if name:
                    found.append({"name": name, "detected": True, "os_supported": (r.get("Status") or "") == "OK",
                                  "runtime": [p for p in self._onnx_providers() if p in NPU_PROVIDERS]})
        elif Path("/dev/accel").exists():
            for dev in sorted(Path("/dev/accel").iterdir()):
                found.append({"name": dev.name, "detected": True, "os_supported": True,
                              "runtime": [p for p in self._onnx_providers() if p in NPU_PROVIDERS]})
        return found

    def detect_storage(self) -> dict:
        try:
            usage = shutil.disk_usage(str(self.data_dir))
        except OSError:
            return {"total_gb": None, "free_gb": None}
        return {"total_gb": round(usage.total / GB, 1), "free_gb": round(usage.free / GB, 1)}

    def detect_network(self, host: str = "1.1.1.1", port: int = 443, timeout: float = 3.0) -> dict:
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError:
            return {"online": False, "latency_ms": None, "hostname": socket.gethostname()}
        return {"online": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "hostname": socket.gethostname()}

    # ---- live conditions
    def detect_battery(self) -> dict:
        if self.system == "win32":
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                            ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
            st = SYSTEM_POWER_STATUS()
            try:
                if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
                    return {"present": False, "percent": None, "plugged_in": None}
            except (AttributeError, OSError):
                return {"present": False, "percent": None, "plugged_in": None}
            present = (st.BatteryFlag & 128) == 0 and (st.BatteryFlag & 0xFF) != 255
            pct = st.BatteryLifePercent & 0xFF
            return {"present": present, "percent": pct if present and pct <= 100 else None,
                    "plugged_in": st.ACLineStatus == 1 if st.ACLineStatus in (0, 1) else None}
        for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
            try:
                pct = int((bat / "capacity").read_text().strip())
                status = (bat / "status").read_text().strip().lower()
            except (OSError, ValueError):
                continue
            return {"present": True, "percent": pct, "plugged_in": status in ("charging", "full", "not charging")}
        return {"present": False, "percent": None, "plugged_in": None}

    def detect_temperature(self) -> Optional[float]:
        """Hottest thermal zone in °C, where the system reports one without admin rights (Linux sysfs)."""
        temps = []
        for zone in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            try:
                temps.append(int(zone.read_text().strip()) / 1000)
            except (OSError, ValueError):
                continue
        return round(max(temps), 1) if temps else None

    def cpu_load(self) -> Optional[float]:
        """Whole-machine CPU use since the previous call (or over a short sample on the first call)."""
        first = self._cpu_times()
        if first is None:
            return None
        if self._cpu_sample is None:
            time.sleep(0.25)
            prev, first = first, self._cpu_times()
            if first is None:
                return None
        else:
            prev = self._cpu_sample
        self._cpu_sample = first
        busy, total = first[0] - prev[0], first[1] - prev[1]
        return round(100.0 * busy / total, 1) if total > 0 else 0.0

    def _cpu_times(self) -> Optional[tuple[float, float]]:
        if self.system == "win32":
            idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
            try:
                if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                             ctypes.byref(user)):
                    return None
            except (AttributeError, OSError):
                return None
            total = kernel.value + user.value  # kernel time includes idle time
            return float(total - idle.value), float(total)
        try:
            parts = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        except (OSError, ValueError, IndexError):
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
        return sum(parts) - idle, sum(parts)

    # ---- runtimes
    def _onnx_providers(self) -> list[str]:
        try:
            import onnxruntime
            return list(onnxruntime.get_available_providers())
        except Exception:
            return []

    def _directml(self) -> list[str]:
        return ["directml"] if "DmlExecutionProvider" in self._onnx_providers() else []

    def detect_runtimes(self, ollama_version: Optional[Callable[[], str]] = None) -> dict:
        version = None
        if ollama_version is not None:
            try:
                version = ollama_version() or None
            except Exception:
                version = None
        return {"ollama": version, "onnxruntime": self._onnx_providers(), "python": platform.python_version()}

    def profile(self, ollama_version: Optional[Callable[[], str]] = None, network: bool = True) -> dict:
        return {"os": f"{platform.system()} {platform.release()}", "machine": platform.machine(),
                "cpu": self.detect_cpu(), "memory": self.detect_memory(), "gpus": self.detect_gpu(),
                "npus": self.detect_npu(), "storage": self.detect_storage(),
                "network": self.detect_network() if network else {"online": None, "latency_ms": None,
                                                                   "hostname": socket.gethostname()},
                "battery": self.detect_battery(), "temperature_c": self.detect_temperature(),
                "runtimes": self.detect_runtimes(ollama_version), "measured_at": time.time()}

    def telemetry(self) -> dict:
        """The cheap, frequently-sent part: load, free memory, battery, temperature."""
        return {"cpu_percent": self.cpu_load(), "memory_free_gb": self.detect_memory()["free_gb"],
                "battery": self.detect_battery(), "temperature_c": self.detect_temperature(),
                "storage_free_gb": self.detect_storage()["free_gb"], "at": time.time()}


def _num(value, scale: float = 1.0) -> Optional[float]:
    try:
        return round(float(value) * scale, 2)
    except (TypeError, ValueError):
        return None


def _vendor(name: str) -> str:
    low = name.lower()
    for key, vendor in (("nvidia", "NVIDIA"), ("geforce", "NVIDIA"), ("radeon", "AMD"), ("amd", "AMD"),
                        ("intel", "Intel"), ("arc", "Intel"), ("apple", "Apple"), ("qualcomm", "Qualcomm"),
                        ("adreno", "Qualcomm")):
        if key in low:
            return vendor
    return "unknown"


@dataclass
class ContributionLimits:
    """What the owner allows this computer to give. Checked before every task, not just at enrollment."""
    enabled: bool = True
    max_cpu_percent: float = 60.0
    max_memory_gb: float = 4.0
    gpu: bool = False
    npu: bool = False
    storage: bool = False
    max_storage_gb: float = 2.0
    hours: list[str] = field(default_factory=lambda: ["00:00-24:00"])
    max_bandwidth_kbps: Optional[int] = None
    min_battery_percent: int = 40
    plugged_in_only: bool = False
    max_temperature_c: float = 85.0
    max_tasks: int = 1

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "ContributionLimits":
        raw = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown limit(s): {', '.join(sorted(unknown))}")
        out = cls(**raw)
        out.validate()
        return out

    def validate(self) -> None:
        if not 1 <= self.max_cpu_percent <= 100:
            raise ValueError("max_cpu_percent must be 1-100")
        if self.max_memory_gb <= 0 or self.max_storage_gb < 0:
            raise ValueError("memory and storage limits must be positive")
        if not 0 <= self.min_battery_percent <= 100:
            raise ValueError("min_battery_percent must be 0-100")
        if not 1 <= self.max_tasks <= 16:
            raise ValueError("max_tasks must be 1-16")
        if self.max_bandwidth_kbps is not None and self.max_bandwidth_kbps <= 0:
            raise ValueError("max_bandwidth_kbps must be positive")
        for span in self.hours:
            _span(span)

    def to_dict(self) -> dict:
        return asdict(self)

    def in_hours(self, now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        for span in self.hours:
            start, end = _span(span)
            if (start <= minute < end) if start <= end else (minute >= start or minute < end):
                return True
        return False

    def refusals(self, telemetry: dict, now: Optional[datetime] = None, running: int = 0,
                 need_memory_gb: float = 0.0) -> list[str]:
        """Every reason this computer must not take another task right now (empty means it may)."""
        now = now or datetime.now()
        why = []
        if not self.enabled:
            why.append("contribution is paused by the owner")
        if not self.in_hours(now):
            why.append(f"outside the allowed hours {', '.join(self.hours)}")
        if running >= self.max_tasks:
            why.append(f"already running {running} task(s), the limit is {self.max_tasks}")
        cpu = telemetry.get("cpu_percent")
        if cpu is not None and cpu >= self.max_cpu_percent:
            why.append(f"the processor is {cpu:.0f}% busy, the limit is {self.max_cpu_percent:.0f}%")
        free = telemetry.get("memory_free_gb")
        if need_memory_gb > self.max_memory_gb:
            why.append(f"the task needs {need_memory_gb:g} GB, the owner allows {self.max_memory_gb:g} GB")
        elif free is not None and need_memory_gb and free - need_memory_gb < 1.0:
            why.append(f"only {free:g} GB free; the task needs {need_memory_gb:g} GB plus 1 GB to spare")
        bat = telemetry.get("battery") or {}
        if bat.get("present"):
            if self.plugged_in_only and bat.get("plugged_in") is False:
                why.append("running on battery and the owner allows plugged-in only")
            elif bat.get("plugged_in") is False and (bat.get("percent") or 0) < self.min_battery_percent:
                why.append(f"battery at {bat.get('percent')}%, below {self.min_battery_percent}%")
        temp = telemetry.get("temperature_c")
        if temp is not None and temp >= self.max_temperature_c:
            why.append(f"running hot ({temp:g} °C)")
        return why


def _span(text: str) -> tuple[int, int]:
    try:
        a, b = text.split("-")
        ha, ma = (int(x) for x in a.strip().split(":"))
        hb, mb = (int(x) for x in b.strip().split(":"))
    except ValueError:
        raise ValueError(f"hours must look like 08:00-17:30, not {text!r}") from None
    if not (0 <= ha <= 24 and 0 <= hb <= 24 and 0 <= ma < 60 and 0 <= mb < 60) or (ha == 24 and ma) or (hb == 24 and mb):
        raise ValueError(f"hours must look like 08:00-17:30, not {text!r}")
    return ha * 60 + ma, hb * 60 + mb


def available_for_contribution(profile: dict, limits: ContributionLimits) -> dict:
    """The fourth level: what the owner lets NEXUS use, from what was detected and is runnable."""
    mem = profile.get("memory") or {}
    gpus = [g for g in profile.get("gpus", []) if g.get("os_supported") and g.get("runtime")]
    npus = [n for n in profile.get("npus", []) if n.get("os_supported") and n.get("runtime")]
    runtimes = ["cpu"] + (["ollama"] if (profile.get("runtimes") or {}).get("ollama") else [])
    if limits.gpu:
        runtimes += sorted({r for g in gpus for r in g["runtime"]})
    if limits.npu:
        runtimes += sorted({r for n in npus for r in n["runtime"]})
    return {"cpu_threads": (profile.get("cpu") or {}).get("threads"),
            "memory_gb": min(limits.max_memory_gb, mem.get("total_gb") or limits.max_memory_gb),
            "gpus": [g["name"] for g in gpus] if limits.gpu else [],
            "npus": [n["name"] for n in npus] if limits.npu else [],
            "storage_gb": min(limits.max_storage_gb, (profile.get("storage") or {}).get("free_gb") or 0)
            if limits.storage else 0, "runtimes": runtimes}
