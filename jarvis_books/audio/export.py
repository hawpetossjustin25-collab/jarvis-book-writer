"""Export audiobooks as one MP3 per chapter or a single chaptered M4B (FFmpeg)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from jarvis_books.audio.narrator import Narrator
from jarvis_books.text import safe_filename

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW on Windows


def ffmpeg_path() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _run(args: list[str]) -> None:
    import sys
    flags = NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run(args, capture_output=True, text=True, creationflags=flags)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + proc.stderr[-800:])


def _probe_ms(ffmpeg: str, path: Path) -> int:
    import re
    import sys
    flags = NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, creationflags=flags)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if not m:
        return 0
    h, mnt, s = m.groups()
    return int((int(h) * 3600 + int(mnt) * 60 + float(s)) * 1000)


def _escape(value: str) -> str:
    return "".join("\\" + c if c in "=;#\\\n" else c for c in value)


def export_audiobook(narrator: Narrator, book_id: str, out_dir: Path, fmt: str = "m4b", progress=None,
                     cancel=None) -> Path:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available")
    book = narrator.library.get_book(book_id)
    segments = narrator.book_segments(book_id)
    if not segments:
        raise RuntimeError("the book has no written chapters yet")
    narrator.prepare(segments, cancel=cancel, progress=(lambda p, m: progress(p * 0.8, m)) if progress else None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters = {c.number: c for c in narrator.library.list_chapters(book_id)}
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        chapter_files: list[tuple[int, Path]] = []
        for number in sorted({s.chapter_number for s in segments}):
            listing = tmp / f"ch{number}.txt"
            files = [narrator.ensure(s) for s in segments if s.chapter_number == number]
            listing.write_text("".join(f"file '{str(f).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                                       for f in files), encoding="utf-8")
            wav = tmp / f"ch{number}.wav"
            _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                  "-ar", "24000", "-ac", "1", str(wav)])
            chapter_files.append((number, wav))
        if progress:
            progress(0.85, "Encoding")
        if fmt == "mp3":
            folder = out_dir / f"{safe_filename(book.title)} - Audiobook"
            folder.mkdir(exist_ok=True)
            for number, wav in chapter_files:
                title = chapters[number].heading
                _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-codec:a", "libmp3lame",
                      "-b:a", "96k", "-metadata", f"title={title}", "-metadata", f"album={book.title}",
                      "-metadata", f"track={number}", str(folder / f"{number:03d} - {safe_filename(title)}.mp3")])
            return folder
        listing = tmp / "all.txt"
        listing.write_text("".join(f"file '{w}'\n" for _, w in chapter_files), encoding="utf-8")
        meta = [";FFMETADATA1", f"title={_escape(book.title)}", f"album={_escape(book.title)}", "genre=Audiobook"]
        start = 0
        for number, wav in chapter_files:
            dur = _probe_ms(ffmpeg, wav)
            meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={start + dur}",
                     f"title={_escape(chapters[number].heading)}"]
            start += dur
        meta_file = tmp / "meta.txt"
        meta_file.write_text("\n".join(meta) + "\n", encoding="utf-8")
        target = out_dir / f"{safe_filename(book.title)}.m4b"
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-i", str(meta_file), "-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac", "-b:a", "64k",
              "-movflags", "+faststart", "-f", "mp4", str(target)])
        if progress:
            progress(1.0, "Audiobook ready")
        return target
