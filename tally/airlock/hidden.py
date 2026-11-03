"""Text a machine reads but a person cannot see.

Every character in a PDF's text layer is checked against what the page actually shows. A character is hidden when:

- its text render mode draws nothing (invisible, or clip only),
- it is filled fully transparent,
- its font is under 2 points, which nobody can read,
- it sits outside the page, or
- the rendered page shows no ink where it is: drawn in the same colour as what is behind it, or covered by a shape
  drawn after it.

The last check is the one that needs no assumption about colours: the character's box, with a small margin, is cut
out of the rendered page, and if the lightest and darkest pixels in it are nearly the same, nothing visible is there.

Hidden characters next to each other are reported together as one run, with the reason. Text that is part of an
image is not text-layer text and is not checked here: it is visible, so it is part of what a person sees.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as raw

MIN_FONT_PT = 2.0
# 0-255 luminance: inside a character's box, the lightest and darkest pixels must differ by at least this much.
MIN_CONTRAST = 24
# The page is rendered at this many pixels per point for the ink check.
SCALE = 2.0
# Around each character's box, so the cut-out holds background as well as glyph.
MARGIN_PT = 1.0
# A run shorter than this is noise (a stray mark), not an instruction.
MIN_RUN_CHARS = 3

INVISIBLE_MODES = {raw.FPDF_TEXTRENDERMODE_INVISIBLE, raw.FPDF_TEXTRENDERMODE_CLIP}


@dataclass
class HiddenRun:
    page: int  # 1-based
    text: str
    reason: str
    # PDF points, origin at the bottom left of the page
    bbox: tuple[float, float, float, float]


def _fill_alpha(textpage: pdfium.PdfTextPage, i: int) -> int:
    r, g, b, a = (ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint())
    ok = raw.FPDFText_GetFillColor(textpage.raw, i, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a))
    return a.value if ok else 255


def _render_mode(textpage: pdfium.PdfTextPage, i: int) -> int | None:
    obj = raw.FPDFText_GetTextObject(textpage.raw, i)
    return raw.FPDFTextObj_GetTextRenderMode(obj) if obj else None


def _no_ink(gray, page_height: float, box: tuple[float, float, float, float]) -> bool:
    left, bottom, right, top = box
    x0 = max(0, int((left - MARGIN_PT) * SCALE))
    x1 = min(gray.width, int((right + MARGIN_PT) * SCALE) + 1)
    y0 = max(0, int((page_height - top - MARGIN_PT) * SCALE))
    y1 = min(gray.height, int((page_height - bottom + MARGIN_PT) * SCALE) + 1)
    if x1 <= x0 or y1 <= y0:
        return True
    lo, hi = gray.crop((x0, y0, x1, y1)).getextrema()
    return hi - lo < MIN_CONTRAST


def _why_hidden(textpage, i: int, box, size: tuple[float, float], gray) -> str | None:
    width, height = size
    mode = _render_mode(textpage, i)
    if mode in INVISIBLE_MODES:
        return "invisible render mode"
    if _fill_alpha(textpage, i) == 0:
        return "transparent fill"
    if raw.FPDFText_GetFontSize(textpage.raw, i) < MIN_FONT_PT:
        return f"font under {MIN_FONT_PT:g}pt"
    left, bottom, right, top = box
    inside_w = max(0.0, min(right, width) - max(left, 0.0))
    inside_h = max(0.0, min(top, height) - max(bottom, 0.0))
    area = max(1e-6, (right - left) * (top - bottom))
    if inside_w * inside_h < 0.5 * area:
        return "off the page"
    if _no_ink(gray, height, box):
        return "no visible ink (same colour as what is behind it, or covered)"
    return None


def find_hidden_text(pdf: pdfium.PdfDocument, max_pages: int | None = None) -> list[HiddenRun]:
    runs: list[HiddenRun] = []
    for index in range(len(pdf) if max_pages is None else min(len(pdf), max_pages)):
        page = pdf[index]
        size = page.get_size()
        gray = page.render(scale=SCALE).to_pil().convert("L")
        textpage = page.get_textpage()
        current: dict | None = None

        def close():
            nonlocal current
            if current and sum(1 for c in current["text"] if not c.isspace()) >= MIN_RUN_CHARS:
                runs.append(HiddenRun(index + 1, current["text"].strip(), current["reason"], tuple(current["bbox"])))
            current = None

        for i in range(textpage.count_chars()):
            ch = chr(raw.FPDFText_GetUnicode(textpage.raw, i))
            if ch in ("\x00", "￾", "￿"):
                continue
            if ch.isspace():
                if current:
                    current["text"] += " " if not current["text"].endswith(" ") else ""
                continue
            box = textpage.get_charbox(i)
            reason = _why_hidden(textpage, i, box, size, gray)
            if reason is None:
                close()
                continue
            if current and current["reason"] == reason:
                current["text"] += ch
                b = current["bbox"]
                current["bbox"] = [min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]), max(b[3], box[3])]
            else:
                close()
                current = {"text": ch, "reason": reason, "bbox": list(box)}
        close()
        textpage.close()
        page.close()
    return runs
