"""Turn audiobook audio into an upload-ready MP4: cover or backdrop, title text and a moving waveform."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from jarvis_books.audio.export import ffmpeg_path

BACKDROP = "0x0f1b2d"
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


def find_font() -> Optional[str]:
    return next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)


def _filter_path(path) -> str:
    """Quote a file path for an ffmpeg filter argument (Windows drive colons and backslashes)."""
    return "'" + str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'") + "'"


def format_timestamp(ms: int) -> str:
    s = int(ms // 1000)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def chapter_timestamps(chapters: list[tuple[str, int]]) -> str:
    """YouTube chapter list for a description. YouTube needs 3+ chapters, starting at 0:00, each 10 s or longer."""
    if len(chapters) < 3 or any(d < 10_000 for _, d in chapters):
        return ""
    lines, start = [], 0
    for heading, duration in chapters:
        lines.append(f"{format_timestamp(start)} {heading}")
        start += duration
    return "\n".join(lines)


def render_video(audio: Path, out: Path, title: str, subtitle: str = "", cover: Optional[Path] = None,
                 duration_ms: int = 0, size: tuple[int, int] = (1280, 720), fps: int = 10, waveform: bool = True,
                 progress: Optional[Callable[[float], None]] = None, cancel: Optional[threading.Event] = None) -> Path:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available")
    w, h = size
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        if cover and Path(cover).exists():
            background = ["-loop", "1", "-framerate", str(fps), "-i", str(cover)]
        else:
            background = ["-f", "lavfi", "-i", f"color=c={BACKDROP}:s={w}x{h}:r={fps}"]
        graph = [f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:"
                 f"color={BACKDROP},fps={fps},format=yuv420p[bg]"]
        current = "bg"
        font = find_font()
        if font:
            for i, (text, frac, fs) in enumerate(((title, 0.26, 58 if len(title) <= 32 else 40),
                                                  (subtitle, 0.42, 34))):
                if not text:
                    continue
                tf = tmp / f"text{i}.txt"
                tf.write_text(text, encoding="utf-8")
                graph.append(f"[{current}]drawtext=fontfile={_filter_path(font)}:textfile={_filter_path(tf)}:"
                             f"fontcolor=white:fontsize={fs}:x=(w-text_w)/2:y=h*{frac}:box=1:boxcolor=black@0.45:"
                             f"boxborderw=18[t{i}]")
                current = f"t{i}"
        if waveform:
            graph.append(f"[1:a]showwaves=s={w}x{h // 5}:mode=cline:rate={fps}:colors=0x90cdf4,format=yuva420p[wv]")
            graph.append(f"[{current}][wv]overlay=0:H-h-{h // 12}:shortest=1[v]")
        else:
            graph.append(f"[{current}]null[v]")
        args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *background, "-i", str(audio),
                "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "1:a", "-c:v", "libx264",
                "-preset", "veryfast", "-tune", "stillimage", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "128k", "-shortest", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        with open(tmp / "ffmpeg.log", "w+", encoding="utf-8", errors="replace") as errlog:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errlog, text=True, creationflags=flags)
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise RuntimeError("video rendering cancelled")
                if progress and duration_ms and line.startswith("out_time_us="):
                    try:
                        progress(min(1.0, int(line.split("=", 1)[1]) / 1000 / duration_ms))
                    except ValueError:
                        pass
            if proc.wait() != 0:
                errlog.seek(0)
                raise RuntimeError("ffmpeg could not make the video: " + errlog.read()[-800:])
    return out
