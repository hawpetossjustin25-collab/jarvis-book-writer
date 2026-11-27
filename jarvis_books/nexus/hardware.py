"""NEXUS hardware advice: what this computer has, what can and can't be upgraded, which expansion ports it really
has, and what an upgrade would realistically change.

Every verdict is "can", "cannot" or "unknown", and carries the evidence it rests on. Nothing is promised that the
detected hardware can't do: no external graphics card without a Thunderbolt or USB4 controller, no processor swap on
a soldered mobile chip, and no speed-up figures. The only predictions are DeepRAM's memory arithmetic (which models
would fit) and the speeds Model Hub actually measured."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from jarvis_books.nexus.deepram import DeepRAM, ModelShape
from jarvis_books.nexus.resources import GB, ResourceProvider

PORTABLE = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}  # SMBIOS chassis types: laptop, notebook, tablet...
DESKTOP = {3, 4, 5, 6, 7, 13, 15, 16, 35, 36}
MEMORY_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 27: "LPDDR", 28: "LPDDR2", 29: "LPDDR3",
                30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}
FORM_FACTORS = {8: "DIMM", 12: "SODIMM", 13: "SRIMM", 23: "LGA"}
MOBILE_CPU = re.compile(r"(?:\bi[3579]-\d{4,5}|\bcore\(tm\) ultra \d|\bryzen \d+ \d{4})\s*(U|Y|H|HQ|HK|HS|HX|G\d)\b",
                        re.I)
_PORT = re.compile(r"thunderbolt|usb ?4", re.I)


class ExpansionProbe(ResourceProvider):
    """The parts of the machine the Collective profile doesn't need: chassis, memory slots, ports, disks, slots.
    Each detector returns None when the operating system won't say, which the advice reports as unknown."""

    def chassis(self) -> dict:
        if self.system == "win32":
            enc = self._ps_json("Get-CimInstance Win32_SystemEnclosure | Select-Object ChassisTypes")
            cs = self._ps_json("Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model")
            types = [int(t) for t in ((enc[0].get("ChassisTypes") if enc else None) or [])]
            maker = (cs[0].get("Manufacturer") or "").strip() if cs else ""
            model = (cs[0].get("Model") or "").strip() if cs else ""
        else:
            base = Path("/sys/class/dmi/id")
            read = lambda n: (base / n).read_text().strip() if (base / n).exists() else ""  # noqa: E731
            raw = read("chassis_type")
            types, maker, model = ([int(raw)] if raw.isdigit() else []), read("sys_vendor"), read("product_name")
        kind = ("portable" if any(t in PORTABLE for t in types) else
                "desktop" if any(t in DESKTOP for t in types) else None)
        return {"kind": kind, "types": types, "maker": maker, "model": model}

    def memory(self) -> Optional[dict]:
        if self.system != "win32":
            return None  # Linux needs root (dmidecode) to list memory slots
        mods = self._ps_json("Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,"
                             "ConfiguredClockSpeed,SMBIOSMemoryType,FormFactor,DeviceLocator")
        arr = self._ps_json("Get-CimInstance Win32_PhysicalMemoryArray | Select-Object MaxCapacityEx,MaxCapacity,"
                            "MemoryDevices")
        if not mods:
            return None
        modules = [{"gb": round(int(m.get("Capacity") or 0) / GB, 1), "slot": (m.get("DeviceLocator") or "").strip(),
                    "type": MEMORY_TYPES.get(int(m.get("SMBIOSMemoryType") or 0)),
                    "form": FORM_FACTORS.get(int(m.get("FormFactor") or 0)),
                    "speed": m.get("ConfiguredClockSpeed") or m.get("Speed")} for m in mods]
        slots = sum(int(a.get("MemoryDevices") or 0) for a in arr) or None
        kb = sum(int(a.get("MaxCapacityEx") or a.get("MaxCapacity") or 0) for a in arr)
        return {"modules": modules, "slots": slots, "max_gb": round(kb / 1024 ** 2, 1) if kb else None}

    def ports(self) -> Optional[list[str]]:
        if self.system == "win32":
            rows = self._ps_json("Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -match "
                                 "'Thunderbolt|USB4|USB 4' } | Select-Object FriendlyName,Status")
            text = self._cmd(["powershell", "-NoProfile", "-Command", "Get-PnpDevice -PresentOnly | Measure-Object"])
            if not rows and not text.strip():
                return None  # PowerShell didn't answer: unknown, not "none"
            return sorted({(r.get("FriendlyName") or "").strip() for r in rows if _PORT.search(r.get("FriendlyName")
                                                                                              or "")})
        tb = Path("/sys/bus/thunderbolt/devices")
        return sorted(d.name for d in tb.iterdir() if d.name.startswith("domain")) if tb.exists() else None

    def disks(self) -> Optional[list[dict]]:
        if self.system == "win32":
            rows = self._ps_json("Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,Size")
            media = {3: "HDD", 4: "SSD", "HDD": "HDD", "SSD": "SSD"}
            bus = {11: "SATA", 17: "NVMe", 7: "USB", "SATA": "SATA", "NVMe": "NVMe", "USB": "USB"}
            return [{"name": (r.get("FriendlyName") or "").strip(), "media": media.get(r.get("MediaType")),
                     "bus": bus.get(r.get("BusType")), "gb": round(int(r.get("Size") or 0) / GB)} for r in rows] or None
        out = []
        for dev in sorted(Path("/sys/block").glob("*")) if Path("/sys/block").exists() else []:
            rot = dev / "queue" / "rotational"
            if dev.name.startswith(("loop", "ram", "zram", "dm-")) or not rot.exists():
                continue
            size = int((dev / "size").read_text().strip() or 0) * 512 if (dev / "size").exists() else 0
            out.append({"name": dev.name, "media": "HDD" if rot.read_text().strip() == "1" else "SSD",
                        "bus": "NVMe" if dev.name.startswith("nvme") else None, "gb": round(size / GB)})
        return out or None

    def pcie_slots(self) -> Optional[list[dict]]:
        if self.system != "win32":
            return None
        rows = self._ps_json("Get-CimInstance Win32_SystemSlot | Select-Object SlotDesignation,CurrentUsage,"
                             "MaxDataWidth")
        return [{"name": (r.get("SlotDesignation") or "").strip(), "free": r.get("CurrentUsage") == 3,
                 "x16": "16" in (r.get("SlotDesignation") or "") or r.get("MaxDataWidth") in (13, 16)}
                for r in rows] if rows else None

    def scan(self) -> dict:
        return {"chassis": self.chassis(), "memory": self.memory(), "ports": self.ports(), "disks": self.disks(),
                "pcie_slots": self.pcie_slots()}


def _item(part: str, verdict: str, what: str, why: str, evidence: str) -> dict:
    return {"part": part, "verdict": verdict, "what": what, "why": why, "evidence": evidence}


def advise(profile: dict, expansion: dict, models: Optional[list[dict]] = None,
           shapes: Optional[list[ModelShape]] = None, context: int = 4096) -> dict:
    """The advice from a ResourceProvider profile, an ExpansionProbe scan and (optionally) Model Hub's installed
    models and their shapes. Pure: every input is what the machine reported."""
    cpu, mem, gpus = profile.get("cpu") or {}, profile.get("memory") or {}, profile.get("gpus") or []
    chassis = expansion.get("chassis") or {}
    portable = chassis.get("kind") == "portable"
    slots, ports, disks, pcie = (expansion.get("memory"), expansion.get("ports"), expansion.get("disks"),
                                 expansion.get("pcie_slots"))
    items: list[dict] = []

    # processor
    name = cpu.get("name") or "unknown processor"
    if MOBILE_CPU.search(name):
        items.append(_item("processor", "cannot", f"replace the {name}",
                           "it is a mobile processor soldered to the main board",
                           "Intel and AMD U/Y/H-series mobile processors ship only in BGA packages (Intel ARK and "
                           "AMD product pages list 'FCBGA' sockets), so they can't be removed"))
    else:
        items.append(_item("processor", "unknown", f"replace the {name}",
                           "it depends on the main board's socket and its supported-processor list",
                           "the operating system doesn't report the socket or the board's processor list"))

    # memory
    total = mem.get("total_gb")
    if slots and slots.get("modules"):
        mods = slots["modules"]
        kinds = {m["type"] for m in mods if m.get("type")}
        installed = round(sum(m["gb"] for m in mods), 1)
        label = "/".join(sorted(kinds)) or "memory"
        forms = "/".join(sorted({m["form"] for m in mods if m.get("form")})) or ""
        if any(k.startswith("LPDDR") for k in kinds):
            items.append(_item("memory", "cannot", f"add memory to the {installed} GB {label}",
                               "LPDDR memory is soldered to the board", "JEDEC LPDDR memory has no module form; "
                               "it is always soldered"))
        elif slots.get("max_gb") and installed >= slots["max_gb"]:
            items.append(_item("memory", "cannot", f"go past {installed:g} GB",
                               f"the board reports {slots['max_gb']:g} GB as its maximum and that is installed",
                               "Win32_PhysicalMemoryArray.MaxCapacityEx (the firmware's own limit)"))
        elif slots.get("max_gb") and slots.get("slots"):
            free = max(0, slots["slots"] - len(mods))
            how = (f"add a {forms + ' ' if forms else ''}{label} module in the free slot" if free else
                   f"replace the {len(mods)} module(s) with bigger {forms + ' ' if forms else ''}{label}")
            speed = next((m["speed"] for m in mods if m.get("speed")), None)
            items.append(_item("memory", "can", f"go from {installed:g} GB up to {slots['max_gb']:g} GB: {how}"
                               + (f" at {speed} MT/s" if speed else ""),
                               f"{slots['slots']} slot(s), {free} free; if a module is soldered to the board, only "
                               f"the socketed slot can change",
                               "Win32_PhysicalMemory (installed modules) and Win32_PhysicalMemoryArray (slots and "
                               "the firmware's maximum)"))
            if len(mods) == 1 and free:
                items.append(_item("memory", "can", "add a matching second module for dual-channel memory",
                                   "two matching modules double the memory bandwidth, and running a model on the "
                                   "processor is limited mostly by memory bandwidth",
                                   "one module installed with a free slot; the real gain must be measured after"))
        else:
            items.append(_item("memory", "unknown", f"add memory beyond {installed:g} GB",
                               "the firmware doesn't report its slot count or maximum", "Win32_PhysicalMemoryArray"))
    else:
        items.append(_item("memory", "unknown", f"add memory beyond {total or '?'} GB",
                           "this system doesn't list its memory slots without administrator tools",
                           "no slot information from the operating system"))

    # graphics
    usable = [g for g in gpus if g.get("runtime")]
    for g in gpus:
        if not g.get("runtime"):
            items.append(_item("graphics", "cannot", f"run AI models on the {g['name']}",
                               "no AI runtime on this computer can use it (no CUDA, ROCm or DirectML)",
                               "detected by the operating system, but no runtime reported it"))
    if portable:
        items.append(_item("graphics", "cannot", "put a graphics card inside", "laptops have no PCIe x16 card slot",
                           f"SMBIOS chassis type {chassis.get('types')} is a portable computer"))
    elif pcie is not None and any(s["x16"] and s["free"] for s in pcie):
        items.append(_item("graphics", "can", "add a graphics card in the free x16 slot",
                           "the power supply's wattage and connectors can't be read by software; check them first",
                           "Win32_SystemSlot reports a free x16 slot"))
    else:
        items.append(_item("graphics", "unknown", "add a graphics card inside",
                           "no free x16 slot was reported (some boards don't report slots)", "Win32_SystemSlot"))
    if ports is None:
        items.append(_item("external graphics", "unknown", "connect an external graphics card",
                           "the port list couldn't be read", "no answer from the device list"))
    elif ports:
        items.append(_item("external graphics", "can", "connect an external graphics card enclosure",
                           f"it needs the enclosure on {ports[0]}, the card's driver and its runtime (CUDA for "
                           f"NVIDIA); Jarvis then checks the runtime and measures it before using it",
                           "a Thunderbolt/USB4 controller is present; these carry PCIe for graphics enclosures"))
    else:
        items.append(_item("external graphics", "cannot", "connect an external graphics card",
                           "no Thunderbolt or USB4 controller was found; ordinary USB-C and USB 3 ports can't carry "
                           "a graphics card", "the device list has no Thunderbolt or USB4 controller"))

    # storage
    if disks:
        if any(d.get("media") == "HDD" for d in disks):
            items.append(_item("storage", "can", "move models to an SSD",
                               "an SSD loads a model from disk many times faster than a spinning hard drive",
                               "a hard drive (HDD) is installed"))
    free_disk = (profile.get("storage") or {}).get("free_gb")
    if free_disk is not None and free_disk < 20:
        items.append(_item("storage", "can", f"free disk space (only {free_disk:g} GB free)",
                           "models need several GB each, and Model Hub refuses installs that don't fit",
                           "measured free space on Jarvis's drive"))

    # more computers
    items.append(_item("more computers", "can", "add another computer to the Collective with a join code",
                       "each computer takes whole tasks (searches, embeddings, model answers); their memory is not "
                       "pooled into one bigger model", "NEXUS Collective schedules whole tasks per worker"))

    maker, model = chassis.get("maker") or "", chassis.get("model") or ""
    computer = model if maker and model.lower().startswith(maker.lower()) else " ".join(x for x in (maker, model) if x)
    return {"owns": {"computer": computer or None,
                     "kind": chassis.get("kind"), "processor": name, "cores": cpu.get("cores"),
                     "threads": cpu.get("threads"), "memory_gb": total, "memory_free_gb": mem.get("free_gb"),
                     "memory_modules": (slots or {}).get("modules"), "graphics": [g.get("name") for g in gpus],
                     "usable_for_ai": ["processor"] + [g["name"] for g in usable], "disks": disks,
                     "expansion_ports": ports},
            "can_upgrade": [i for i in items if i["verdict"] == "can"],
            "cannot_upgrade": [i for i in items if i["verdict"] == "cannot"],
            "unknown": [i for i in items if i["verdict"] == "unknown"],
            "expected": _expected(mem, slots, models or [], shapes or [], context)}


def _expected(mem: dict, slots: Optional[dict], models: list[dict], shapes: list[ModelShape],
              context: int) -> dict:
    """What more memory would change, by DeepRAM's arithmetic, and the speeds Model Hub measured. No guesses."""
    out: dict = {"measured_speeds": {m["name"]: m.get("output_tps") for m in models},
                 "note": "speeds are what Model Hub measured here; an upgrade's speed change must be measured after"}
    total, free = mem.get("total_gb"), mem.get("free_gb")
    top = (slots or {}).get("max_gb")
    if not (shapes and total and free is not None):
        return out
    ram = DeepRAM()
    now = [p.model for p in ram.choose(shapes, context, int(free * GB), int(total * GB)) if p.fits]
    out["fit_now"] = now
    if top and top > total:
        extra = top - total
        later = [p.model for p in ram.choose(shapes, context, int((free + extra) * GB), int(top * GB)) if p.fits]
        out[f"fit_with_{top:g}_gb"] = later
        out["newly_fit"] = [m for m in later if m not in now]
    return out


def scan_this_computer(home: Path) -> tuple[dict, dict]:
    provider = ResourceProvider(home)
    probe = ExpansionProbe(home, system=sys.platform)
    return provider.profile(network=False), probe.scan()
