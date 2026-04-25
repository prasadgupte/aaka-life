"""
tools/pdf_tool_test.py — Smoke test for pdf_tool.py

Run:  python3 tools/pdf_tool_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz
from tools.pdf_tool import compress, extract, split, merge, delete_pages
from tools.pdf_page_spec import parse_for_split, parse_for_delete


def _make_pdf(path: str, pages: int) -> None:
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 72), "Page " + str(i + 1), fontsize=12)
    doc.save(path)
    doc.close()


def test_compress():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "test.pdf")
        out = os.path.join(d, "out.pdf")
        _make_pdf(src, 4)
        r = compress(src, out)
        assert os.path.exists(r["output"]), "compress output missing"
        assert r["original_kb"] >= 1
        assert r["compressed_kb"] >= 1


def test_extract():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "test.pdf")
        _make_pdf(src, 3)
        r = extract(src, d)
        assert r["pages"] == 3
        assert os.path.exists(r["text_file"])
        text = Path(r["text_file"]).read_text(encoding="utf-8")
        assert "Page 1" in text
        assert "Page 3" in text


def test_split():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "test.pdf")
        _make_pdf(src, 8)
        r = split(src, "2s", d)
        assert r["count"] == 4, "expected 4 parts for 2s on 8 pages"
        r2 = split(src, "3-6", d)
        assert r2["count"] == 1
        r3 = split(src, "1,3,7", d)
        assert r3["count"] == 3


def test_merge():
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "a.pdf")
        b = os.path.join(d, "b.pdf")
        out = os.path.join(d, "merged.pdf")
        _make_pdf(a, 3)
        _make_pdf(b, 2)
        r = merge([a, b], out)
        assert r["pages"] == 5
        assert os.path.exists(r["output"])


def test_delete():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "test.pdf")
        _make_pdf(src, 8)
        r = delete_pages(src, "2-4", os.path.join(d, "out1.pdf"))
        assert r["removed"] == 3 and r["remaining"] == 5
        r2 = delete_pages(src, "2s", os.path.join(d, "out2.pdf"))
        assert r2["removed"] == 4 and r2["remaining"] == 4
        r3 = delete_pages(src, "1,3,5", os.path.join(d, "out3.pdf"))
        assert r3["removed"] == 3 and r3["remaining"] == 5


def test_page_spec():
    assert parse_for_split("1,5,9", 10) == [[0,1,2,3],[4,5,6,7],[8,9]]
    assert parse_for_split("3-5", 10) == [[2,3,4]]
    assert parse_for_split("2s", 8) == [[0,1],[2,3],[4,5],[6,7]]
    assert parse_for_delete("1,5,9", 10) == [0,4,8]
    assert parse_for_delete("3-5", 10) == [2,3,4]
    assert parse_for_delete("2s", 8) == [1,3,5,7]


if __name__ == "__main__":
    tests = [test_page_spec, test_compress, test_extract, test_split, test_merge, test_delete]
    failed = 0
    for t in tests:
        try:
            t()
            print("OK  " + t.__name__)
        except Exception as e:
            print("FAIL " + t.__name__ + ": " + str(e))
            failed += 1
    if failed:
        sys.exit(1)
    print("All " + str(len(tests)) + " tests passed")
