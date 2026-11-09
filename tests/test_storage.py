import pytest

from jarvis_books.errors import NotFound, ValidationError
from jarvis_books.storage.capacity import CapacityMonitor
from jarvis_books.storage.mirror import BackupManager, BookMirror


def test_book_chapter_revision_roundtrip(library):
    book = library.create_book("The Midnight Vault", "thriller", "a heist")
    chapters = library.set_outline(book.id, [{"number": 1, "title": "Rain", "outline": "Intro"},
                                             {"number": 2, "title": "Vault", "outline": "Break-in"}])
    assert [c.number for c in chapters] == [1, 2]
    ch = library.get_chapter(book.id, 1)
    library.save_chapter_text(ch.id, "First draft.\r\n\r\nMore.")
    library.save_chapter_text(ch.id, "First draft.\r\n\r\nMore.")  # unchanged text adds no revision
    library.save_chapter_text(ch.id, "Second draft words here.", source="ai-draft")
    revs = library.revisions(ch.id)
    assert len(revs) == 2
    assert library.get_chapter(book.id, 1).word_count == 4
    restored = library.restore_revision(min(revs, key=lambda r: r.created_at).id)
    assert "\r" not in restored.text and restored.text.startswith("First draft.")
    assert len(library.revisions(ch.id)) == 3  # restoring keeps history


def test_find_book_fuzzy_and_missing(library):
    library.create_book("The Midnight Vault", "thriller")
    library.create_book("Dragons of Ash", "fantasy")
    assert library.find_book("midnight vault").title == "The Midnight Vault"
    assert library.find_book("dragon").title == "Dragons of Ash"
    with pytest.raises(NotFound):
        library.get_book("nope")


def test_bible_upsert_merges_case_insensitively(library):
    book = library.create_book("B", "mystery")
    library.upsert_bible(book.id, "character", "Mara", "thief", {"age": 30})
    entry = library.upsert_bible(book.id, "character", "mara", "", {"eyes": "green"})
    assert entry.summary == "thief" and entry.data == {"age": 30, "eyes": "green"}
    assert len(library.list_bible(book.id, "character")) == 1


def test_invalid_values_rejected(library):
    assert library.create_book("", "thriller").title == "Untitled Book"
    with pytest.raises(ValidationError):
        library.create_book("X", "thriller", status="bogus")
    with pytest.raises(ValidationError):
        library.create_book("X", "thriller", target_words=-5)


def test_mirror_and_backup(library, tmp_path):
    mirror = BookMirror(library, tmp_path / "mirror", debounce_s=0)
    mirror.start()
    book = library.create_book("Mirror Book", "romance")
    library.set_outline(book.id, [{"number": 1, "title": "Meet", "outline": "They meet"}])
    library.save_chapter_text(library.get_chapter(book.id, 1).id, "Hello there.")
    mirror.flush()
    files = {p.name for p in (tmp_path / "mirror").rglob("*.md")}
    assert "Outline.md" in files and any("Meet" in f for f in files)
    manuscript = next(p for p in (tmp_path / "mirror").rglob("*.md") if "Manuscript" in p.name or "Full" in p.name)
    assert "Hello there." in manuscript.read_text(encoding="utf-8")
    backups = BackupManager(library, tmp_path / "backups", keep=2)
    for i in range(3):
        backups.backup(now=1_700_000_000 + i * 60)
    assert len(list((tmp_path / "backups").glob("library-*.db"))) == 2


def test_capacity_evicts_only_audio(library, tmp_path):
    book = library.create_book("Audio", "horror")
    library.set_outline(book.id, [{"number": 1, "title": "One", "outline": "x"}])
    ch = library.get_chapter(book.id, 1)
    library.save_chapter_text(ch.id, "Words " * 50)
    cache = tmp_path / "audio"
    cache.mkdir()
    for i in range(5):
        f = cache / f"seg{i}.wav"
        f.write_bytes(b"0" * 1000)
        library.db.execute("INSERT INTO audio_segments (chapter_id, ord, text_hash, voice_key, path, bytes, "
                           "duration_ms, created_at, last_used) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ch.id, i, f"h{i}", "v", str(f), 1000, 100, i, i))
    monitor = CapacityMonitor(library, cache, audio_cap_bytes=2500)
    freed = monitor.enforce()
    assert freed == 3000
    assert len(list(cache.glob("*.wav"))) == 2
    assert library.get_chapter(book.id, 1).word_count == 50
    assert monitor.report().describe()
