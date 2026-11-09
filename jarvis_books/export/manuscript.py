"""Manuscript exports: DOCX in standard submission format, EPUB 3 e-book, and Markdown."""
from __future__ import annotations

import html
import uuid
import zipfile
from pathlib import Path

from jarvis_books.craft.manuscript import MANUSCRIPT_FORMAT, SCENE_BREAK
from jarvis_books.storage.library import Library
from jarvis_books.text import paragraphs, safe_filename, word_count


def _written(library: Library, book_id: str):
    return [c for c in library.list_chapters(book_id) if c.text.strip()]


def export_markdown(library: Library, book_id: str, out_dir: Path) -> Path:
    book = library.get_book(book_id)
    parts = [f"# {book.title}", ""]
    for c in _written(library, book_id):
        parts += [f"## {c.heading}", "", c.text.strip(), ""]
    path = Path(out_dir) / f"{safe_filename(book.title)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def export_docx(library: Library, book_id: str, out_dir: Path, author: str = "") -> Path:
    """Standard manuscript format (Shunn-style): 12 pt Times New Roman, double spaced, 1" margins,
    0.5" first-line indents, title page with word count, each chapter on a new page."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.shared import Inches, Pt

    fmt = MANUSCRIPT_FORMAT
    book = library.get_book(book_id)
    chapters = _written(library, book_id)
    total = sum(word_count(c.text) for c in chapters)
    doc = Document()
    doc.core_properties.title = book.title
    doc.core_properties.author = author
    style = doc.styles["Normal"]
    style.font.name = fmt["font"]
    style.font.size = Pt(fmt["font_size_pt"])
    pf = style.paragraph_format
    pf.line_spacing = fmt["line_spacing"]
    pf.space_after = Pt(0)
    pf.space_before = Pt(0)
    for section in doc.sections:
        for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            setattr(section, side, Inches(fmt["margins_in"]))
        header = section.header.paragraphs[0]
        header.text = f"{author + ' / ' if author else ''}{book.title.upper()[:30]}"
        header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    top = doc.add_paragraph(f"{author}\nAbout {round(total, -2) if total >= 1000 else total:,} words")
    top.paragraph_format.line_spacing = 1.0
    for _ in range(8):
        doc.add_paragraph()
    t = doc.add_paragraph(book.title.upper())
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if author:
        a = doc.add_paragraph(f"by {author}")
        a.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for chapter in chapters:
        brk = doc.add_paragraph()
        brk.add_run().add_break(WD_BREAK.PAGE)
        for _ in range(fmt.get("chapter_drop_lines", 4)):
            doc.add_paragraph()
        h = doc.add_paragraph(chapter.heading)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        doc.add_paragraph()
        for para in paragraphs(chapter.text):
            if para.strip() in (SCENE_BREAK, "***", "* * *", "#"):
                p = doc.add_paragraph("#")
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                continue
            p = doc.add_paragraph(para.replace("\n", " "))
            p.paragraph_format.first_line_indent = Inches(fmt["first_line_indent_in"])
    end = doc.add_paragraph("END")
    end.alignment = WD_ALIGN_PARAGRAPH.CENTER
    path = Path(out_dir) / f"{safe_filename(book.title)}.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


_CSS = ("body{font-family:serif;line-height:1.5;margin:0 5%}h1,h2{text-align:center;margin:2em 0 1em}"
        "p{text-indent:1.5em;margin:0}p.first{text-indent:0}p.break{text-align:center;text-indent:0;margin:1em 0}")


def export_epub(library: Library, book_id: str, out_dir: Path, author: str = "") -> Path:
    book = library.get_book(book_id)
    chapters = _written(library, book_id)
    uid = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'jarvis-books/' + book.id)}"
    esc = html.escape
    path = Path(out_dir) / f"{safe_filename(book.title)}.epub"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                   'container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/'
                   'oebps-package+xml"/></rootfiles></container>', compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", _CSS, compress_type=zipfile.ZIP_DEFLATED)
        items, spine, nav = [], [], []
        for c in chapters:
            name = f"ch{c.number:03d}.xhtml"
            body, first = [], True
            for para in paragraphs(c.text):
                if para.strip() in (SCENE_BREAK, "***", "* * *", "#"):
                    body.append('<p class="break">* * *</p>')
                    first = True
                    continue
                cls = ' class="first"' if first else ""
                body.append(f"<p{cls}>{esc(para)}</p>")
                first = False
            z.writestr(f"OEBPS/{name}",
                       '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/'
                       f'xhtml"><head><title>{esc(c.heading)}</title><link rel="stylesheet" href="style.css"/></head>'
                       f'<body><h2>{esc(c.heading)}</h2>{"".join(body)}</body></html>',
                       compress_type=zipfile.ZIP_DEFLATED)
            items.append(f'<item id="c{c.number}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{c.number}"/>')
            nav.append(f'<li><a href="{name}">{esc(c.heading)}</a></li>')
        z.writestr("OEBPS/nav.xhtml",
                   '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" '
                   'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body>'
                   f'<nav epub:type="toc"><h1>Contents</h1><ol>{"".join(nav)}</ol></nav></body></html>',
                   compress_type=zipfile.ZIP_DEFLATED)
        import datetime as dt
        modified = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                   'unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f'<dc:identifier id="bookid">{uid}</dc:identifier><dc:title>{esc(book.title)}</dc:title>'
                   f'<dc:creator>{esc(author or "Unknown")}</dc:creator><dc:language>en</dc:language>'
                   f'<meta property="dcterms:modified">{modified}</meta></metadata><manifest>'
                   '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                   '<item id="css" href="style.css" media-type="text/css"/>'
                   f'{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>',
                   compress_type=zipfile.ZIP_DEFLATED)
    return path
