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
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    added_by   TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    revoked_by TEXT,
    revoked_at TEXT,
    last_seen  TEXT,
    firmware   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS desk_requests (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    at          TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('queued', 'working', 'done', 'failed')),
    audio_bytes INTEGER NOT NULL,
    heard       TEXT,
    intent      TEXT,
    answer      TEXT,
    invoice_id  INTEGER,
    detail      TEXT,
    done_at     TEXT
);
CREATE TABLE IF NOT EXISTS desk_holds (
    id          INTEGER PRIMARY KEY,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    device_name TEXT NOT NULL,
    at          TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS desk_news (
    id         INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    ledger_seq INTEGER NOT NULL UNIQUE,
    at         TEXT NOT NULL,
    text       TEXT NOT NULL,
    spoken     INTEGER NOT NULL DEFAULT 0,
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS desk_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
JSON_COLUMNS = ("airlock", "reading", "check_reading", "decision")
DESK_ASKS_PER_HOUR = 60     # questions one device may send in an hour
DESK_STATUSES_HELD = ("held", "ready")  # what a desk device may put on hold


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _token_hash(token: str) -> str:
    """A device token is 256 random bits, so a plain SHA-256 is enough to keep it unusable if the database leaks."""
    return hashlib.sha256(token.encode()).hexdigest()


def with_desk_holds(decision: dict, holds: list[dict]) -> dict:
    """The checks, plus one failed check for each open hold placed from a desk device: such an invoice stays held,
    whatever the other checks say, until a person releases it."""
    if not holds:
        return decision
    extra = [{"code": "desk", "ok": False, "text": f"Put on hold from the desk device \"{h['device_name']}\" at {h['at']}. "
                                                  "A person releases it on this page."} for h in holds]
    return decision | {"ready": False, "checks": decision["checks"] + extra,
                       "holds": decision["holds"] + [{"code": c["code"], "text": c["text"]} for c in extra]}


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

    def requeue_unfinished(self, actor: str) -> list[int]:
        """Invoices a stopped worker left half-done go back in the queue."""
        with self.tx() as db:
            ids = [r[0] for r in db.execute("UPDATE invoices SET status = 'queued' WHERE status = 'processing' RETURNING id")]
            for iid in ids:
                ledger.append(db, actor, "requeued", {"why": "the worker stopped before finishing it"}, iid)
        return ids

    def save_result(self, actor: str, invoice_id: int, *, airlock: dict, reading: dict | None, check_reading: dict | None, decision: dict) -> None:
        f = decision["facts"]
        with self.tx() as db:
            decision = with_desk_holds(decision, self._open_desk_holds(db, invoice_id))
            db.execute("UPDATE invoices SET status = ?, airlock = ?, reading = ?, check_reading = ?, decision = ?, vendor_id = ?, po_id = ?,"
                       " invoice_number_key = ?, total_cents = ?, currency = ? WHERE id = ?",
                       ("ready" if decision["ready"] else "held", json.dumps(airlock), json.dumps(reading), json.dumps(check_reading),
                        json.dumps(decision), f["vendor_id"], f["po_id"], f["invoice_number_key"], f["total_cents"], f["currency"], invoice_id))
            ledger.append(db, actor, "checked", {"ready": decision["ready"], "holds": [h["code"] for h in decision["holds"]]}, invoice_id)

    def check_again(self, actor: str, invoice_id: int, recheck: Callable[[dict], dict]) -> dict:
        """Re-run the checks on a held or ready invoice, on what was already read, against the records as they are
        now (a purchase order added since, say). Nothing is read again. Returns the invoice as it ends up."""
        with self.tx() as db:
            inv = self.invoice(invoice_id)
            if inv is None:
                raise KeyError(f"no invoice {invoice_id}")
            if inv["status"] not in ("held", "ready"):
                raise ValueError(f"an invoice that is {inv['status']} is not checked again")
            fresh = with_desk_holds(recheck(inv), self._open_desk_holds(db, invoice_id))
            f = fresh["facts"]
            db.execute("UPDATE invoices SET status = ?, decision = ?, vendor_id = ?, po_id = ? WHERE id = ?",
                       ("ready" if fresh["ready"] else "held", json.dumps(fresh), f["vendor_id"], f["po_id"], invoice_id))
            ledger.append(db, actor, "checked again", {"ready": fresh["ready"], "holds": [h["code"] for h in fresh["holds"]]}, invoice_id)
        return self.invoice(invoice_id)

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
                fresh = with_desk_holds(recheck(inv), self._open_desk_holds(db, invoice_id))
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
        """Earlier live invoices (not rejected or failed), or approved ones, with the same file or the same vendor and
        invoice number. The duplicate is the later one: an invoice is not held because a copy arrived after it,
        while that copy is held because of it. If the earlier one is the bad one, rejecting it frees the later."""
        live = "status NOT IN ('rejected', 'failed') AND id != ? AND (id < ? OR status = 'approved')"
        same_file = [r[0] for r in self.db.execute(f"SELECT id FROM invoices WHERE {live} AND sha256 = ?", (invoice_id, invoice_id, sha256))]
        same_number = [r[0] for r in self.db.execute(
            f"SELECT id FROM invoices WHERE {live} AND vendor_id = ? AND invoice_number_key = ?",
            (invoice_id, invoice_id, vendor_id, number_key))] \
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

    def change_password(self, name: str, current: str, new: str) -> None:
        if not self.check_password(name, current):
            raise ValueError("the current password is not right")
        if len(new) < 12:
            raise ValueError("use a password of at least 12 characters")
        salt = os.urandom(16)
        with self.tx() as db:
            db.execute("UPDATE users SET salt = ?, hash = ? WHERE name = ?", (salt, _password_hash(new, salt), name))
            ledger.append(db, name, "password changed", {"name": name})

    def check_password(self, name: str, password: str) -> bool:
        row = self.db.execute("SELECT salt, hash FROM users WHERE name = ?", (name,)).fetchone()
        if row is None:
            _password_hash(password, b"\0" * 16)  # same work either way, so timing does not say which names exist
            return False
        return hmac.compare_digest(_password_hash(password, row["salt"]), row["hash"])

    # Desk devices. A device can read the queue and put an invoice on hold, and nothing else: there is no method here
    # that lets one approve, reject, release or change anything. Each device has its own token, kept only as its
    # hash, and every question it is asked and every hold it places goes in the ledger.

    def add_device(self, actor: str, name: str) -> tuple[int, str]:
        """A new device and its token. The token is returned once, here, and never stored."""
        name = " ".join(str(name or "").split())[:60]
        if not name:
            raise ValueError("give the device a name")
        token = secrets.token_urlsafe(32)
        with self.tx() as db:
            did = db.execute("INSERT INTO devices (name, token_hash, added_by, added_at) VALUES (?, ?, ?, ?)",
                             (name, _token_hash(token), actor, _now())).lastrowid
            ledger.append(db, actor, "device added", {"device_id": did, "name": name, "may": ["read the queue", "hold an invoice"]})
        return did, token

    def revoke_device(self, actor: str, device_id: int) -> None:
        with self.tx() as db:
            row = db.execute("UPDATE devices SET revoked_by = ?, revoked_at = ? WHERE id = ? AND revoked_at IS NULL RETURNING name",
                             (actor, _now(), device_id)).fetchone()
            if row is None:
                raise KeyError(f"no device {device_id} in use")
            ledger.append(db, actor, "device revoked", {"device_id": device_id, "name": row[0]})

    def device_for_token(self, token: str) -> dict | None:
        if not token:
            return None
        row = self.db.execute("SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", (_token_hash(token),)).fetchone()
        return dict(row) if row else None

    def device(self, device_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def devices(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM devices ORDER BY id")]

    def device_seen(self, device_id: int, firmware: str) -> None:
        """When a device last called in, and with which firmware. Telemetry, not a change to any record, so not in
        the ledger."""
        self.db.execute("UPDATE devices SET last_seen = ?, firmware = ? WHERE id = ?", (_now(), firmware[:40], device_id))

    def desk_dir(self) -> Path:
        d = self.dir / "desk"
        d.mkdir(exist_ok=True)
        return d

    def add_desk_request(self, device: dict, wav: bytes) -> int:
        """A question recorded at a device, queued for the desk worker: one at a time per device, a few an hour."""
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        with self.tx() as db:
            if db.execute("SELECT COUNT(*) FROM desk_requests WHERE device_id = ? AND status IN ('queued', 'working')",
                          (device["id"],)).fetchone()[0]:
                raise ValueError("this device already has a question being answered")
            if db.execute("SELECT COUNT(*) FROM desk_requests WHERE device_id = ? AND at > ?", (device["id"], since)).fetchone()[0] >= DESK_ASKS_PER_HOUR:
                raise ValueError(f"a device can ask {DESK_ASKS_PER_HOUR} questions an hour")
            rid = db.execute("INSERT INTO desk_requests (device_id, at, status, audio_bytes) VALUES (?, ?, 'queued', ?)",
                             (device["id"], _now(), len(wav))).lastrowid
            path = self.desk_dir() / f"asked-{rid}.wav"
            path.with_suffix(".part").write_bytes(wav)
            os.replace(path.with_suffix(".part"), path)
        return rid

    def claim_desk_request(self) -> dict | None:
        with self.tx() as db:
            row = db.execute("UPDATE desk_requests SET status = 'working' WHERE id = "
                             "(SELECT id FROM desk_requests WHERE status = 'queued' ORDER BY id LIMIT 1) RETURNING id").fetchone()
        return self.desk_request(row[0]) if row else None

    def requeue_desk_requests(self) -> None:
        """Questions a stopped desk worker left half-answered go back in the queue."""
        self.db.execute("UPDATE desk_requests SET status = 'queued' WHERE status = 'working'")

    def desk_request(self, request_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM desk_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        r = dict(row)
        for c in ("intent", "detail"):
            r[c] = json.loads(r[c]) if r[c] else None
        return r

    def answer_desk_request(self, request_id: int, *, heard: str, intent: dict | None, answer: str, invoice_id: int | None,
                            detail: dict, spoken: bytes | None, failed: bool = False) -> None:
        """Record the answer to a question, and the question, in the ledger. The recording itself is deleted: what
        was heard is kept as text."""
        req = self.desk_request(request_id)
        device = self.device(req["device_id"])
        if spoken:
            out = self.desk_dir() / f"answer-{request_id}.wav"
            out.with_suffix(".part").write_bytes(spoken)
            os.replace(out.with_suffix(".part"), out)
        with self.tx() as db:
            db.execute("UPDATE desk_requests SET status = ?, heard = ?, intent = ?, answer = ?, invoice_id = ?, detail = ?, done_at = ? WHERE id = ?",
                       ("failed" if failed else "done", heard, json.dumps(intent) if intent else None, answer, invoice_id,
                        json.dumps(detail), _now(), request_id))
            ledger.append(db, desk_actor(device), "desk question",
                          {"device_id": device["id"], "request_id": request_id, "heard": heard, "intent": intent, "answer": answer,
                           "failed": failed} | detail, invoice_id)
        (self.desk_dir() / f"asked-{request_id}.wav").unlink(missing_ok=True)

    def hold_from_desk(self, device: dict, invoice_id: int) -> dict:
        """Put an invoice on hold from a desk device. It stays held, whatever its checks say, until a person releases
        it on the invoice's page. Holding is the only change a device can make."""
        with self.tx() as db:
            inv = self.invoice(invoice_id)
            if inv is None:
                raise KeyError(f"no invoice {invoice_id}")
            if inv["status"] not in DESK_STATUSES_HELD:
                raise ValueError(f"an invoice that is {inv['status']} cannot be put on hold")
            if self._open_desk_holds(db, invoice_id):
                raise ValueError("it is already on hold from a desk device")
            db.execute("INSERT INTO desk_holds (invoice_id, device_id, device_name, at) VALUES (?, ?, ?, ?)",
                       (invoice_id, device["id"], device["name"], _now()))
            base = inv["decision"]
            base = base | {"checks": [c for c in base["checks"] if c["code"] != "desk"], "holds": [h for h in base["holds"] if h["code"] != "desk"]}
            decision = with_desk_holds(base, self._open_desk_holds(db, invoice_id))
            db.execute("UPDATE invoices SET status = 'held', decision = ? WHERE id = ?", (json.dumps(decision), invoice_id))
            ledger.append(db, desk_actor(device), "held from desk", {"device_id": device["id"], "was": inv["status"]}, invoice_id)
        return self.invoice(invoice_id)

    def _open_desk_holds(self, db, invoice_id: int) -> list[dict]:
        return [dict(r) for r in db.execute("SELECT * FROM desk_holds WHERE invoice_id = ? AND released_at IS NULL ORDER BY id", (invoice_id,))]

    def desk_holds(self, invoice_id: int) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM desk_holds WHERE invoice_id = ? ORDER BY id", (invoice_id,))]

    def release_desk_holds(self, actor: str, invoice_id: int, note: str, recheck: Callable[[dict], dict]) -> dict:
        """A person lifts the holds placed from desk devices, with a note of why, and the checks run again."""
        if not note.strip():
            raise ValueError("say why the hold is lifted")
        with self.tx() as db:
            inv = self.invoice(invoice_id)
            if inv is None:
                raise KeyError(f"no invoice {invoice_id}")
            held = self._open_desk_holds(db, invoice_id)
            if not held:
                raise ValueError("this invoice has no hold from a desk device")
            db.execute("UPDATE desk_holds SET released_by = ?, released_at = ?, note = ? WHERE invoice_id = ? AND released_at IS NULL",
                       (actor, _now(), note.strip(), invoice_id))
            ledger.append(db, actor, "desk hold released", {"devices": [h["device_name"] for h in held], "note": note.strip()}, invoice_id)
            if inv["status"] in ("held", "ready"):
                fresh = recheck(inv)
                f = fresh["facts"]
                db.execute("UPDATE invoices SET status = ?, decision = ?, vendor_id = ?, po_id = ? WHERE id = ?",
                           ("ready" if fresh["ready"] else "held", json.dumps(fresh), f["vendor_id"], f["po_id"], invoice_id))
                ledger.append(db, actor, "checked again", {"ready": fresh["ready"], "holds": [h["code"] for h in fresh["holds"]]}, invoice_id)
        return self.invoice(invoice_id)

    def desk_counts(self) -> dict:
        rows = dict(self.db.execute("SELECT status, COUNT(*) FROM invoices GROUP BY status").fetchall())
        return {"held": rows.get("held", 0), "ready": rows.get("ready", 0), "waiting": rows.get("queued", 0) + rows.get("processing", 0)}

    # What the devices announce: invoices that have just been held, in words written by code (tally.desk).

    def desk_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_desk_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO desk_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def add_desk_news(self, ledger_seq: int, invoice_id: int, text: str) -> int | None:
        row = self.db.execute("INSERT INTO desk_news (invoice_id, ledger_seq, at, text) VALUES (?, ?, ?, ?) ON CONFLICT(ledger_seq) DO NOTHING RETURNING id",
                              (invoice_id, ledger_seq, _now(), text)).fetchone()
        return row[0] if row else None

    def unspoken_news(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM desk_news WHERE spoken = 0 ORDER BY id")]

    def news_spoken(self, news_id: int, wav: bytes | None, detail: dict) -> None:
        """The announcement's audio is ready (or, with no audio, it is shown as text only)."""
        if wav:
            out = self.desk_dir() / f"news-{news_id}.wav"
            out.with_suffix(".part").write_bytes(wav)
            os.replace(out.with_suffix(".part"), out)
        self.db.execute("UPDATE desk_news SET spoken = ?, detail = ? WHERE id = ?", (1 if wav else -1, json.dumps(detail), news_id))

    def news_after(self, after_id: int, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM desk_news WHERE id > ? AND spoken != 0 ORDER BY id LIMIT ?", (after_id, limit))]

    def latest_news_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM desk_news WHERE spoken != 0").fetchone()[0]


def desk_actor(device: dict) -> str:
    return f"desk: {device['name']}"
