"""The author's desktop: open and close programs, websites and files, system facts, the clipboard, screenshots,
the keyboard and mouse, volume and music keys.

Everything goes through small fixed PowerShell scripts on Windows. What Jarvis asks for (a program name, text to
type) is handed to the script in environment variables, never pasted into the script, so it can't turn into a
different command. Other systems get the parts that make sense there (opening things, the clipboard is Windows-only).
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

RUN_TIMEOUT_S = 30
# Opening one of these runs code, so it goes through run_command (which asks first) instead.
RUNS_CODE = (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".msix", ".scr",
             ".com", ".pif", ".hta", ".reg", ".lnk")
_URL = re.compile(r"^(https?://|www\.)\S+$|^[\w-]+(\.[\w-]+)*\.(com|org|net|io|ai|dev|gov|edu|co|uk|us|app|tv|"
                  r"me|info|biz|ca|de|fr|au|gg|ly|so|xyz|news|blog|shop|store)(/\S*)?$", re.I)
_SCHEME = re.compile(r"^(mailto:|ms-settings:|spotify:|steam:|discord:|zoommtg:|ms-[\w-]+:)", re.I)

# Virtual-key codes for press_keys.
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
      "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
      "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "printscreen": 0x2C, "capslock": 0x14,
      "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD, "play_pause": 0xB3, "next_track": 0xB0,
      "previous_track": 0xB1, "stop_media": 0xB2,
      **{f"f{n}": 0x6F + n for n in range(1, 13)},
      **{chr(c): c for c in range(ord("0"), ord("9") + 1)},
      **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)}}
MEDIA = {"volume_up": "volume_up", "louder": "volume_up", "volume_down": "volume_down", "quieter": "volume_down",
         "mute": "mute", "unmute": "mute", "play": "play_pause", "pause": "play_pause", "play_pause": "play_pause",
         "next": "next_track", "next_track": "next_track", "skip": "next_track", "previous": "previous_track",
         "previous_track": "previous_track", "back": "previous_track", "stop": "stop_media"}

_INPUT = r'''
Add-Type -Namespace JB -Name Input -MemberDefinition @"
[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, System.UIntPtr extra);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint flags, int dx, int dy, uint data, System.UIntPtr extra);
"@
'''
SCRIPTS = {
    "open_app": r'''
$t = $env:JB_A
try { Start-Process $t -ErrorAction Stop; "started $t"; exit 0 } catch {}
$app = Get-StartApps | Where-Object { $_.Name -like "*$t*" } | Select-Object -First 1
if ($app) { Start-Process ("shell:AppsFolder\" + $app.AppID); "started " + $app.Name; exit 0 }
$dirs = @("$env:APPDATA\Microsoft\Windows\Start Menu\Programs", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs")
$lnk = Get-ChildItem $dirs -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | Where-Object { $_.BaseName -like "*$t*" } | Select-Object -First 1
if ($lnk) { Start-Process $lnk.FullName; "started " + $lnk.BaseName; exit 0 }
"not found"; exit 3
''',
    "system_info": r'''
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
$disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
  @{drive=$_.DeviceID; free_gb=[math]::Round($_.FreeSpace/1GB,1); size_gb=[math]::Round($_.Size/1GB,1)} }
@{computer=$env:COMPUTERNAME; user=$env:USERNAME; windows=$os.Caption + " " + $os.Version;
  uptime_hours=[math]::Round(((Get-Date)-$os.LastBootUpTime).TotalHours,1);
  cpu=$cpu.Name; cpu_load_percent=$cpu.LoadPercentage;
  memory_gb=[math]::Round($os.TotalVisibleMemorySize/1MB,1); memory_free_gb=[math]::Round($os.FreePhysicalMemory/1MB,1);
  battery_percent=$(if ($bat) { $bat.EstimatedChargeRemaining } else { $null });
  plugged_in=$(if ($bat) { $bat.BatteryStatus -ne 1 } else { $null }); disks=@($disks)} | ConvertTo-Json -Depth 4 -Compress
''',
    "list_programs": r'''
Get-Process | Where-Object { $_.MainWindowTitle } | Sort-Object WorkingSet64 -Descending | Select-Object -First 40 |
  ForEach-Object { @{name=$_.ProcessName; id=$_.Id; window=$_.MainWindowTitle; memory_mb=[math]::Round($_.WorkingSet64/1MB)} } |
  ConvertTo-Json -Compress
''',
    "close_program": r'''
$t = $env:JB_A; $force = $env:JB_B -eq "1"
$p = @(Get-Process | Where-Object { $_.MainWindowTitle -and ($_.ProcessName -like "*$t*" -or $_.MainWindowTitle -like "*$t*") })
if (-not $p) { $p = @(Get-Process -Name $t -ErrorAction SilentlyContinue) }
if (-not $p) { "not found"; exit 3 }
foreach ($x in $p) { if ($force) { Stop-Process -Id $x.Id -Force } else { [void]$x.CloseMainWindow() } }
($p | ForEach-Object { $_.ProcessName } | Select-Object -Unique) -join ", "
''',
    "get_clipboard": "Get-Clipboard -Raw",
    "set_clipboard": "Set-Clipboard -Value $env:JB_A; 'ok'",
    "screenshot": r'''
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save($env:JB_A, [System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose()
"$($b.Width)x$($b.Height)"
''',
    "type_text": "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait($env:JB_A); 'ok'",
    "press_keys": _INPUT + r'''
$keys = $env:JB_A -split "," | ForEach-Object { [byte][int]$_ }
foreach ($k in $keys) { [JB.Input]::keybd_event($k, 0, 0, [System.UIntPtr]::Zero) }
[array]::Reverse($keys)
foreach ($k in $keys) { [JB.Input]::keybd_event($k, 0, 2, [System.UIntPtr]::Zero) }
"ok"
''',
    "click": _INPUT + r'''
[void][JB.Input]::SetCursorPos([int]$env:JB_A, [int]$env:JB_B)
$down, $up = if ($env:JB_C -eq "right") { 8, 16 } else { 2, 4 }
for ($i = 0; $i -lt [int]$env:JB_D; $i++) {
  [JB.Input]::mouse_event($down, 0, 0, 0, [System.UIntPtr]::Zero); [JB.Input]::mouse_event($up, 0, 0, 0, [System.UIntPtr]::Zero)
  Start-Sleep -Milliseconds 80 }
"ok"
''',
}


def sendkeys_escape(text: str) -> str:
    """Typed text for SendKeys: its special characters are typed as themselves, new lines as Enter."""
    out = re.sub(r"([+^%~(){}\[\]])", r"{\1}", text)
    return out.replace("\r\n", "\n").replace("\n", "{ENTER}").replace("\t", "{TAB}")


def key_codes(combo: str) -> list[int]:
    """'ctrl+shift+s', 'alt+f4', 'win+d', 'enter', 'volume_up' -> virtual-key codes, modifiers first."""
    names = [k.strip().lower().replace(" ", "") for k in re.split(r"\s*\+\s*", combo or "") if k.strip()]
    if not names:
        raise ValueError("say which keys to press, like ctrl+s or alt+tab")
    unknown = [n for n in names if n not in VK]
    if unknown:
        raise ValueError(f"unknown key {unknown[0]!r}: use names like ctrl, shift, alt, win, enter, tab, esc, "
                         "f1-f12, up/down/left/right, a-z, 0-9")
    if len(names) > 4:
        raise ValueError("at most 4 keys at once")
    return [VK[n] for n in names]


class Desktop:
    def __init__(self, work_dir: Path, platform: str = sys.platform, runner: Optional[Callable] = None,
                 opener: Optional[Callable[[str], None]] = None):
        self.shots = work_dir / "screenshots"
        self.windows = platform == "win32"
        self.runner = runner or self._powershell
        self.opener = opener or self._open_default

    # ---- plumbing ---------------------------------------------------------------------
    @staticmethod
    def _powershell(script: str, env: dict) -> tuple[int, str, str]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
                              capture_output=True, timeout=RUN_TIMEOUT_S, env={**os.environ, **env},
                              stdin=subprocess.DEVNULL, creationflags=flags)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def _run(self, name: str, **args) -> str:
        if not self.windows:
            raise ValueError("this only works on Windows")
        env = {f"JB_{k}": str(v) for k, v in args.items()}
        code, out, err = self.runner(SCRIPTS[name], env)
        out = out.strip()
        if code == 3:
            raise LookupError(out or "not found")
        if code != 0:
            raise RuntimeError((err or out).strip().splitlines()[-1] if (err or out).strip() else f"exit {code}")
        return out

    def _open_default(self, target: str) -> None:
        if self.windows:
            os.startfile(target)  # type: ignore[attr-defined]  # Windows-only
        elif not webbrowser.open(target):
            subprocess.Popen(["xdg-open", target])

    # ---- opening and closing ------------------------------------------------------------
    def open(self, target: str) -> dict:
        t = (target or "").strip().strip('"')
        if not t:
            raise ValueError("say what to open: a program, a website, a file or a folder")
        if _URL.match(t) and not Path(os.path.expandvars(t)).exists():
            url = t if t.lower().startswith("http") else "https://" + t
            self.opener(url)
            return {"opened": url, "kind": "website"}
        if _SCHEME.match(t):
            self.opener(t)
            return {"opened": t, "kind": "link"}
        p = Path(os.path.expandvars(os.path.expanduser(t)))
        if p.exists():
            if p.is_file() and p.suffix.lower() in RUNS_CODE:
                raise ValueError(f"{p.name} is a program or script: run it with run_command so the author sees it")
            self.opener(str(p))
            return {"opened": str(p), "kind": "folder" if p.is_dir() else "file"}
        if "\\" in t or "/" in t:
            raise LookupError(f"nothing at {t}")
        try:
            started = self._run("open_app", A=t)
        except LookupError:
            raise LookupError(f"couldn't find a program called {t!r}; try its full name, or search_programs") from None
        return {"opened": started.removeprefix("started ").strip(), "kind": "program"}

    def close(self, name: str, force: bool = False) -> dict:
        n = (name or "").strip()
        if not n or re.search(r"[*?]", n) or n.lower() in ("explorer", "winlogon", "csrss", "svchost", "lsass"):
            raise ValueError("say which program to close by its name")
        try:
            closed = self._run("close_program", A=n, B="1" if force else "0")
        except LookupError:
            raise LookupError(f"no open program called {n!r} (list_programs shows what is open)") from None
        return {"closed": closed, "forced": force,
                **({} if force else {"note": "asked it to close; it may ask to save first"})}

    # ---- facts ---------------------------------------------------------------------------
    def info(self) -> dict:
        if not self.windows:
            import platform
            import shutil
            total, _, free = shutil.disk_usage(Path.home())
            return {"system": platform.platform(), "home_disk_free_gb": round(free / 1e9, 1),
                    "home_disk_size_gb": round(total / 1e9, 1), "cpus": os.cpu_count()}
        return json.loads(self._run("system_info") or "{}")

    def programs(self) -> dict:
        raw = self._run("list_programs")
        data = json.loads(raw) if raw else []
        return {"open_programs": data if isinstance(data, list) else [data]}

    # ---- clipboard ---------------------------------------------------------------------
    def get_clipboard(self) -> dict:
        text = self._run("get_clipboard")
        return {"clipboard": text[:20000], **({"more": True} if len(text) > 20000 else {})}

    def set_clipboard(self, text: str) -> dict:
        if not text:
            raise ValueError("nothing to copy")
        self._run("set_clipboard", A=text)
        return {"copied": len(text)}

    # ---- screen ------------------------------------------------------------------------
    def screenshot(self) -> dict:
        self.shots.mkdir(parents=True, exist_ok=True)
        for old in sorted(self.shots.glob("*.png"))[:-19]:  # keep the last 20
            old.unlink(missing_ok=True)
        path = self.shots / time.strftime("screen-%Y%m%d-%H%M%S.png")
        size = self._run("screenshot", A=str(path))
        if not path.exists():
            raise RuntimeError("the screenshot was not saved")
        w, _, h = size.partition("x")
        return {"path": str(path), "width": int(w or 0), "height": int(h or 0)}

    @staticmethod
    def image_part(path: str) -> dict:
        return {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(Path(path).read_bytes()).decode()}}

    # ---- keyboard and mouse ------------------------------------------------------------------
    def type_text(self, text: str) -> dict:
        if not text:
            raise ValueError("nothing to type")
        if len(text) > 5000:
            raise ValueError("at most 5,000 characters at a time")
        self._run("type_text", A=sendkeys_escape(text))
        return {"typed": len(text)}

    def press(self, combo: str) -> dict:
        codes = key_codes(combo)
        self._run("press_keys", A=",".join(str(c) for c in codes))
        return {"pressed": combo}

    def media(self, action: str, times: int = 1) -> dict:
        key = MEDIA.get((action or "").strip().lower().replace(" ", "_"))
        if key is None:
            raise ValueError("action must be one of: " + ", ".join(sorted(set(MEDIA))))
        times = max(1, min(int(times or 1), 50))
        code = str(VK[key])
        for _ in range(times):
            self._run("press_keys", A=code)
        return {"done": key, "times": times}

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> dict:
        if button not in ("left", "right"):
            raise ValueError("button must be left or right")
        clicks = max(1, min(int(clicks or 1), 3))
        self._run("click", A=int(x), B=int(y), C=button, D=clicks)
        return {"clicked": [int(x), int(y)], "button": button, "clicks": clicks}
