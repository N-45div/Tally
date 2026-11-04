"""Tally's records: vendors and the bank details on file for them, purchase orders, invoices, and who may approve.

One SQLite file in the data folder, next to the uploaded files and the page images. Every change goes through a
method here, and each one writes its ledger entry in the same transaction, so a record and its ledger entry are
saved together or not at all.

Bank details on file are the ones Tally pays to. The account printed on an invoice is only compared with them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tally import ledger
from tally.normalize import bank, key, vendor_key

SCHEMA = ledger.SCHEMA + """
CREATE TABLE IF NOT EXISTS vendors (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    name_key       TEXT NOT NULL UNIQUE,
    aliases        TEXT NOT NULL DEFAULT '[]',
    iban           TEXT NOT NULL DEFAULT '',
    account_number TEXT NOT NULL DEFAULT '',
    sort_code      TEXT NOT NULL DEFAULT '',
    routing_number TEXT NOT NULL DEFAULT '',
    phone          TEXT NOT NULL DEFAULT '',
    added_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id           INTEGER PRIMARY KEY,
    number       TEXT NOT NULL,
    number_key   TEXT NOT NULL UNIQUE,
    vendor_id    INTEGER NOT NULL REFERENCES vendors(id),
    currency     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    added_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id                 INTEGER PRIMARY KEY,
    uploaded_at        TEXT NOT NULL,
    uploaded_by        TEXT NOT NULL,
    filename           TEXT NOT NULL,
    sha256             TEXT NOT NULL,
    bytes              INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'held', 'ready', 'approved', 'rejected', 'failed')),
    airlock            TEXT,
    reading            TEXT,
    check_reading      TEXT,
    decision           TEXT,
    vendor_id          INTEGER REFERENCES vendors(id),
    po_id              INTEGER REFERENCES purchase_orders(id),
    invoice_number_key TEXT,
    total_cents        INTEGER,
    currency           TEXT,
    decided_by         TEXT,
    decided_at         TEXT,
    note               TEXT
);
CREATE TABLE IF NOT EXISTS users (
    name     TEXT PRIMARY KEY,
    salt     BLOB NOT NULL,
    hash     BLOB NOT NULL,
    added_at TEXT NOT NULL
);
"""
JSON_COLUMNS = ("airlock", "reading", "check_reading", "decision")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        for sub in ("files", "pages"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "tally.db", timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """One write transaction: everything inside is saved together, or nothing is."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def log(self, actor: str, kind: str, detail: dict, invoice_id: int | None = None) -> None:
        with self.tx() as db:
            ledger.append(db, actor, kind, detail, invoice_id)

    # Vendors

    def add_vendor(self, actor: str, name: str, *, aliases: list[str] = (), pay_to: dict | None = None, phone: str = "") -> int:
        b = bank(pay_to)
        with self.tx() as db:
            vid = db.execute(
                "INSERT INTO vendors (name, name_key, aliases, iban, account_number, sort_code, routing_number, phone, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name.strip(), vendor_key(name), json.dumps(list(aliases)), b["iban"], b["account_number"], b["sort_code"],
                 b["routing_number"], phone.strip(), _now())).lastrowid
            ledger.append(db, actor, "vendor added", {"vendor_id": vid, "name": name.strip(), "aliases": list(aliases), "bank": b, "phone": phone.strip()})
        return vid

    def change_vendor_bank(self, actor: str, vendor_id: int, pay_to: dict, how_confirmed: str) -> None:
        """Bank details on file change only with a note of how the change was confirmed, and the old ones are kept in the ledger."""
        if not how_confirmed.strip():
            raise ValueError("say how the new bank details were confirmed")
        new = bank(pay_to)
        with self.tx() as db:
            old = self._vendor(db, vendor_id)
            db.execute("UPDATE vendors SET iban = ?, account_number = ?, sort_code = ?, routing_number = ? WHERE id = ?",
                       (new["iban"], new["account_number"], new["sort_code"], new["routing_number"], vendor_id))
            ledger.append(db, actor, "vendor bank details changed",
                          {"vendor_id": vendor_id, "old": old["bank"], "new": new, "how_confirmed": how_confirmed.strip()})

    def _vendor(self, db, vendor_id: int) -> dict:
        row = db.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if row is None:
            raise KeyError(f"no vendor {vendor_id}")
        return self._vendor_dict(row)

    @staticmethod
    def _vendor_dict(row) -> dict:
        v = dict(row)
        v["aliases"] = json.loads(v["aliases"])
        v["bank"] = {k: v.pop(k) for k in ("iban", "account_number", "sort_code", "routing_number")}
        return v

    def vendor(self, vendor_id: int) -> dict:
        return self._vendor(self.db, vendor_id)

    def vendors(self) -> list[dict]:
        return [self._vendor_dict(r) for r in self.db.execute("SELECT * FROM vendors ORDER BY name")]

    # Purchase orders

    def add_po(self, actor: str, number: str, vendor_id: int, currency: str, amount_cents: int) -> int:
        with self.tx() as db:
            self._vendor(db, vendor_id)
            pid = db.execute("INSERT INTO purchase_orders (number, number_key, vendor_id, currency, amount_cents, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (number.strip(), key(number), vendor_id, key(currency), amount_cents, _now())).lastrowid
            ledger.append(db, actor, "purchase order added",
                          {"po_id": pid, "number": number.strip(), "vendor_id": vendor_id, "currency": key(currency), "amount_cents": amount_cents})
        return pid

    def close_po(self, actor: str, po_id: int) -> None:
        with self.tx() as db:
            db.execute("UPDATE purchase_orders SET status = 'closed' WHERE id = ?", (po_id,))
            ledger.append(db, actor, "purchase order closed", {"po_id": po_id})

    def pos(self) -> list[dict]:
        """Purchase orders, each with what approved invoices have already used of it."""
        return [dict(r) for r in self.db.execute(
            "SELECT p.*, v.name AS vendor_name, COALESCE((SELECT SUM(i.total_cents) FROM invoices i"
            " WHERE i.po_id = p.id AND i.status = 'approved'), 0) AS spent_cents"
            " FROM purchase_orders p JOIN vendors v ON v.id = p.vendor_id ORDER BY p.id")]

    # Invoices

    def add_invoice(self, actor: str, filename: str, data: bytes) -> int:
        digest = hashlib.sha256(data).hexdigest()
        path = self.dir / "files" / f"{digest}.pdf"
        if not path.exists():
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        name = Path(filename or "invoice.pdf").name[:200]
        with self.tx() as db:
            iid = db.execute("INSERT INTO invoices (uploaded_at, uploaded_by, filename, sha256, bytes, status) VALUES (?, ?, ?, ?, ?, 'queued')",
                             (_now(), actor, name, digest, len(data))).lastrowid
            ledger.append(db, actor, "uploaded", {"filename": name, "sha256": digest, "bytes": len(data)}, iid)
        return iid

    def file_path(self, invoice: dict) -> Path:
        return self.dir / "files" / f"{invoice['sha256']}.pdf"

    def pages_dir(self, invoice_id: int) -> Path:
        return self.dir / "pages" / str(invoice_id)

    def claim_next(self) -> int | None:
        """Take the oldest queued invoice for processing. Two workers never get the same one."""
        with self.tx() as db:
            row = db.execute("UPDATE invoices SET status = 'processing' WHERE id = "
                             "(SELECT id FROM invoices WHERE status = 'queued' ORDER BY id LIMIT 1) RETURNING id").fetchone()
        return row[0] if row else None

    def save_result(self, actor: str, invoice_id: int, *, airlock: dict, reading: dict | None, check_reading: dict | None, decision: dict) -> None:
        f = decision["facts"]
        with self.tx() as db:
            db.execute("UPDATE invoices SET status = ?, airlock = ?, reading = ?, check_reading = ?, decision = ?, vendor_id = ?, po_id = ?,"
                       " invoice_number_key = ?, total_cents = ?, currency = ? WHERE id = ?",
                       ("ready" if decision["ready"] else "held", json.dumps(airlock), json.dumps(reading), json.dumps(check_reading),
                        json.dumps(decision), f["vendor_id"], f["po_id"], f["invoice_number_key"], f["total_cents"], f["currency"], invoice_id))
            ledger.append(db, actor, "checked", {"ready": decision["ready"], "holds": [h["code"] for h in decision["holds"]]}, invoice_id)

    def fail(self, actor: str, invoice_id: int, why: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE invoices SET status = 'failed', note = ? WHERE id = ?", (why[:500], invoice_id))
            ledger.append(db, actor, "failed", {"why": why[:500]}, invoice_id)

    def decide(self, actor: str, invoice_id: int, approve: bool, note: str, recheck: Callable[[dict], dict]) -> dict:
        """Approve or reject. Approval re-runs the checks first, in the same transaction, against the vendors and
        purchase orders as they are now (another invoice may have used up the purchase order since). Returns the
        invoice as it ends up."""
        with self.tx() as db:
            inv = self.invoice(invoice_id)
            if inv is None:
                raise KeyError(f"no invoice {invoice_id}")
            if approve:
                if inv["status"] != "ready":
                    raise ValueError("only an invoice that passed every check can be approved")
                fresh = recheck(inv)
                if not fresh["ready"]:
                    db.execute("UPDATE invoices SET status = 'held', decision = ? WHERE id = ?", (json.dumps(fresh), invoice_id))
                    ledger.append(db, actor, "held at approval", {"holds": [h["code"] for h in fresh["holds"]]}, invoice_id)
                    return self.invoice(invoice_id)
                vendor = self._vendor(db, inv["vendor_id"])
                db.execute("UPDATE invoices SET status = 'approved', decided_by = ?, decided_at = ?, note = ? WHERE id = ?",
                           (actor, _now(), note.strip(), invoice_id))
                ledger.append(db, actor, "approved", {"amount_cents": inv["total_cents"], "currency": inv["currency"],
                                                      "pay_to": vendor["bank"], "vendor": vendor["name"], "note": note.strip()}, invoice_id)
            else:
                if inv["status"] not in ("held", "ready", "failed"):
                    raise ValueError(f"an invoice that is {inv['status']} cannot be rejected")
                if not note.strip():
                    raise ValueError("say why the invoice is rejected")
                db.execute("UPDATE invoices SET status = 'rejected', decided_by = ?, decided_at = ?, note = ? WHERE id = ?",
                           (actor, _now(), note.strip(), invoice_id))
                ledger.append(db, actor, "rejected", {"note": note.strip()}, invoice_id)
        return self.invoice(invoice_id)

    def invoice(self, invoice_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            return None
        inv = dict(row)
        for c in JSON_COLUMNS:
            inv[c] = json.loads(inv[c]) if inv[c] else None
        return inv

    def invoices(self, status: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT id FROM invoices" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC",
                               (status,) if status else ()).fetchall()
        return [self.invoice(r[0]) for r in rows]

    def others_like(self, invoice_id: int, sha256: str, vendor_id: int | None, number_key: str) -> dict:
        """Other live invoices (not rejected or failed) with the same file, or the same vendor and invoice number."""
        live = "status NOT IN ('rejected', 'failed') AND id != ?"
        same_file = [r[0] for r in self.db.execute(f"SELECT id FROM invoices WHERE {live} AND sha256 = ?", (invoice_id, sha256))]
        same_number = [r[0] for r in self.db.execute(
            f"SELECT id FROM invoices WHERE {live} AND vendor_id = ? AND invoice_number_key = ?", (invoice_id, vendor_id, number_key))] \
            if vendor_id and number_key else []
        return {"same_file": same_file, "same_number": same_number}

    # People

    def add_user(self, actor: str, name: str, password: str) -> None:
        if len(password) < 12:
            raise ValueError("use a password of at least 12 characters")
        salt = os.urandom(16)
        with self.tx() as db:
            db.execute("INSERT INTO users (name, salt, hash, added_at) VALUES (?, ?, ?, ?)", (name, salt, _password_hash(password, salt), _now()))
            ledger.append(db, actor, "user added", {"name": name})

    def check_password(self, name: str, password: str) -> bool:
        row = self.db.execute("SELECT salt, hash FROM users WHERE name = ?", (name,)).fetchone()
        if row is None:
            _password_hash(password, b"\0" * 16)  # same work either way, so timing does not say which names exist
            return False
        return hmac.compare_digest(_password_hash(password, row["salt"]), row["hash"])
