import json

import pytest

from lab.demo import PRELOADED, SAMPLES, reset, seed
from tally.store import Store


def test_the_demo_workspace_has_records_samples_and_a_few_invoices_waiting(tmp_path):
    seed(tmp_path)
    listed = json.loads((tmp_path / "samples.json").read_text())
    assert [x["file"] for x in listed] == [f for _, f, _ in SAMPLES]
    assert all((tmp_path / "samples" / x["file"]).read_bytes()[:5] == b"%PDF-" for x in listed)
    assert not list((tmp_path / "samples").glob("*.src.pdf"))
    s = Store(tmp_path)
    assert {v["name"] for v in s.vendors()} == {"Northwind Office Supply Ltd", "Fabrikam Cleaning Ltd"}
    assert [p["number"] for p in s.pos()] == ["PO-55120", "PO-55121"]
    assert sorted(i["status"] for i in s.invoices()) == ["queued"] * len(PRELOADED)


def test_reset_starts_again_and_never_removes_a_folder_that_is_not_a_demo(tmp_path):
    seed(tmp_path / "demo")
    s = Store(tmp_path / "demo")
    s.add_invoice("visitor", "mine.pdf", b"%PDF-1.7 mine")
    s.db.close()
    reset(tmp_path / "demo")
    assert len(Store(tmp_path / "demo").invoices()) == len(PRELOADED)
    other = tmp_path / "real"
    other.mkdir()
    (other / "tally.db").write_text("the real workspace")
    with pytest.raises(SystemExit, match="does not look like a demo"):
        reset(other)
    assert (other / "tally.db").read_text() == "the real workspace"
