"""Reading invoices Tally has never seen: the FATURA dataset, through the whole pipeline, scored against its labels.

FATURA (Limam, Dhiaf and Kessentini, 2023; https://doi.org/10.5281/zenodo.10371464; CC BY 4.0) is 10,000 invoice
images in 50 layouts, 200 of each, with the printed text of every field annotated. Nothing in Tally was built or
tuned on it. This takes `per_layout` invoices from each layout (half on white, half on coloured backgrounds), turns
each image into a one-page PDF with no text layer, as a scanned invoice is, and runs it through Tally like any other
upload: a microVM per file, two readings by two model families, the fixed rules. Then it compares what each reading
copied with the dataset's annotation, field by field.

    python -m lab.fatura <out_dir> [per_layout] [workers]

Only the images and annotations it needs are fetched, by HTTP range requests on the dataset's zip, and each is
checked against the zip's own CRC. It writes fatura.json and fatura.txt to out_dir.

What it measures: how often each model copies each field correctly, how often the two readings agree on the fields
that decide a payment (an invoice whose readings differ is held), and how often Tally's own sums check passes. What
it does not: the vendor, bank and purchase order rules, which need records the dataset does not have.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from PIL import Image

from tally import ledger, rules
from tally.normalize import key, to_cents, vendor_key
from tally.pipeline import work_in_parallel
from tally.store import Store

SOURCE = "https://zenodo.org/api/records/10371464/files/FATURA2.zip/content"
ROOT = "invoices_dataset_final/"
LAYOUTS = range(1, 51)
FIELDS = ("vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "currency", "subtotal", "tax", "total")


class RemoteZip(io.RawIOBase):
    """A file over HTTP, read with range requests, so zipfile can read single members of a large remote zip."""

    def __init__(self, url: str):
        self.url, self.pos = url, 0
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            self.size = int(r.headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def readinto(self, buffer):
        if self.pos >= self.size or not len(buffer):
            return 0
        end = min(self.size, self.pos + len(buffer)) - 1
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        buffer[:len(data)] = data
        self.pos += len(data)
        return len(data)


def pick(per_layout: int) -> list[tuple[int, int, str]]:
    """(layout, instance, folder): instances spread over the 200 of each layout, the first half on white backgrounds."""
    step = 200 // per_layout
    return [(t, 1 + i * step, "images" if i < per_layout / 2 else "colored_images") for t in LAYOUTS for i in range(per_layout)]


def fetch(sample, cache: Path) -> list[dict]:
    cache.mkdir(parents=True, exist_ok=True)
    z = None
    out = []
    for t, i, folder in sample:
        name = f"Template{t}_Instance{i}"
        jpg, ann = cache / f"{folder}-{name}.jpg", cache / f"{name}.json"
        if not (jpg.exists() and ann.exists()):
            z = z or zipfile.ZipFile(io.BufferedReader(RemoteZip(SOURCE), buffer_size=1 << 20))
            jpg.write_bytes(z.read(f"{ROOT}{folder}/{name}.jpg"))  # zipfile checks each member's CRC
            ann.write_bytes(z.read(f"{ROOT}Annotations/Original_Format/{name}.json"))
        out.append({"name": name, "layout": t, "background": "white" if folder == "images" else "coloured", "jpg": jpg,
                    "annotation": json.loads(ann.read_text())})
    return out


# The annotation holds each field's printed text, label included: "INVOICE # 9Y2M7d-647", "TOTAL : 796.34 EUR".
AMOUNT = re.compile(r"(?<![\d.])(\d[\d,]*\.\d{2})(?!\d)(?!\s*%)")
NUMBER_LABEL = re.compile(r"^\s*invoice\s*(?:#|id\b|number\b|no\b\.?)?\s*[:#]?\s*", re.I)
CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}


def _text(ann: dict, field: str) -> str | None:
    v = ann.get(field)
    return v["text"].strip() if isinstance(v, dict) and v.get("text") else None


def _after_colon(text: str) -> str:
    return text.rsplit(":", 1)[-1].strip() if ":" in text else text.strip()


def _amount(text: str | None) -> int | None:
    found = AMOUNT.findall(text or "")
    return to_cents(found[-1]) if found else None


def _currency(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"\b(USD|EUR|GBP|INR)\b|([$€£₹])", text)
    return (m.group(1) or CURRENCIES[m.group(2)]) if m else None


def parse_date(text) -> date | None:
    text = str(text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d/%m/%Y", "%b %d, %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def truth(ann: dict) -> dict:
    """The value of each field the layout prints, from the annotation; a field it does not print is left out."""
    t: dict = {}
    if (s := _text(ann, "SELLER_NAME")):
        t["vendor_name"] = s
    if (s := _text(ann, "NUMBER")):
        t["invoice_number"] = NUMBER_LABEL.sub("", s)
    for field, name in (("DATE", "invoice_date"), ("DUE_DATE", "due_date")):
        if (s := _text(ann, field)) and parse_date(_after_colon(s)):
            t[name] = parse_date(_after_colon(s)).isoformat()
    if (s := _text(ann, "PO_NUMBER")):
        t["po_number"] = _after_colon(s)
    for field, name in (("SUB_TOTAL", "subtotal"), ("TAX", "tax"), ("TOTAL", "total")):
        if (cents := _amount(_text(ann, field))) is not None:
            t[name] = cents
    if (c := _currency(_text(ann, "TOTAL"))):
        t["currency"] = c
    return t


def correct(field: str, read, expected) -> bool:
    if field == "vendor_name":
        return bool(vendor_key(read)) and vendor_key(read) == vendor_key(expected)
    if field in ("invoice_number", "po_number"):
        return bool(key(read)) and key(read) == key(expected)
    if field in ("invoice_date", "due_date"):
        d = parse_date(read)
        return d is not None and d.isoformat() == expected
    if field == "currency":
        r = str(read or "").strip()
        return key(CURRENCIES.get(r, r)) == expected
    return to_cents(read) == expected


def _pdf(jpg: Path, pdf: Path) -> Path:
    Image.open(jpg).convert("RGB").save(pdf, "PDF", resolution=72.0)  # one point per pixel: the page is the image
    return pdf


def run(out_dir: Path, per_layout: int = 4, workers: int = 4) -> dict:
    cache = Path.home() / ".cache" / "tally-fatura"
    items = fetch(pick(per_layout), cache)
    work = Path(tempfile.mkdtemp(prefix="tally-fatura-"))
    store = Store(work / "data")
    for it in items:
        it["id"] = store.add_invoice("fatura", it["name"] + ".pdf", _pdf(it["jpg"], work / (it["name"] + ".pdf")).read_bytes())
    started = time.monotonic()
    work_in_parallel(store.dir, workers=workers, once=True)
    seconds = time.monotonic() - started

    rows = []
    for it in items:
        inv = store.invoice(it["id"])
        readings = {"reader": inv["reading"], "checker": inv["check_reading"]}
        t = truth(it["annotation"])
        fields = {}
        for f in FIELDS:
            if f in t:
                fields[f] = {"truth": t[f], **{who: (r or {}).get("fields", {}).get(f) for who, r in readings.items()},
                             **{f"{who}_correct": bool(r) and correct(f, r["fields"].get(f), t[f]) for who, r in readings.items()}}
        # Tally's own checks on what was read, without records: whether the readings agree, whether the sums add up.
        decision = rules.decide(inv["airlock"] or {}, (inv["reading"] or {}).get("fields"), (inv["check_reading"] or {}).get("fields"),
                                reader_model=(inv["reading"] or {}).get("model"), checker_model=(inv["check_reading"] or {}).get("model"),
                                vendors=[], pos=[], others=lambda v, n: {"same_file": [], "same_number": []})
        checks = {c["code"]: c["ok"] for c in decision["checks"]}
        opened = next((e["detail"] for e in ledger.entries(store.db, it["id"]) if e["kind"] == "opened"), {})
        rows.append({"name": it["name"], "layout": it["layout"], "background": it["background"], "status": inv["status"],
                     "airlock": (inv["airlock"] or {}).get("verdict"), "fields": fields,
                     "readings_agree": checks.get("second_reading"), "sums_ok": checks.get("sums"),
                     "models": [(r or {}).get("model") for r in readings.values()],
                     "sandbox_seconds": opened.get("seconds"),
                     "read_seconds": sum((r or {}).get("seconds") or 0 for r in readings.values()),
                     "usd": sum((r or {}).get("usd") or 0 for r in readings.values())})
    res = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": SOURCE, "per_layout": per_layout,
           "workers": workers, "seconds": round(seconds, 1), "rows": rows, "summary": summarise(rows, seconds)}
    shutil.rmtree(work, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fatura.json").write_text(json.dumps(res, indent=1, default=str))
    (out_dir / "fatura.txt").write_text(report(res))
    return res


def _share(n: int, of: int) -> str:
    return f"{n} of {of} ({100 * n / of:.0f}%)" if of else "-"


def summarise(rows: list[dict], seconds: float) -> dict:
    read = [r for r in rows if r["fields"] and any(v.get("reader") is not None for v in r["fields"].values())]
    per_field = {}
    for f in FIELDS:
        cells = [r["fields"][f] for r in rows if f in r["fields"]]
        per_field[f] = {"printed_on": len(cells), "reader": sum(c["reader_correct"] for c in cells),
                        "checker": sum(c["checker_correct"] for c in cells),
                        "both": sum(c["reader_correct"] and c["checker_correct"] for c in cells)}
    sandbox = sorted(r["sandbox_seconds"] for r in rows if r["sandbox_seconds"] is not None)
    return {"invoices": len(rows), "read": len(read), "airlock_clear": sum(r["airlock"] == "clear" for r in rows),
            "readings_agree": sum(bool(r["readings_agree"]) for r in rows), "sums_ok": sum(bool(r["sums_ok"]) for r in rows),
            "per_field": per_field, "usd": round(sum(r["usd"] for r in rows), 4),
            "usd_per_invoice": round(sum(r["usd"] for r in rows) / len(rows), 5) if rows else None,
            "sandbox_seconds_median": sandbox[len(sandbox) // 2] if sandbox else None,
            "invoices_per_minute": round(60 * len(rows) / seconds, 1) if seconds else None}


def report(res: dict) -> str:
    s = res["summary"]
    lines = [f"FATURA benchmark, {res['at']}: {s['invoices']} invoices, {res['per_layout']} from each of 50 layouts Tally has never seen",
             f"(half on white, half on coloured backgrounds), each as a one-page PDF with no text layer. Source: {res['source']} (CC BY 4.0).", "",
             f"Opened in a microVM and read twice: {_share(s['read'], s['invoices'])}; file clear when opened: {_share(s['airlock_clear'], s['invoices'])}.",
             f"The two readings agreed on every field that decides a payment: {_share(s['readings_agree'], s['invoices'])}; the rest would be held.",
             f"Tally's sums check passed: {_share(s['sums_ok'], s['invoices'])}.", "",
             f"{'field':16} {'printed on':>10}  {'reader correct':>18}  {'checker correct':>18}  {'both correct':>18}"]
    for f, c in s["per_field"].items():
        lines.append(f"{f:16} {c['printed_on']:>10}  {_share(c['reader'], c['printed_on']):>18}  {_share(c['checker'], c['printed_on']):>18}  "
                     f"{_share(c['both'], c['printed_on']):>18}")
    lines += ["", f"{res['workers']} workers, {res['seconds']} s in all: {s['invoices_per_minute']} invoices a minute; median {s['sandbox_seconds_median']} s "
                  f"in the microVM; ${s['usd']:.4f} of model calls, ${s['usd_per_invoice']:.5f} per invoice."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 4:
        sys.exit(__doc__)
    r = run(Path(sys.argv[1]), *(int(a) for a in sys.argv[2:]))
    print(report(r))
