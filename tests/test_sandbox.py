"""The sandbox runner. Runs anywhere for the refusals; the live tests need a host with KVM and Microsandbox."""
import os
from pathlib import Path

import pytest

from tally import sandbox
from tally.sandbox.__main__ import copy_guest_code
from tests.invoices import HIDDEN, make


def test_no_sandbox_means_no_file_is_opened(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no msb on this PATH
    pdf = make(tmp_path / "invoice.pdf")
    with pytest.raises(sandbox.SandboxUnavailable, match="not installed"):
        sandbox.open_invoice(pdf, tmp_path)
    assert not list(tmp_path.glob("tally-*")), "nothing was set up for a file that could not be opened"


def test_only_the_airlock_goes_into_the_guest(tmp_path):
    copy_guest_code(tmp_path)
    files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.py"))
    assert files == ["tally/__init__.py", "tally/airlock/__init__.py", "tally/airlock/active.py",
                     "tally/airlock/hidden.py", "tally/airlock/inspect.py"]
    # Nothing in the guest knows about the model service or its key.
    assert not any("VULTR" in p.read_text() or "urllib" in p.read_text() for p in tmp_path.rglob("*.py"))


live = pytest.mark.skipif(sandbox.available() is not None, reason=f"no sandbox here: {sandbox.available()}")


@live
@pytest.mark.parametrize("hide", [None, "white_on_white", "covered_by_box"])
def test_an_invoice_is_opened_in_a_microvm(tmp_path, hide):
    pdf = make(tmp_path / "invoice.pdf", HIDDEN[hide][0] if hide else None)
    opened = sandbox.open_invoice(pdf, tmp_path)
    r = opened.report
    assert r["sandbox"]["backend"] == "microsandbox" and r["sandbox"]["network"] == "none"
    assert r["verdict"] == ("hold" if hide else "clear"), r["reasons"]
    assert r["images"] == ["page-1.png"] and (opened.out_dir / "page-1.png").exists()
    if hide:
        assert "IBAN DE89" in r["hidden"][0]["text"]


@live
def test_a_hostile_file_cannot_crash_its_way_out(tmp_path):
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"%PDF-1.7\n" + os.urandom(4096))
    r = sandbox.open_invoice(f, tmp_path).report
    assert r["verdict"] == "hold"
