import sqlite3

import pytest

from tally import ledger


@pytest.fixture
def db():
    db = sqlite3.connect(":memory:")
    db.executescript(ledger.SCHEMA)
    for i in range(5):
        ledger.append(db, "alice", "approved", {"invoice": i, "amount": "100.00"}, invoice_id=i)
    db.commit()
    return db


def test_an_untouched_ledger_verifies(db):
    ok, what = ledger.verify(db)
    assert ok and what.startswith("5 entries, intact")
    assert [e["seq"] for e in ledger.entries(db)] == [1, 2, 3, 4, 5]
    assert [e["detail"]["invoice"] for e in ledger.entries(db, invoice_id=3)] == [3]


def test_the_ledger_refuses_edits_and_deletions(db):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE ledger SET actor = 'mallory' WHERE seq = 2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM ledger WHERE seq = 2")


def test_an_edit_made_around_the_triggers_is_found(db):
    # Someone with the database file can drop the trigger first; the chain still shows the edit.
    db.execute("DROP TRIGGER ledger_no_update")
    db.execute("""UPDATE ledger SET detail = '{"amount":"9999.00","invoice":2}' WHERE seq = 3""")
    assert ledger.verify(db) == (False, "entry 3 was changed")


def test_a_removed_entry_is_found(db):
    db.execute("DROP TRIGGER ledger_no_delete")
    db.execute("DELETE FROM ledger WHERE seq = 2")
    assert ledger.verify(db) == (False, "entry 2 is missing")


def test_a_rehashed_entry_breaks_the_next_link(db):
    # Rewriting one entry and its own hash is not enough: the next entry still points at the old hash.
    db.execute("DROP TRIGGER ledger_no_update")
    at, prev = db.execute("SELECT at, prev FROM ledger WHERE seq = 3").fetchone()
    detail = '{"amount":"9999.00","invoice":2}'
    db.execute("UPDATE ledger SET detail = ?, hash = ? WHERE seq = 3", (detail, ledger._hash(3, at, "alice", "approved", 2, detail, prev)))
    assert ledger.verify(db) == (False, "entry 4 was changed")
