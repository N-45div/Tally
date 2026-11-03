"""The airlock's one entry point: inspect one untrusted PDF and write what may leave the sandbox.

    python -m tally.airlock.inspect <invoice.pdf> <out_dir>

It writes <out_dir>/report.json and one PNG per page (page-1.png, ...). Those files are all that leaves the sandbox:
the images are what a person would see, and the report says what in the file was hidden or active. The text layer
itself never leaves, so nothing hidden in it can reach a model.

Limits are checked before the file is parsed: at most MAX_BYTES and MAX_PAGES. A file that cannot be parsed is not an
error to hide; it is reported as unreadable and the invoice is held. The sandbox around this process adds its own
limits (no network, no secrets, memory, time, processes); this module does not rely on them, and they do not rely
on it.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pypdfium2 as pdfium

from tally.airlock.active import Finding, find_active_content
from tally.airlock.hidden import find_hidden_text

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 10
DPI = 150
# A page bigger than this many pixels at DPI is refused rather than rendered: a 200-inch page is not an invoice.
MAX_PIXELS = 40_000_000


def inspect(pdf_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = pdf_path.read_bytes() if pdf_path.stat().st_size <= MAX_BYTES else None
    report: dict = {"file": pdf_path.name, "bytes": pdf_path.stat().st_size, "pages": 0, "images": [], "active": [], "hidden": [], "problems": []}
    if data is None:
        report["problems"].append(f"larger than {MAX_BYTES // (1024 * 1024)} MB")
        return _finish(report, out_dir)
    report["sha256"] = hashlib.sha256(data).hexdigest()
    if not data.lstrip()[:5].startswith(b"%PDF-"):
        report["problems"].append("not a PDF")
        return _finish(report, out_dir)
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:  # a malformed or hostile file: say so, and hold it
        report["problems"].append(f"unreadable: {type(e).__name__}")
        return _finish(report, out_dir)
    report["pages"] = len(pdf)
    if len(pdf) > MAX_PAGES:
        report["problems"].append(f"{len(pdf)} pages; only the first {MAX_PAGES} were inspected")
    try:
        report["active"] = [asdict(f) for f in find_active_content(str(pdf_path), max_pages=MAX_PAGES)]
    except Exception as e:
        report["active"] = [asdict(Finding("hold", f"structure could not be read: {type(e).__name__}", "document"))]
    report["hidden"] = [asdict(r) for r in find_hidden_text(pdf, max_pages=MAX_PAGES)]
    for i in range(min(len(pdf), MAX_PAGES)):
        page = pdf[i]
        w, h = page.get_size()
        if (w * DPI / 72) * (h * DPI / 72) > MAX_PIXELS:
            report["problems"].append(f"page {i + 1} is too large to render")
            continue
        name = f"page-{i + 1}.png"
        page.render(scale=DPI / 72).to_pil().save(out_dir / name)
        report["images"].append(name)
        page.close()
    pdf.close()
    return _finish(report, out_dir)


def _finish(report: dict, out_dir: Path) -> dict:
    reasons = list(report["problems"])
    reasons += [f"hidden text ({r['reason']}) on page {r['page']}" for r in report["hidden"]]
    reasons += [f"{a['what']}, {a['where']}" for a in report["active"] if a["severity"] == "hold"]
    report["verdict"] = "hold" if reasons else "clear"
    report["reasons"] = reasons
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -m tally.airlock.inspect <invoice.pdf> <out_dir>")
    r = inspect(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps({"verdict": r["verdict"], "reasons": r["reasons"], "images": r["images"]}))
