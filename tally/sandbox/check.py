"""Check what came out of a sandbox before anything on this side uses it.

A sandbox can be broken into by the file it opened. So its output is treated like the file itself: untrusted. It
may contain only report.json and page-N.png files; each is size-checked, the PNGs are checked by their header alone
(nothing here decodes them), and the report must have exactly the expected shape. The verdict is worked out again
here from the report's own findings, so a report cannot say "clear" next to a hidden-text finding.

What this cannot catch: a sandbox that was broken into and then lies consistently (a clean report and clean-looking
images for a bad file). The fixed rules that run later (known vendor, known bank account, matching purchase order)
do not depend on the sandbox, and nothing is paid without a person's approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path

from tally.airlock.inspect import MAX_PAGES, MAX_PIXELS

MAX_REPORT_BYTES = 1_000_000
MAX_PNG_BYTES = 25 * 1024 * 1024
MAX_TEXT = 4000
MAX_ITEMS = 1000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PAGE_NAME = re.compile(r"page-([1-9][0-9]?)\.png")


class BadOutput(ValueError):
    """The sandbox's output is not what the airlock writes. The invoice is held and its images are not used."""


def _png_size(path: Path) -> tuple[int, int]:
    head = path.open("rb").read(24)
    if len(head) < 24 or head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise BadOutput(f"{path.name} is not a PNG")
    return struct.unpack(">II", head[16:24])


def _text(value, what: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise BadOutput(f"{what} is not a short string")
    return value


def _int(value, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BadOutput(f"{what} is not a count")
    return value


def _items(value, what: str) -> list:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise BadOutput(f"{what} is not a list")
    return value


def _record(value, keys: set[str], what: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise BadOutput(f"{what} does not have the fields {sorted(keys)}")
    return value


def verdict(report: dict) -> tuple[str, list[str]]:
    """The airlock's rule, worked out here again: any problem, hidden text or active content holds the invoice."""
    reasons = list(report["problems"])
    reasons += [f"hidden text ({r['reason']}) on page {r['page']}" for r in report["hidden"]]
    reasons += [f"{a['what']}, {a['where']}" for a in report["active"] if a["severity"] == "hold"]
    return ("hold" if reasons else "clear"), reasons


def check_output(out_dir: Path, pdf_bytes: bytes) -> dict:
    """Return the sandbox's report once it has passed every check, or raise BadOutput saying which one failed."""
    entries = list(out_dir.iterdir())
    if len(entries) > MAX_PAGES + 1:
        raise BadOutput(f"{len(entries)} files came out; at most {MAX_PAGES + 1} are expected")
    pngs: dict[str, tuple[int, int]] = {}
    report_path = None
    for p in entries:
        if p.is_symlink() or not p.is_file():
            raise BadOutput(f"{p.name} is not a plain file")
        if p.name == "report.json":
            report_path = p
            continue
        m = PAGE_NAME.fullmatch(p.name)
        if not m or int(m.group(1)) > MAX_PAGES:
            raise BadOutput(f"unexpected file {p.name!r}")
        if p.stat().st_size > MAX_PNG_BYTES:
            raise BadOutput(f"{p.name} is larger than {MAX_PNG_BYTES // (1024 * 1024)} MB")
        w, h = _png_size(p)
        if not (0 < w and 0 < h and w * h <= MAX_PIXELS):
            raise BadOutput(f"{p.name} is {w}x{h} pixels")
        pngs[p.name] = (w, h)
    if report_path is None:
        raise BadOutput("no report.json")
    if report_path.stat().st_size > MAX_REPORT_BYTES:
        raise BadOutput("report.json is too large")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BadOutput("report.json is not JSON") from None

    keys = {"file", "bytes", "pages", "images", "active", "hidden", "problems", "verdict", "reasons"}
    if not isinstance(report, dict) or not keys <= set(report) <= keys | {"sha256"}:
        raise BadOutput("report.json does not have the airlock's fields")
    _text(report["file"], "file")
    if _int(report["bytes"], "bytes") != len(pdf_bytes):
        raise BadOutput("the report is about a file of a different size")
    if "sha256" in report and report["sha256"] != hashlib.sha256(pdf_bytes).hexdigest():
        raise BadOutput("the report is about a different file")
    _int(report["pages"], "pages")
    images = [_text(x, "image name") for x in _items(report["images"], "images")]
    if sorted(images) != sorted(pngs) or len(set(images)) != len(images):
        raise BadOutput("the report's images are not the images that came out")
    for x in _items(report["problems"], "problems") + _items(report["reasons"], "reasons"):
        _text(x, "reason")
    for a in _items(report["active"], "active"):
        _record(a, {"severity", "what", "where"}, "an active-content finding")
        if a["severity"] not in ("hold", "info"):
            raise BadOutput("an active-content finding has an unknown severity")
        _text(a["what"], "what"), _text(a["where"], "where")
    for r in _items(report["hidden"], "hidden"):
        _record(r, {"page", "text", "reason", "bbox"}, "a hidden-text finding")
        _int(r["page"], "page"), _text(r["text"], "hidden text"), _text(r["reason"], "reason")
        if not (isinstance(r["bbox"], list) and len(r["bbox"]) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r["bbox"])):
            raise BadOutput("a hidden-text finding has a bad box")
    if report["verdict"] not in ("hold", "clear"):
        raise BadOutput("unknown verdict")

    expected, reasons = verdict(report)
    if report["verdict"] != expected or report["reasons"] != reasons:
        raise BadOutput("the report's verdict does not follow from its own findings")
    return report
