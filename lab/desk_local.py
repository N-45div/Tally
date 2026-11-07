"""Tally on a laptop, for trying the desk device on a local network without the host.

    python -m lab.desk_local <data_dir> seed                the lab's vendor and purchase orders, a person, a device
    python -m lab.desk_local <data_dir> serve [port]        the web app on every address (8765 unless given) and the
                                                            desk worker beside it; needs VULTR_INFERENCE_API_KEY
    python -m lab.desk_local <data_dir> add ready|bank|lookalike
                                                            an invoice as the worker would leave it: ready to approve,
                                                            asking for a new bank account, or from a lookalike vendor

Then point the board at it: TALLY_DEVICE_TOKEN=$(cat <data_dir>/device-token.txt) py device/provision.py --server
http://<this laptop's address>:<port>. Sign in at the same address as "lab" with the password in <data_dir>/person.txt.

No file is opened here. An invoice is a reading written by this script and saved as the worker would save it, so
nothing untrusted is parsed outside a microVM. The workspace is a folder of its own, never inside the repository.
"""
from __future__ import annotations

import copy
import secrets
import sys
import threading
from pathlib import Path

from lab.attacks import RECORDS
from tally import desk
from tally.normalize import to_cents
from tally.pipeline import recheck
from tally.store import Store
from tally.web import create_app

READING = {
    "vendor_name": "Northwind Office Supply Ltd", "vendor_address": "12 Harbour Road, Bristol BS1 4XX", "bill_to": "Contoso Clinics",
    "invoice_number": "", "invoice_date": "2026-10-28", "due_date": "2026-11-27", "po_number": "PO-55120",
    "currency": "GBP", "subtotal": 1244.0, "tax": 248.8, "total": 1492.8,
    "pay_to": {"account_name": "Northwind Office Supply Ltd", "iban": RECORDS["vendors"][0]["iban"], "account_number": "",
               "sort_code": "", "routing_number": "", "swift_bic": ""},
    "lines": [{"description": "A4 paper, 80gsm box", "quantity": 40, "unit_price": 21.5, "amount": 860.0},
              {"description": "Toner cartridge TN-2420", "quantity": 6, "unit_price": 64.0, "amount": 384.0}],
}
KINDS = {
    "ready": {},
    "bank": {"pay_to": {"iban": "DE89 3704 0044 0532 0130 00"}},
    "lookalike": {"vendor_name": "Northwlnd Office Supply Ltd"},
}
ACTOR = "lab"


def _check(data_dir: Path) -> None:
    here = Path(__file__).resolve().parent.parent
    target = data_dir.resolve()
    if str(data_dir) in ("", ".") or target == here or here in target.parents:
        raise SystemExit(f"refusing to use {target}: give a folder outside the repository")


def seed(data_dir: Path) -> None:
    s = Store(data_dir)
    if s.vendors():
        raise SystemExit(f"{data_dir} already has records")
    vendors = {}
    for v in RECORDS["vendors"]:
        vendors[v["name"]] = s.add_vendor(ACTOR, v["name"], pay_to={"iban": v["iban"]}, phone=v["phone"])
    for p in RECORDS["pos"]:
        s.add_po(ACTOR, p["number"], vendors[p["vendor"]], p["currency"], to_cents(p["amount"]))
    password = secrets.token_urlsafe(12)
    s.add_user(ACTOR, "lab", password)
    (data_dir / "person.txt").write_text(f"lab\n{password}\n")
    _, token = s.add_device(ACTOR, "Front desk")
    (data_dir / "device-token.txt").write_text(token)
    print(f"seeded {data_dir}: sign-in in person.txt, the device's token in device-token.txt")


def add(data_dir: Path, kind: str) -> int:
    s = Store(data_dir)
    n = len(s.invoices()) + 1
    fields = copy.deepcopy(READING) | {"invoice_number": f"INV-2026-{1000 + n}"}
    for k, v in KINDS[kind].items():
        fields[k] = fields[k] | v if isinstance(v, dict) else v
    iid = s.add_invoice(ACTOR, f"{kind}-{n}.pdf", f"%PDF-1.7 lab.desk_local {kind} {n}".encode())
    s.claim_next()
    airlock = {"verdict": "clear", "reasons": [], "images": [], "hidden": [], "active": [], "problems": [], "pages": 1, "bytes": 0,
               "sandbox": {"backend": "none: written by lab.desk_local, no file opened", "network": "none"}}
    reading = {"fields": fields, "model": "glm-5.3-flash", "seconds": 0, "usd": 0}
    check = {"fields": fields, "model": "qwen3.8-flash-next", "seconds": 0, "usd": 0}
    inv = s.invoice(iid) | {"airlock": airlock, "reading": reading, "check_reading": check}
    s.save_result(ACTOR, iid, airlock=airlock, reading=reading, check_reading=check, decision=recheck(s, inv))
    print(f"invoice {iid}: {s.invoice(iid)['status']}")
    return iid


def serve(data_dir: Path, port: int) -> None:
    Store(data_dir)
    threading.Thread(target=lambda: desk.work(Store(data_dir)), daemon=True).start()  # its own connection, in its thread
    create_app(data_dir, secrets.token_urlsafe(32), secure_cookies=False, lab_dir=data_dir / "lab").run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2 or args[1] not in ("seed", "serve", "add") or (args[1] == "add" and (len(args) != 3 or args[2] not in KINDS)):
        sys.exit(__doc__)
    folder = Path(args[0])
    _check(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if args[1] == "seed":
        seed(folder)
    elif args[1] == "add":
        add(folder, args[2])
    else:
        serve(folder, int(args[2]) if len(args) > 2 else 8765)
