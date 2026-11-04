"""The audit ledger: every step of every invoice, and every change to vendors and purchase orders, in order.

Each entry carries the SHA-256 of the one before it, so changing or removing any entry breaks every hash after it,
and verify() says where. The table also refuses UPDATE and DELETE outright. Neither stops someone who can rewrite
the whole database file; they make a quiet edit visible. The latest hash can be published elsewhere (for example on
the host's status page) so that a rewrite from the start can be noticed too.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq        INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    invoice_id INTEGER,
    detail     TEXT NOT NULL,
    prev       TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
"""
GENESIS = "0" * 64


def _hash(seq: int, at: str, actor: str, kind: str, invoice_id: int | None, detail: str, prev: str) -> str:
    line = json.dumps([seq, at, actor, kind, invoice_id, detail, prev], separators=(",", ":"))
    return hashlib.sha256(line.encode()).hexdigest()


def append(db: sqlite3.Connection, actor: str, kind: str, detail: dict, invoice_id: int | None = None) -> str:
    """Add one entry and return its hash. Runs inside the caller's transaction, if there is one."""
    row = db.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = json.dumps(detail, sort_keys=True, separators=(",", ":"))
    digest = _hash(seq, at, actor, kind, invoice_id, text, prev)
    db.execute("INSERT INTO ledger (seq, at, actor, kind, invoice_id, detail, prev, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (seq, at, actor, kind, invoice_id, text, prev, digest))
    return digest


def entries(db: sqlite3.Connection, invoice_id: int | None = None) -> list[dict]:
    query = "SELECT seq, at, actor, kind, invoice_id, detail, hash FROM ledger"
    rows = db.execute(query + (" WHERE invoice_id = ?" if invoice_id is not None else "") + " ORDER BY seq",
                      (invoice_id,) if invoice_id is not None else ()).fetchall()
    return [{"seq": r[0], "at": r[1], "actor": r[2], "kind": r[3], "invoice_id": r[4], "detail": json.loads(r[5]), "hash": r[6]}
            for r in rows]


def verify(db: sqlite3.Connection) -> tuple[bool, str]:
    """Recompute the chain from the first entry. Returns (intact, what was found)."""
    prev, expected_seq, count = GENESIS, 1, 0
    for seq, at, actor, kind, invoice_id, detail, stored_prev, digest in db.execute(
            "SELECT seq, at, actor, kind, invoice_id, detail, prev, hash FROM ledger ORDER BY seq"):
        if seq != expected_seq:
            return False, f"entry {expected_seq} is missing"
        if stored_prev != prev or _hash(seq, at, actor, kind, invoice_id, detail, prev) != digest:
            return False, f"entry {seq} was changed"
        prev, expected_seq, count = digest, seq + 1, count + 1
    return True, f"{count} entries, intact; latest {prev[:16]}"
