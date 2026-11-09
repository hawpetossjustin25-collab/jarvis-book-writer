import zipfile

import pytest

from jarvis_books.audio import Narrator
from jarvis_books.audio.export import export_audiobook, ffmpeg_path
from jarvis_books.export import export_docx, export_epub, export_markdown
from jarvis_books.llm.errors import LLMError


def make_book(library):
    book = library.create_book("Night Train", "thriller", "a chase")
    library.set_outline(book.id, [{"number": 1, "title": "Departure", "outline": "x"},
                                  {"number": 2, "title": "Tunnel", "outline": "y"},
                                  {"number": 3, "title": "Unwritten", "outline": "z"}])
    library.save_chapter_text(library.get_chapter(book.id, 1).id,
                              "The train left at midnight.\n\n* * *\n\nMara found the seat empty.")
    library.save_chapter_text(library.get_chapter(book.id, 2).id, "The tunnel swallowed the light.")
    return book


def test_unfinished_book_narrates_written_chapters_with_cache(library, engine, fake, tmp_path):
    book = make_book(library)
    narrator = Narrator(library, engine, tmp_path / "audio", edge_fallback=False)
    segs = narrator.book_segments(book.id)
    assert {s.chapter_number for s in segs} == {1, 2}  # chapter 3 is unwritten
    assert segs[0].text == "Chapter 1: Departure."
    assert narrator.prepare(segs) == len(segs)
    calls = fake.tts_calls
    assert narrator.prepare(segs) == 0 and fake.tts_calls == calls  # fully cached
    ch2 = library.get_chapter(book.id, 2)
    library.save_chapter_text(ch2.id, "The tunnel swallowed the light. A whistle screamed.")
    assert narrator.prepare(narrator.book_segments(book.id)) == 1  # only the edited passage
    assert narrator.prune_orphans(book.id) == 1


def test_edge_fallback_when_gemini_quota_is_used_up(library, engine, tmp_path, monkeypatch):
    book = make_book(library)

    def no_quota(*a, **k):
        raise LLMError("quota", kind="quota")
    monkeypatch.setattr(engine, "tts", no_quota)
    narrator = Narrator(library, engine, tmp_path / "audio", edge_synth=lambda text, voice: b"ID3fake-mp3")
    path = narrator.ensure(narrator.book_segments(book.id)[0])
    assert path.suffix == ".mp3" and narrator.last_backend == "edge"


def test_auth_error_is_not_hidden_by_fallback(library, engine, tmp_path, monkeypatch):
    book = make_book(library)

    def bad(*a, **k):
        raise LLMError("bad key", kind="auth")
    monkeypatch.setattr(engine, "tts", bad)
    narrator = Narrator(library, engine, tmp_path / "audio", edge_synth=lambda t, v: b"x")
    with pytest.raises(LLMError):
        narrator.ensure(narrator.book_segments(book.id)[0])


@pytest.mark.skipif(ffmpeg_path() is None, reason="FFmpeg not installed")
def test_m4b_has_chapter_markers(library, engine, tmp_path):
    import subprocess
    book = make_book(library)
    narrator = Narrator(library, engine, tmp_path / "audio", edge_fallback=False)
    out = export_audiobook(narrator, book.id, tmp_path / "out", "m4b")
    assert out.suffix == ".m4b" and out.stat().st_size > 0
    probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "Chapter 1: Departure" in probe and "Chapter 2: Tunnel" in probe
    mp3_dir = export_audiobook(narrator, book.id, tmp_path / "out", "mp3")
    assert len(list(mp3_dir.glob("*.mp3"))) == 2


def test_docx_manuscript_format(library, tmp_path):
    from docx import Document
    book = make_book(library)
    path = export_docx(library, book.id, tmp_path, author="Justin")
    doc = Document(str(path))
    texts = [p.text for p in doc.paragraphs]
    assert "NIGHT TRAIN" in texts and "Chapter 1: Departure" in texts and "#" in texts and "END" in texts
    assert doc.styles["Normal"].font.name == "Times New Roman"
    body = next(p for p in doc.paragraphs if p.text.startswith("The train"))
    assert body.paragraph_format.first_line_indent is not None
    assert abs(doc.sections[0].left_margin.inches - 1.0) < 0.01


def test_epub_structure(library, tmp_path):
    book = make_book(library)
    path = export_epub(library, book.id, tmp_path, author="Justin")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert names[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert {"OEBPS/content.opf", "OEBPS/nav.xhtml", "OEBPS/ch001.xhtml", "OEBPS/ch002.xhtml"} <= set(names)
        assert "ch003.xhtml" not in " ".join(names)
        assert "Mara found the seat empty." in z.read("OEBPS/ch001.xhtml").decode()


def test_markdown_export(library, tmp_path):
    book = make_book(library)
    text = export_markdown(library, book.id, tmp_path).read_text(encoding="utf-8")
    assert "## Chapter 2: Tunnel" in text and "Unwritten" not in text
