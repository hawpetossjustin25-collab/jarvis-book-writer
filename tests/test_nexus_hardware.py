"""Hardware advice: verdicts come from what the machine reports, with evidence, and never invent upgrades."""
import json
import subprocess

from jarvis_books.nexus.deepram import ModelShape
from jarvis_books.nexus.hardware import ExpansionProbe, advise

GIB = 1024 ** 3


def fake_run(answers: dict):
    def run(args, **kw):
        script = args[-1]
        for key, value in answers.items():
            if key in script:
                out = value if isinstance(value, str) else json.dumps(value)
                return subprocess.CompletedProcess(args, 0, out, "")
        return subprocess.CompletedProcess(args, 0, "", "")
    return run


LAPTOP = {  # what the i3-7100U laptop's Windows answers look like
    "Win32_SystemEnclosure": {"ChassisTypes": [10]},
    "Win32_ComputerSystem": {"Manufacturer": "HP", "Model": "HP Laptop 15"},
    "Win32_PhysicalMemoryArray": {"MaxCapacityEx": 33554432, "MemoryDevices": 2},
    "Win32_PhysicalMemory": [{"Capacity": 8 * GIB, "Speed": 2133, "ConfiguredClockSpeed": 2133,
                              "SMBIOSMemoryType": 26, "FormFactor": 12, "DeviceLocator": "DIMM 1"},
                             {"Capacity": 8 * GIB, "Speed": 2133, "ConfiguredClockSpeed": 2133,
                              "SMBIOSMemoryType": 26, "FormFactor": 12, "DeviceLocator": "DIMM 2"}],
    "Thunderbolt|USB4": "",
    "Measure-Object": "Count : 180",
    "Get-PhysicalDisk": {"FriendlyName": "WDC WD10", "MediaType": 3, "BusType": 11, "Size": 1000 * GIB},
    "Win32_SystemSlot": "",
}
PROFILE = {"cpu": {"name": "Intel(R) Core(TM) i3-7100U CPU @ 2.40GHz", "cores": 2, "threads": 4},
           "memory": {"total_gb": 16.0, "free_gb": 9.0},
           "gpus": [{"name": "Intel(R) HD Graphics 620", "runtime": []}],
           "storage": {"free_gb": 120.0}}


def shape(name: str, gb: float) -> ModelShape:
    return ModelShape(name=name, weights_bytes=int(gb * GIB), max_context=32768, layers=36, kv_heads=8,
                      heads=32, embedding=4096, key_length=128, value_length=128)


def test_the_laptop_scan_reads_windows_answers():
    scan = ExpansionProbe(system="win32", run=fake_run(LAPTOP)).scan()
    assert scan["chassis"] == {"kind": "portable", "types": [10], "maker": "HP", "model": "HP Laptop 15"}
    assert scan["memory"]["slots"] == 2 and scan["memory"]["max_gb"] == 32.0
    assert scan["memory"]["modules"][0] == {"gb": 8.0, "slot": "DIMM 1", "type": "DDR4", "form": "SODIMM",
                                            "speed": 2133}
    assert scan["ports"] == [] and scan["disks"] == [{"name": "WDC WD10", "media": "HDD", "bus": "SATA", "gb": 1000}]
    assert scan["pcie_slots"] is None


def test_laptop_advice_is_honest():
    scan = ExpansionProbe(system="win32", run=fake_run(LAPTOP)).scan()
    out = advise(PROFILE, scan)
    cannot = {i["part"]: i for i in out["cannot_upgrade"]}
    can = [i["what"] for i in out["can_upgrade"]]
    assert "processor" in cannot and "BGA" in cannot["processor"]["evidence"]
    assert "external graphics" in cannot and "Thunderbolt" in cannot["external graphics"]["why"]
    assert any(i["what"] == "run AI models on the Intel(R) HD Graphics 620" for i in out["cannot_upgrade"])
    assert any(i["what"] == "put a graphics card inside" for i in out["cannot_upgrade"])
    assert any(w.startswith("go from 16 GB up to 32 GB: replace the 2 module(s) with bigger SODIMM DDR4") for w in can)
    assert "move models to an SSD" in can and any("Collective" in w for w in can)
    assert not any("external graphics" == i["part"] for i in out["can_upgrade"])
    assert out["owns"]["usable_for_ai"] == ["processor"] and out["owns"]["computer"] == "HP Laptop 15"
    for item in out["can_upgrade"] + out["cannot_upgrade"] + out["unknown"]:
        assert item["evidence"] and item["why"]


def test_more_memory_is_shown_as_models_that_would_fit_not_speed():
    scan = ExpansionProbe(system="win32", run=fake_run(LAPTOP)).scan()
    shapes = [shape("small", 2.5), shape("big", 14.0)]
    models = [{"name": "small", "output_tps": 4.2}, {"name": "big", "output_tps": None}]
    exp = advise(PROFILE, scan, models, shapes)["expected"]
    assert exp["fit_now"] == ["small"] and exp["newly_fit"] == ["big"]
    assert exp["measured_speeds"] == {"small": 4.2, "big": None} and "measured" in exp["note"]


def test_thunderbolt_one_module_and_full_memory_cases():
    tb = dict(LAPTOP, **{"Thunderbolt|USB4": {"FriendlyName": "Thunderbolt(TM) Controller - 15D2", "Status": "OK"},
                         "Win32_PhysicalMemory": [LAPTOP["Win32_PhysicalMemory"][0]]})
    out = advise(PROFILE, ExpansionProbe(system="win32", run=fake_run(tb)).scan())
    can = {i["part"] + ": " + i["what"] for i in out["can_upgrade"]}
    assert "external graphics: connect an external graphics card enclosure" in can
    assert "memory: add a matching second module for dual-channel memory" in can
    full = dict(LAPTOP, **{"Win32_PhysicalMemoryArray": {"MaxCapacityEx": 16777216, "MemoryDevices": 2}})
    out = advise(PROFILE, ExpansionProbe(system="win32", run=fake_run(full)).scan())
    assert any(i["what"] == "go past 16 GB" for i in out["cannot_upgrade"])
    soldered = dict(LAPTOP, **{"Win32_PhysicalMemory": [dict(LAPTOP["Win32_PhysicalMemory"][0], SMBIOSMemoryType=35)]})
    out = advise(PROFILE, ExpansionProbe(system="win32", run=fake_run(soldered)).scan())
    assert any("LPDDR" in i["why"] for i in out["cannot_upgrade"])


def test_unknowns_stay_unknown():
    silent = ExpansionProbe(system="win32", run=fake_run({})).scan()
    assert silent == {"chassis": {"kind": None, "types": [], "maker": "", "model": ""}, "memory": None,
                      "ports": None, "disks": None, "pcie_slots": None}
    out = advise({"cpu": {"name": "AMD Ryzen 7 7700X"}, "memory": {"total_gb": 32, "free_gb": 20}, "gpus": []},
                 silent)
    unknown = {i["part"] for i in out["unknown"]}
    assert {"processor", "memory", "graphics", "external graphics"} <= unknown
    assert not [i for i in out["cannot_upgrade"] if i["part"] in ("processor", "external graphics")]


def test_desktop_with_a_free_x16_slot():
    desk = {"Win32_SystemEnclosure": {"ChassisTypes": [3]},
            "Win32_SystemSlot": [{"SlotDesignation": "PCIEX16_1", "CurrentUsage": 3, "MaxDataWidth": 13}]}
    out = advise({"cpu": {"name": "AMD Ryzen 7 7700X"}, "memory": {}, "gpus": []},
                 ExpansionProbe(system="win32", run=fake_run(desk)).scan())
    slot = next(i for i in out["can_upgrade"] if i["part"] == "graphics")
    assert "power supply" in slot["why"]


def test_the_tool_is_declared_read_only_with_templates():
    from jarvis_books.assistant.nexus_tools import MODEL_TOOLS
    from jarvis_books.assistant.permissions import READ, level_of
    assert "hardware_advice" in MODEL_TOOLS and level_of("hardware_advice") == READ
