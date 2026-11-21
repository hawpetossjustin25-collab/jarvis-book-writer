"""Reading scanned PDFs offline with the text reader built into Windows (Windows.Media.Ocr), no Gemini needed."""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

SCRIPT = r"""
param([string]$Pdf, [string]$Out)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$ext = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 }
$opTask = $ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$actTask = $ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1
function Await($op, [Type]$type) { $t = $opTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
function AwaitAction($op) { $actTask.Invoke($null, @($op)).Wait(-1) | Out-Null }
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Pdf)) ([Windows.Storage.StorageFile])
$doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw 'Windows has no text-reading (OCR) language installed' }
$words = New-Object System.Collections.ArrayList
for ($i = 0; $i -lt $doc.PageCount; $i++) {
  $page = $doc.GetPage($i)
  $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
  $opts = New-Object Windows.Data.Pdf.PdfPageRenderOptions
  $opts.DestinationWidth = [uint32]2400
  AwaitAction ($page.RenderToStreamAsync($stream, $opts))
  $stream.Seek(0)
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  foreach ($line in $result.Lines) { foreach ($w in $line.Words) {
    [void]$words.Add([ordered]@{ p = $i; text = $w.Text; x = $w.BoundingRect.X; y = $w.BoundingRect.Y;
                                 w = $w.BoundingRect.Width; h = $w.BoundingRect.Height }) } }
}
ConvertTo-Json -InputObject @($words) -Depth 3 -Compress | Out-File -Encoding utf8 $Out
"""

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def lines(words: list[dict]) -> list[str]:
    """Words from one page put back into text lines by height on the page, left to right."""
    if not words:
        return []
    tol = statistics.median(max(1.0, float(w["h"])) for w in words) * 0.6
    rows: list[list] = []
    for w in sorted(words, key=lambda w: float(w["y"]) + float(w["h"]) / 2):
        mid = float(w["y"]) + float(w["h"]) / 2
        if rows and abs(rows[-1][0] - mid) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([mid, [w]])
    return [" ".join(str(x["text"]) for x in sorted(r[1], key=lambda w: float(w["x"]))) for r in rows]


def pages_of(words: list[dict]) -> list[list[str]]:
    by_page: dict[int, list[dict]] = {}
    for w in words:
        by_page.setdefault(int(w.get("p", 0)), []).append(w)
    return [lines(by_page[p]) for p in sorted(by_page)]


def _powershell() -> list[str]:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return [str(exe) if exe.exists() else "powershell"]


def windows_ocr(pdf: bytes, run: Optional[Runner] = None, timeout_s: int = 300) -> list[list[str]]:
    """The text lines of every page of a PDF, read by Windows' own OCR."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    with tempfile.TemporaryDirectory() as tmp:
        src, script, out = Path(tmp) / "list.pdf", Path(tmp) / "ocr.ps1", Path(tmp) / "words.json"
        src.write_bytes(pdf)
        script.write_text(SCRIPT, encoding="utf-8")
        done = run(_powershell() + ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                    "-Pdf", str(src), "-Out", str(out)])
        if done.returncode != 0 or not out.exists():
            err = (done.stderr or done.stdout or "").strip().splitlines()
            raise RuntimeError("Windows couldn't read the PDF: " + (err[-1] if err else "no output"))
        words = json.loads(out.read_text(encoding="utf-8-sig") or "[]")
    return pages_of(words if isinstance(words, list) else [words])


def available() -> bool:
    return sys.platform == "win32"


SALE = re.compile(r"\b\d{4}\s*-\s*\d{5}\b")
PARCEL = re.compile(r"(\d{2}[A-Z0-9]?)\s*-\s*(\d{4})\s*-\s*([A-Z0-9]{1,4})\s*-\s*(\d{3})\s*-\s*(\d)\b")


def fulton_rows(pages: list[list[str]]) -> list[dict]:
    """Sheriff's sale rows (sale number, parcel, address) from the OCR text of Fulton's sale list."""
    out = []
    for text in (line for page in pages for line in page):
        p = PARCEL.search(text)
        if not p:
            continue
        sale = SALE.search(text[:p.start()])
        address = re.sub(r"[|\[\]_]+", " ", text[p.end():])
        address = re.sub(r"\s+", " ", address).strip(" .,")
        out.append({"sale_number": re.sub(r"\s", "", sale.group(0)) if sale else "",
                    "parcel": "-".join(p.groups()), "address": address})
    return out
