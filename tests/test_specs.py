import json
import subprocess

from jarvis_books.llm import specs as specsmod
from jarvis_books.llm.specs import describe, parse_cpuinfo, probe_specs

CPUINFO = """processor\t: 0
model name\t: Intel(R) Core(TM) i3-7100U CPU @ 2.40GHz
cpu MHz\t\t: 2400.000
physical id\t: 0
core id\t\t: 0

processor\t: 1
model name\t: Intel(R) Core(TM) i3-7100U CPU @ 2.40GHz
cpu MHz\t\t: 2394.100
physical id\t: 0
core id\t\t: 1

processor\t: 2
physical id\t: 0
core id\t\t: 0

processor\t: 3
physical id\t: 0
core id\t\t: 1
"""


def test_linux_processor_details_are_read_from_cpuinfo():
    out = parse_cpuinfo(CPUINFO)
    assert out["cpu"] == "Intel(R) Core(TM) i3-7100U CPU @ 2.40GHz"
    assert (out["cores"], out["threads"], out["max_mhz"]) == (2, 4, 2400)


def test_windows_processor_details_come_from_powershell(monkeypatch, tmp_path):
    monkeypatch.setattr(specsmod.sys, "platform", "win32")
    monkeypatch.setattr(specsmod, "_windows_memory", lambda: (16 * 1024 ** 3, 9 * 1024 ** 3))
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        body = {"name": "Intel(R) Core(TM) i3-7100U CPU @ 2.40GHz ", "cores": 2, "threads": 4, "mhz": 2400,
                "l3_kb": 3072, "gpus": "Intel(R) HD Graphics 620"}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
    out = probe_specs(tmp_path, run=run)
    assert seen[0][0] == "powershell"
    assert out["cpu"].endswith("2.40GHz") and out["cores"] == 2 and out["threads"] == 4
    assert out["gpus"] == ["Intel(R) HD Graphics 620"] and out["ram_gb"] == 16.0 and out["ram_free_gb"] == 9.0
    assert out["problems"] == [] and out["disk_free_gb"] > 0
    assert "2 cores / 4 threads" in describe(out)


def test_an_unreadable_processor_still_gives_the_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(specsmod.sys, "platform", "win32")
    monkeypatch.setattr(specsmod, "_windows_memory", lambda: (8 * 1024 ** 3, 2 * 1024 ** 3))

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)
    out = probe_specs(tmp_path, run=run)
    assert "processor details unreadable" in out["problems"][0]
    assert out["ram_gb"] == 8.0 and out["cores"] == out["threads"] and isinstance(out["flags"], list)


def test_this_computers_real_specs_are_measured():
    out = probe_specs()
    assert out["threads"] >= 1 and out["ram_gb"] > 0 and isinstance(out["cuda_gpu"], bool)
    assert set(out["flags"]) <= set(specsmod.FAST_FLAGS)
