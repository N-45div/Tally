import sqlite3

import pytest

from tally import ledger
from tally.store import Store

NORTHWIND_BANK = {"iban": "GB29 NWBK 6016 1331 9268 19"}


def _decision(ready, vendor_id=None, po_id=None, total=149280):
    return {"ready": ready, "holds": [] if ready else [{"code": "unknown_vendor", "text": "x"}], "checks": [],
            "facts": {"vendor_id": vendor_id, "po_id": po_id, "invoice_number_key": "INV20260418", "total_cents": total, "currency": "GBP"}}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.vid = s.add_vendor("alice", "Northwind Office Supply Ltd", pay_to=NORTHWIND_BANK, phone="+44 117 496 0000")
    s.pid = s.add_po("alice", "PO-55120", s.vid, "gbp", 200000)
    return s


def test_a_vendor_is_known_by_its_name_without_the_suffix(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_vendor("alice", "NORTHWIND OFFICE SUPPLY LIMITED")
    assert [v["name"] for v in store.vendors()] == ["Northwind Office Supply Ltd"]
    assert store.vendor(store.vid)["bank"]["iban"] == "GB29NWBK60161331926819"
    # The failed insert left no ledger entry behind.
    assert [e["kind"] for e in ledger.entries(store.db)] == ["vendor added", "purchase order added"]


def test_bank_details_change_only_with_how_it_was_confirmed_and_the_old_ones_are_kept(store):
    with pytest.raises(ValueError, match="confirmed"):
        store.change_vendor_bank("alice", store.vid, {"iban": "DE89 3704 0044 0532 0130 00"}, "  ")
    store.change_vendor_bank("alice", store.vid, {"iban": "DE89 3704 0044 0532 0130 00"}, "phoned +44 117 496 0000, spoke to Sam")
    change = ledger.entries(store.db)[-1]
    assert change["kind"] == "vendor bank details changed"
    assert change["detail"]["old"]["iban"] == "GB29NWBK60161331926819" and change["detail"]["new"]["iban"] == "DE89370400440532013000"


def test_an_invoice_goes_from_upload_to_approval_and_is_paid_to_the_account_on_file(store):
    iid = store.add_invoice("bob", "../../etc/invoice.pdf", b"%PDF-1.7 test")
    inv = store.invoice(iid)
    assert inv["status"] == "queued" and inv["filename"] == "invoice.pdf" and store.file_path(inv).read_bytes() == b"%PDF-1.7 test"
    assert store.claim_next() == iid and store.claim_next() is None
    store.save_result("worker", iid, airlock={"verdict": "clear"}, reading={}, check_reading={}, decision=_decision(True, store.vid, store.pid))
    assert store.invoice(iid)["status"] == "ready"
    done = store.decide("carol", iid, True, "", recheck=lambda inv: _decision(True, store.vid, store.pid))
    assert done["status"] == "approved" and done["decided_by"] == "carol"
    approved = ledger.entries(store.db, iid)[-1]
    assert approved["kind"] == "approved" and approved["detail"]["pay_to"]["iban"] == "GB29NWBK60161331926819"
    assert store.pos()[0]["spent_cents"] == 149280
    assert [e["kind"] for e in ledger.entries(store.db, iid)] == ["uploaded", "checked", "approved"]
    assert ledger.verify(store.db)[0]


def test_approval_rechecks_and_holds_if_things_changed(store):
    iid = store.add_invoice("bob", "a.pdf", b"%PDF a")
    store.claim_next()
    store.save_result("worker", iid, airlock={}, reading={}, check_reading={}, decision=_decision(True, store.vid, store.pid))
    after = store.decide("carol", iid, True, "", recheck=lambda inv: _decision(False))
    assert after["status"] == "held" and ledger.entries(store.db, iid)[-1]["kind"] == "held at approval"


def test_a_held_invoice_cannot_be_approved_and_a_rejection_needs_a_reason(store):
    iid = store.add_invoice("bob", "a.pdf", b"%PDF a")
    store.claim_next()
    store.save_result("worker", iid, airlock={}, reading={}, check_reading={}, decision=_decision(False))
    with pytest.raises(ValueError, match="passed every check"):
        store.decide("carol", iid, True, "", recheck=lambda inv: _decision(True))
    with pytest.raises(ValueError, match="why"):
        store.decide("carol", iid, False, "", recheck=None)
    assert store.decide("carol", iid, False, "not ours", recheck=None)["status"] == "rejected"


def test_the_same_file_or_number_twice_is_found(store):
    a = store.add_invoice("bob", "a.pdf", b"%PDF same")
    b = store.add_invoice("bob", "b.pdf", b"%PDF same")
    for iid in (a, b):
        store.claim_next()
        store.save_result("worker", iid, airlock={}, reading={}, check_reading={}, decision=_decision(True, store.vid, store.pid))
    seen = store.others_like(b, store.invoice(b)["sha256"], store.vid, "INV20260418")
    assert seen == {"same_file": [a], "same_number": [a]}


def test_passwords(store):
    with pytest.raises(ValueError, match="12"):
        store.add_user("alice", "dave", "short")
    store.add_user("alice", "dave", "correct horse battery")
    assert store.check_password("dave", "correct horse battery")
    assert not store.check_password("dave", "correct horse batterY")
    assert not store.check_password("nobody", "correct horse battery")
    assert "correct horse" not in str(ledger.entries(store.db))


def test_a_held_invoice_can_be_checked_again_without_reading_it_again(store):
    iid = store.add_invoice("bob", "a.pdf", b"%PDF a")
    store.claim_next()
    store.save_result("worker", iid, airlock={}, reading={"x": 1}, check_reading={}, decision=_decision(False))
    after = store.check_again("carol", iid, recheck=lambda inv: _decision(True, store.vid, store.pid))
    assert after["status"] == "ready" and after["reading"] == {"x": 1} and after["po_id"] == store.pid
    assert ledger.entries(store.db, iid)[-1]["kind"] == "checked again"
    store.decide("carol", iid, False, "not ours", recheck=None)
    with pytest.raises(ValueError, match="rejected"):
        store.check_again("carol", iid, recheck=lambda inv: _decision(True))
