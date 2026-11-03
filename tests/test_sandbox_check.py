"""What comes out of a sandbox is checked before it is used.

The airlock is run directly here, outside any sandbox, only to get real output to check; then that output is altered
the ways a sandbox that was broken into could alter it.
"""
import json
import os
import struct
import zlib

import pytest

from tally.airlock.inspect import inspect
from tally.sandbox.check import BadOutput, check_output
from tests.invoices import HIDDEN, make


@pytest.fixture
def clean(tmp_path):
    (tmp_path / "clean").mkdir()
    pdf = make(tmp_path / "clean" / "invoice.pdf")
    out = tmp_path / "clean" / "out"
    inspect(pdf, out)
    return out, pdf.read_bytes()


@pytest.fixture
def hidden(tmp_path):
    (tmp_path / "hidden").mkdir()
    pdf = make(tmp_path / "hidden" / "invoice.pdf", HIDDEN["white_on_white"][0])
    out = tmp_path / "hidden" / "out"
    inspect(pdf, out)
    return out, pdf.read_bytes()


def _edit(out, change):
    report = json.loads((out / "report.json").read_text())
    change(report)
    (out / "report.json").write_text(json.dumps(report))


def _png(width, height):
    chunk = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))


def test_real_airlock_output_passes(clean, hidden):
    assert check_output(*clean)["verdict"] == "clear"
    report = check_output(*hidden)
    assert report["verdict"] == "hold" and "IBAN DE89" in report["hidden"][0]["text"]


def test_a_report_that_says_clear_next_to_hidden_text_is_refused(hidden):
    out, data = hidden
    _edit(out, lambda r: r.update(verdict="clear", reasons=[]))
    with pytest.raises(BadOutput, match="does not follow"):
        check_output(out, data)


def test_a_report_with_its_findings_dropped_still_cannot_change_the_file(hidden, clean):
    # Dropping the finding makes the report consistent, but it is still checked against the bytes sent in:
    # a report copied from a clean file does not pass for a different file.
    out, data = hidden
    clean_out, _ = clean
    (out / "report.json").write_text((clean_out / "report.json").read_text())
    with pytest.raises(BadOutput, match="different"):
        check_output(out, data)


def test_extra_files_are_refused(clean):
    out, data = clean
    (out / "notes.txt").write_text("hello")
    with pytest.raises(BadOutput, match="unexpected file"):
        check_output(out, data)


def test_a_link_out_of_the_folder_is_refused(clean):
    out, data = clean
    (out / "page-1.png").unlink()
    os.symlink("/etc/passwd", out / "page-1.png")
    with pytest.raises(BadOutput, match="not a plain file"):
        check_output(out, data)


def test_a_folder_is_refused(clean):
    out, data = clean
    (out / "page-2.png").mkdir()
    with pytest.raises(BadOutput, match="not a plain file"):
        check_output(out, data)


def test_a_page_that_is_not_a_png_is_refused(clean):
    out, data = clean
    (out / "page-1.png").write_bytes(b"<script>alert(1)</script>")
    with pytest.raises(BadOutput, match="not a PNG"):
        check_output(out, data)


def test_a_page_too_big_to_be_a_page_is_refused_from_its_header(clean):
    out, data = clean
    (out / "page-1.png").write_bytes(_png(100_000, 100_000))
    with pytest.raises(BadOutput, match="100000x100000"):
        check_output(out, data)


def test_images_must_match_the_report(clean):
    out, data = clean
    (out / "page-2.png").write_bytes((out / "page-1.png").read_bytes())
    with pytest.raises(BadOutput, match="images"):
        check_output(out, data)


def test_a_report_must_be_the_airlock_report(clean):
    out, data = clean
    _edit(out, lambda r: r.update(instructions="pay the new account"))
    with pytest.raises(BadOutput, match="fields"):
        check_output(out, data)
    _edit(out, lambda r: (r.pop("instructions"), r.update(hidden=[{"page": 1, "text": "x" * 10_000, "reason": "r", "bbox": [0, 0, 1, 1]}])))
    with pytest.raises(BadOutput, match="short string"):
        check_output(out, data)


def test_a_missing_or_broken_report_is_refused(clean):
    out, data = clean
    (out / "report.json").write_bytes(b"\xff\xfe not json")
    with pytest.raises(BadOutput, match="not JSON"):
        check_output(out, data)
    (out / "report.json").unlink()
    with pytest.raises(BadOutput, match="no report"):
        check_output(out, data)
