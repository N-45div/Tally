import json
import subprocess
import sys

from PIL import Image

from tally.airlock.inspect import MAX_PAGES, inspect
from tests.invoices import HIDDEN, make
from reportlab.pdfgen import canvas


def test_a_clean_invoice_clears_and_leaves_only_its_page_images_and_a_report(tmp_path):
    out = tmp_path / "out"
    r = inspect(make(tmp_path / "clean.pdf"), out)
    assert r["verdict"] == "clear" and r["reasons"] == []
    assert sorted(p.name for p in out.iterdir()) == ["page-1.png", "report.json"]
    # The page image is what a person would see: an A4 page at 150 dpi (1240.2 x 1754.0 px, rounded either way).
    w, h = Image.open(out / "page-1.png").size
    assert 1240 <= w <= 1241 and 1754 <= h <= 1755
    # The report never carries the text layer.
    assert "Northwind" not in (out / "report.json").read_text()


def test_an_invoice_hiding_an_instruction_is_held_and_says_why(tmp_path):
    draw, _ = HIDDEN["white_on_white"]
    r = inspect(make(tmp_path / "hidden.pdf", draw), tmp_path / "out")
    assert r["verdict"] == "hold"
    assert any("hidden text" in reason for reason in r["reasons"])
    # The hidden instruction is quoted for the person reviewing it, in the report only.
    assert "IBAN DE89" in r["hidden"][0]["text"]


def test_a_file_that_is_not_a_pdf_is_held_without_being_parsed(tmp_path):
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"MZ\x90\x00 not a pdf at all")
    r = inspect(f, tmp_path / "out")
    assert r["verdict"] == "hold" and r["reasons"] == ["not a PDF"] and r["images"] == []


def test_a_broken_pdf_is_held_as_unreadable(tmp_path):
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 50)
    r = inspect(f, tmp_path / "out")
    assert r["verdict"] == "hold" and any(x.startswith("unreadable") for x in r["reasons"])


def test_only_the_first_pages_are_inspected_and_the_rest_is_reported(tmp_path):
    f = tmp_path / "long.pdf"
    c = canvas.Canvas(str(f))
    for i in range(MAX_PAGES + 3):
        c.drawString(72, 720, f"page {i + 1}")
        c.showPage()
    c.save()
    r = inspect(f, tmp_path / "out")
    assert len(r["images"]) == MAX_PAGES and r["verdict"] == "hold"


def test_the_command_prints_a_one_line_verdict(tmp_path):
    pdf = make(tmp_path / "clean.pdf")
    done = subprocess.run([sys.executable, "-m", "tally.airlock.inspect", str(pdf), str(tmp_path / "out")], capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == {"verdict": "clear", "reasons": [], "images": ["page-1.png"]}


def test_a_giant_page_is_held_without_being_drawn(tmp_path):
    import time
    f = tmp_path / "giant.pdf"
    c = canvas.Canvas(str(f), pagesize=(14400, 14400))  # 200 x 200 inches
    c.drawString(72, 72, "INVOICE")
    c.showPage()
    c.save()
    started = time.monotonic()
    r = inspect(f, tmp_path / "out")
    assert r["verdict"] == "hold" and r["reasons"] == ["page 1 is too large to render"] and r["images"] == []
    assert time.monotonic() - started < 3, "the page must be refused before anything draws it"
