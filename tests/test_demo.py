import json

import pytest

from lab.demo import HISTORY, PRELOADED, SAMPLES, SUPPLIERS, reset, seed
from tally.store import Store


def test_the_demo_workspace_has_records_samples_and_a_few_invoices_waiting(tmp_path):
    seed(tmp_path)
    listed = json.loads((tmp_path / "samples.json").read_text())
    assert [x["file"] for x in listed] == [f for _, f, _ in SAMPLES]
    assert all((tmp_path / "samples" / x["file"]).read_bytes()[:5] == b"%PDF-" for x in listed)
    assert not list((tmp_path / "samples").glob("*.src.pdf"))
    s = Store(tmp_path)
    assert {v["name"] for v in s.vendors()} == {"Northwind Office Supply Ltd", "Fabrikam Cleaning Ltd", *(n for n, _, _ in SUPPLIERS)}
    assert [p["number"] for p in s.pos()][:2] == ["PO-55120", "PO-55121"]
    assert sorted(i["status"] for i in s.invoices("queued")) == ["queued"] * len(PRELOADED)
    history = s.invoices("approved")
    assert len(history) == len(HISTORY) and len(s.payable("GBP")) == len(HISTORY)  # ready for a payment run
    assert all(i["airlock"]["sandbox"]["backend"].startswith("none: a record") for i in history)  # says no file was opened
    clean = next(p for p in s.pos() if p["number"] == "PO-55120")
    assert clean["spent_cents"] == 0  # the history leaves the samples' purchase order untouched


def test_reset_starts_again_and_never_removes_a_folder_that_is_not_a_demo(tmp_path):
    seed(tmp_path / "demo")
    s = Store(tmp_path / "demo")
    s.add_invoice("visitor", "mine.pdf", b"%PDF-1.7 mine")
    s.db.close()
    reset(tmp_path / "demo")
    assert len(Store(tmp_path / "demo").invoices()) == len(PRELOADED) + len(HISTORY)
    other = tmp_path / "real"
    other.mkdir()
    (other / "tally.db").write_text("the real workspace")
    with pytest.raises(SystemExit, match="does not look like a demo"):
        reset(other)
    assert (other / "tally.db").read_text() == "the real workspace"


def test_the_demo_is_never_built_in_the_repository_or_an_empty_path():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    for bad in (Path(""), Path("."), repo, repo / "somewhere"):
        with pytest.raises(SystemExit, match="refusing"):
            seed(bad)
