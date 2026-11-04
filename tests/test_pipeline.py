"""The pipeline. Without a sandbox it must refuse; on a host with microVMs and the model key it runs for real."""
import os

import pytest

from tally import ledger, sandbox
from tally.pipeline import WORKER, recheck, work
from tally.store import Store
from tests.invoices import HIDDEN, make


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    s.vid = s.add_vendor("alice", "Northwind Office Supply Ltd", pay_to={"iban": "GB29 NWBK 6016 1331 9268 19"}, phone="+44 117 496 0000")
    s.add_po("alice", "PO-55120", s.vid, "GBP", 500000)
    return s


def _upload(store, tmp_path, name, extra=None, **fields):
    pdf = make(tmp_path / name, extra, **fields)
    return store.add_invoice("bob", name, pdf.read_bytes())


def test_without_a_sandbox_the_invoice_fails_and_is_never_opened(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no msb
    iid = _upload(store, tmp_path, "a.pdf")
    work(store, once=True)
    inv = store.invoice(iid)
    assert inv["status"] == "failed" and "no sandbox" in inv["note"] and inv["airlock"] is None
    assert not store.pages_dir(iid).exists()


def test_a_stopped_worker_leaves_nothing_half_done(store, tmp_path):
    iid = _upload(store, tmp_path, "a.pdf")
    assert store.claim_next() == iid  # the worker took it, then stopped
    assert store.requeue_unfinished(WORKER) == [iid]
    assert store.invoice(iid)["status"] == "queued" and ledger.entries(store.db, iid)[-1]["kind"] == "requeued"


live = pytest.mark.skipif(sandbox.available() is not None or not os.environ.get("VULTR_INFERENCE_API_KEY"),
                          reason="needs a host with microVMs and VULTR_INFERENCE_API_KEY")


@live
def test_three_invoices_end_to_end(store, tmp_path):
    clean = _upload(store, tmp_path, "clean.pdf")
    hidden = _upload(store, tmp_path, "hidden.pdf", HIDDEN["white_on_white"][0])
    moved = _upload(store, tmp_path, "moved.pdf", iban="DE89 3704 0044 0532 0130 00", invoice_number="INV-2026-0419")
    work(store, once=True)

    inv = store.invoice(clean)
    assert inv["status"] == "ready", inv["decision"]["holds"]
    assert inv["reading"]["model"] != inv["check_reading"]["model"]
    assert [e["kind"] for e in ledger.entries(store.db, clean)] == ["uploaded", "opened", "read", "read again", "checked"]
    assert (store.pages_dir(clean) / "page-1.png").exists()

    inv = store.invoice(hidden)
    codes = [h["code"] for h in inv["decision"]["holds"]]
    assert inv["status"] == "held" and "airlock" in codes
    assert "DE89" not in str(inv["reading"]["fields"]).replace(" ", "")  # the hidden account was never read

    inv = store.invoice(moved)
    assert inv["status"] == "held" and [h["code"] for h in inv["decision"]["holds"]] == ["bank"]

    # Approving the clean one records payment to the account on file; the ledger is intact.
    assert store.decide("carol", clean, True, "", recheck=lambda i: recheck(store, i))["status"] == "approved"
    assert ledger.entries(store.db, clean)[-1]["detail"]["pay_to"]["iban"] == "GB29NWBK60161331926819"
    assert ledger.verify(store.db)[0]
