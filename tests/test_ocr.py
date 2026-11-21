"""Offline reading of scanned lists with Windows' own OCR, and turning its words into Fulton sale rows."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jarvis_books.computer import ocr


def word(text, x, y, p=0, h=30):
    return {"p": p, "text": text, "x": x, "y": y, "w": 10 * len(text), "h": h}


ROW_WORDS = [  # one table row whose words OCR returned out of order and a few pixels apart in height
    word("605", 1300, 2203), word("1226-57192", 380, 2200), word("14", 800, 2201), word("-0176-0018-055-3", 850, 2198),
    word("SOUTH", 1400, 2202), word("EUGENIA", 1520, 2199), word("PL", 1700, 2200), word("NW", 1760, 2201),
    word("1226-57113", 380, 2300), word("14", 800, 2300), word("-0008-", 850, 2300), word("LL-023-6", 1000, 2301),
    word("1600", 1300, 2299), word("THOMASVILLE", 1400, 2300), word("BLVD", 1700, 2300), word("SE.", 1800, 2300),
    word("SHERIFF'S", 380, 1800), word("TAX", 800, 1800), word("PARCEL", 900, 1800), word("ID", 1000, 1800),
    word("1226-58202", 380, 300, p=4), word("17", 800, 300, p=4), word("-0099-", 850, 300, p=4),
    word("LL-134-4", 1000, 300, p=4), word("3040", 1300, 300, p=4), word("PEACHTREE", 1400, 300, p=4),
    word("RD", 1600, 300, p=4), word("NW,", 1660, 300, p=4), word("#614", 1730, 300, p=4),
]


def test_words_are_put_back_into_lines_per_page():
    pages = ocr.pages_of(ROW_WORDS)
    assert len(pages) == 2
    assert pages[0] == ["SHERIFF'S TAX PARCEL ID", "1226-57192 14 -0176-0018-055-3 605 SOUTH EUGENIA PL NW",
                        "1226-57113 14 -0008- LL-023-6 1600 THOMASVILLE BLVD SE."]
    assert ocr.lines([]) == []


def test_fulton_rows_come_out_of_the_ocr_text():
    rows = ocr.fulton_rows(ocr.pages_of(ROW_WORDS))
    assert rows == [
        {"sale_number": "1226-57192", "parcel": "14-0176-0018-055-3", "address": "605 SOUTH EUGENIA PL NW"},
        {"sale_number": "1226-57113", "parcel": "14-0008-LL-023-6", "address": "1600 THOMASVILLE BLVD SE"},
        {"sale_number": "1226-58202", "parcel": "17-0099-LL-134-4", "address": "3040 PEACHTREE RD NW, #614"}]
    from jarvis_books.computer.taxliens import fulton_parcel
    assert fulton_parcel(rows[1]["parcel"]) == "14 0008  LL0236"


def test_windows_ocr_runs_the_script_and_reads_its_words():
    seen = {}

    def run(cmd):
        seen["cmd"] = cmd
        script = Path(cmd[cmd.index("-File") + 1])
        assert "Windows.Media.Ocr" in script.read_text() and Path(cmd[cmd.index("-Pdf") + 1]).read_bytes() == b"%PDF"
        Path(cmd[cmd.index("-Out") + 1]).write_text("\ufeff" + json.dumps(ROW_WORDS[:8]), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    pages = ocr.windows_ocr(b"%PDF", run=run)
    assert pages == [["1226-57192 14 -0176-0018-055-3 605 SOUTH EUGENIA PL NW"]]
    assert "-ExecutionPolicy" in seen["cmd"] and "-NoProfile" in seen["cmd"]


def test_a_single_word_page_and_failures_are_handled():
    def one(cmd):
        Path(cmd[cmd.index("-Out") + 1]).write_text(json.dumps(word("HELLO", 1, 1)), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    assert ocr.windows_ocr(b"%PDF", run=one) == [["HELLO"]]

    def broken(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "line 1\nWindows has no text-reading (OCR) language installed")
    with pytest.raises(RuntimeError, match="no text-reading"):
        ocr.windows_ocr(b"%PDF", run=broken)


def test_every_row_of_the_real_december_list_survives_ocr_misreads():
    """Windows OCR's real text of Fulton's Dec 2026 list: O for 0, l for 1, LI for LL, long dashes,
    underscores and stray spaces inside the parcel ID must not lose a property."""
    lines = (Path(__file__).parent / "fixtures" / "fulton_ocr_lines.txt").read_text(encoding="utf-8").splitlines()
    rows = ocr.fulton_rows([lines])
    assert len(rows) == 110
    assert len({r["sale_number"] for r in rows}) == 110
    got = {r["sale_number"]: (r["parcel"], r["address"]) for r in rows}
    assert got["1226-58005"] == ("09C-0100-0003-025-1", "9955 CEDAR GROVE RD")
    assert got["1226-58025"] == ("12-2044-0476-021-0", "335 SADDLE CREEK DR")
    assert got["1226-58041"] == ("13-0133-LL-276-0", "340 DRESSAGE CT")
    assert got["1226-58056"] == ("14-0049-0001-549-9", "860 PEACHTREE ST 2318")
    assert got["1226-58145"] == ("14F-0024-0002-015-3", "4149 RENFREW CT SW")
    assert got["1226-58149"] == ("14-0071-LL-054-7", "3665 THAXTON RD")
    assert got["1226-58151"] == ("14F-0109-LL-044-4", "5440 CASCADE RD")
    assert got["1226-58180"] == ("17-0186-0006-118-4", "1221 BRICKTON DR")
    assert got["1226-58195"] == ("22-5030-0463-109-6", "O HOPEWELL RD")
    from jarvis_books.computer.taxliens import fulton_parcel
    assert fulton_parcel(got["1226-58041"][0]) == "13 0133  LL2760"
