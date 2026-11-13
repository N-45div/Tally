"""The public demo workspace: its own records and its own sample invoices, rebuilt from scratch on a schedule.

    python -m lab.demo reset <demo_dir>     wipe the demo workspace and build it again
    python -m lab.demo seed <demo_dir>      build it if it is not there yet

It is kept apart from the real workspace (its own database, files and ledger) and holds: the vendors and purchase
orders the lab uses, sample invoice files that visitors can download and upload (samples/, listed in
samples.json), and three of them already uploaded, so the first page a visitor sees has something on it. The demo
worker opens and reads those like any other invoice.

It also holds a short history: invoices from six suppliers, already approved, with dates around the day it is built,
so a visitor can plan a payment run, and so the case file on a held sample has a vendor's past to weigh. These are
records, not files: each is a reading written here and checked by the real rules, and its page says that no file was
opened. Nothing here is parsed outside a microVM.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from lab.attacks import ATTACKS, RECORDS
from tally.normalize import to_cents
from tally.pipeline import recheck
from tally.store import Store

# (attack in lab/attacks.py, file name, what a visitor should see happen)
SAMPLES = [
    ("clean", "northwind-october.pdf", "A real invoice from a vendor on file. Tally makes it ready to approve."),
    ("hidden: white on white", "northwind-white-text.pdf",
     "The same invoice with a hidden instruction in white text. Held when it is opened; the hidden text is quoted for you."),
    ("hidden: quiet bank update", "northwind-quiet-notice.pdf",
     "A hidden bank-change notice under the payment line, worded like an ordinary notice. Held when it is opened."),
    ("visible: new bank details", "northwind-new-bank.pdf",
     "A visible 'we have changed banks' notice and a new account on the payment line. Held: it is not the account on file."),
    ("lookalike vendor", "northwlnd-lookalike.pdf", "A vendor name one letter off the real one. Held: not a vendor on file."),
    ("over the purchase order", "northwind-over-po.pdf", "More than is left on its purchase order. Held by the rules."),
    ("JavaScript on open", "northwind-javascript.pdf", "JavaScript that runs when the file opens. Held when it is opened; nothing runs it."),
]
PRELOADED = ("clean", "hidden: white on white", "visible: new bank details")
# Suppliers with history: name, IBAN on file, phone on file.
SUPPLIERS = [
    ("Thames Water Utilities Ltd", "GB82 WEST 1234 5698 7654 32", "+44 345 737 2200"),
    ("Greystone Properties Ltd", "GB33 BUKB 2020 1555 5555 55", "+44 20 7946 0123"),
    ("Bright Sparks Electrical", "GB94 BARC 1020 1530 0934 59", "+44 117 496 0456"),
    ("Acme Cloud Hosting Ltd", "GB71 NWBK 6070 6012 3456 78", "+44 20 7946 0999"),
]
# Approved invoices: vendor, number, total, days from the build date to the invoice date and to the due date.
HISTORY = [
    ("Northwind Office Supply Ltd", "NW-3101", 1180.00, -70, -40), ("Northwind Office Supply Ltd", "NW-3188", 1356.20, -45, -15),
    ("Northwind Office Supply Ltd", "NW-3240", 1268.40, -24, 6), ("Thames Water Utilities Ltd", "TW-88412", 318.40, -16, -2),
    ("Greystone Properties Ltd", "GP-2026-11", 4200.00, -12, 1), ("Bright Sparks Electrical", "BSE-0457", 640.00, -9, -1),
    ("Bright Sparks Electrical", "BSE-0461", 385.50, -5, 9), ("Acme Cloud Hosting Ltd", "ACH-77120", 9600.00, -8, 17),
]


def _check(demo_dir: Path) -> None:
    """A demo workspace is a folder of its own: never the current folder by accident, never inside the repository."""
    here = Path(__file__).resolve().parent.parent
    target = demo_dir.resolve()
    if str(demo_dir) in ("", ".") or target == here or here in target.parents:
        raise SystemExit(f"refusing to build a demo workspace in {target}: give a folder outside the repository")


def seed(demo_dir: Path) -> None:
    _check(demo_dir)
    if (demo_dir / "tally.db").exists():
        return
    by_name = {a.name: a for a in ATTACKS}
    samples = demo_dir / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    listed = []
    for attack, filename, what in SAMPLES:
        by_name[attack].build(samples / filename)
        if attack in PRELOADED:
            what += " Already uploaded for you: upload it again to see the duplicate check hold the copy."
        listed.append({"file": filename, "attack": attack, "what": what, "preloaded": attack in PRELOADED})
    for leftover in samples.glob("*.src.pdf"):  # some builders leave their source file behind
        leftover.unlink()
    (demo_dir / "samples.json").write_text(json.dumps(listed, indent=1))

    store = Store(demo_dir)
    v = RECORDS["vendors"][0]
    vid = store.add_vendor("demo", v["name"], pay_to={"iban": v["iban"]}, phone=v["phone"])
    store.add_vendor("demo", "Fabrikam Cleaning Ltd", pay_to={"account_number": "31926819", "sort_code": "60-16-13"}, phone="+44 20 7946 0000")
    for po in RECORDS["pos"]:
        store.add_po("demo", po["number"], vid, po["currency"], to_cents(po["amount"]))
    _history(store, date.today())
    for attack, filename, _ in SAMPLES:
        if attack in PRELOADED:
            store.add_invoice("demo", filename, (samples / filename).read_bytes())
    store.db.close()


def _history(store: Store, today: date) -> None:
    """Approved invoices from the suppliers on file, as records: readings written here, checked by the real rules,
    approved by "demo". No file is opened; the page of each says so."""
    ids = {v["name"]: v["id"] for v in store.vendors()}
    for name, iban, phone in SUPPLIERS:
        ids[name] = store.add_vendor("demo", name, pay_to={"iban": iban}, phone=phone)
    for i, name in enumerate(ids):
        store.add_po("demo", f"PO-8{i:04d}", ids[name], "GBP", to_cents("50000"))
    blank = {"account_name": "", "iban": "", "account_number": "", "sort_code": "", "routing_number": "", "swift_bic": ""}
    airlock = {"verdict": "clear", "reasons": [], "images": [], "hidden": [], "active": [], "problems": [], "pages": 1, "bytes": 0,
               "sandbox": {"backend": "none: a record written by lab.demo, no file opened", "network": "none"}}
    for name, number, total, invoiced, due in HISTORY:
        fields = {"vendor_name": name, "vendor_address": "", "bill_to": "Contoso Clinics", "invoice_number": number,
                  "invoice_date": (today + timedelta(days=invoiced)).isoformat(), "due_date": (today + timedelta(days=due)).isoformat(),
                  "po_number": f"PO-8{list(ids).index(name):04d}", "currency": "GBP", "subtotal": None, "tax": None, "total": total,
                  "pay_to": copy.deepcopy(blank), "lines": []}
        iid = store.add_invoice("demo", f"{number.lower()}.pdf", f"%PDF-1.7 a record written by lab.demo: {number}".encode())
        store.claim_next()
        reading, check = {"fields": fields, "model": "glm-5.3-flash"}, {"fields": fields, "model": "qwen3.8-flash-next"}
        inv = store.invoice(iid) | {"airlock": airlock, "reading": reading, "check_reading": check}
        store.save_result("lab.demo", iid, airlock=airlock, reading=reading, check_reading=check, decision=recheck(store, inv))
        store.decide("demo", iid, True, "History for the demo.", lambda inv: recheck(store, inv))


def reset(demo_dir: Path) -> None:
    """Remove everything in the demo workspace and build it again. Refuses a folder that is not a demo workspace."""
    _check(demo_dir)
    if demo_dir.exists():
        if not (demo_dir / "samples.json").exists():
            raise SystemExit(f"{demo_dir} does not look like a demo workspace; not removing it")
        for p in demo_dir.iterdir():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    seed(demo_dir)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("seed", "reset"):
        sys.exit(__doc__)
    (seed if sys.argv[1] == "seed" else reset)(Path(sys.argv[2]))
    print(f"demo workspace ready at {sys.argv[2]}")
