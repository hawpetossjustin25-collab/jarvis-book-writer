"""Local hardware profile. The writing model runs in Google's cloud, so local hardware sizes the
parts that run here: audio synthesis workers, embedding batches, the audio cache and the voice pipeline."""
from __future__ import annotations

import ctypes
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.llm.models import HardwareProfile


def _windows_memory() -> tuple[Optional[int], Optional[int]]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None, None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def parse_meminfo(text: str) -> tuple[Optional[int], Optional[int]]:
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1]) * 1024
    return values.get("MemTotal"), values.get("MemAvailable", values.get("MemFree"))


def measure_network_latency(host: str = "generativelanguage.googleapis.com", port: int = 443,
                            timeout: float = 5.0) -> Optional[float]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except OSError:
        return None


def _qt_audio_devices() -> tuple[Optional[list[str]], Optional[list[str]]]:
    try:
        from PyQt6.QtCore import QCoreApplication
        if QCoreApplication.instance() is None:
            return None, None
        from PyQt6.QtMultimedia import QMediaDevices
        return ([d.description() for d in QMediaDevices.audioInputs()],
                [d.description() for d in QMediaDevices.audioOutputs()])
    except Exception:
        return None, None


def probe_hardware(data_dir: Optional[Path] = None,
                   audio_probe: Callable[[], tuple] = _qt_audio_devices,
                   latency_probe: Callable[[], Optional[float]] = measure_network_latency) -> HardwareProfile:
    warnings: list[str] = []
    sources: dict[str, str] = {}
    if sys.platform == "win32":
        total, available = _windows_memory()
        sources["ram"] = "GlobalMemoryStatusEx"
    else:
        try:
            total, available = parse_meminfo(Path("/proc/meminfo").read_text())
            sources["ram"] = "/proc/meminfo"
        except OSError:
            total = available = None
    if total is None:
        warnings.append("could not read system RAM")
    try:
        disk_free = shutil.disk_usage(str(data_dir or Path.home())).free
        sources["disk"] = "shutil.disk_usage"
    except OSError:
        disk_free = None
        warnings.append("could not read free disk space")
    inputs, outputs = audio_probe()
    if inputs is None:
        warnings.append("audio devices unknown (Qt Multimedia unavailable)")
    else:
        sources["audio"] = "QMediaDevices"
        if not inputs:
            warnings.append("no microphone found: voice input disabled")
        if not outputs:
            warnings.append("no speakers found: audio playback disabled")
    latency = latency_probe()
    if latency is None:
        warnings.append("Gemini API unreachable (offline?)")
    else:
        sources["network"] = "TCP connect to generativelanguage.googleapis.com"
    gpu = shutil.which("nvidia-smi") is not None
    return HardwareProfile(total_ram_bytes=total, available_ram_bytes=available, cpu_count=os.cpu_count(),
                           gpu_available=gpu, gpu_name=None, disk_free_bytes=disk_free, audio_inputs=inputs,
                           audio_outputs=outputs, network_latency_ms=latency, detection_warnings=warnings,
                           sources=sources, measured_at=time.time())
