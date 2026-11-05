"""The public demo workspace: its own records and its own sample invoices, rebuilt from scratch on a schedule.

    python -m lab.demo reset <demo_dir>     wipe the demo workspace and build it again
    python -m lab.demo seed <demo_dir>      build it if it is not there yet

It is kept apart from the real workspace (its own database, files and ledger) and holds: the vendors and purchase
orders the lab uses, sample invoice files that visitors can download and upload (samples/, listed in
samples.json), and three of them already uploaded, so the first page a visitor sees has something on it. The demo
worker opens and reads those like any other invoice.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from lab.attacks import ATTACKS, RECORDS
from tally.normalize import to_cents
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


def seed(demo_dir: Path) -> None:
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
    for attack, filename, _ in SAMPLES:
        if attack in PRELOADED:
            store.add_invoice("demo", filename, (samples / filename).read_bytes())
    store.db.close()


def reset(demo_dir: Path) -> None:
    """Remove everything in the demo workspace and build it again. Refuses a folder that is not a demo workspace."""
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
