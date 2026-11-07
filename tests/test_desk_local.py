"""lab.desk_local: Tally on a laptop for the desk device, with invoices written as readings (no file is opened)."""
from pathlib import Path

import pytest

from lab import desk_local
from tally.store import Store


def test_a_local_workspace_has_records_a_person_a_device_and_each_kind_of_invoice(tmp_path):
    folder = tmp_path / "desk"
    folder.mkdir()
    desk_local.seed(folder)
    s = Store(folder)
    token = (folder / "device-token.txt").read_text()
    name, password = (folder / "person.txt").read_text().split()
    assert s.device_for_token(token)["name"] == "Front desk" and s.check_password(name, password)
    got = {kind: s.invoice(desk_local.add(folder, kind)) for kind in desk_local.KINDS}
    assert got["ready"]["status"] == "ready"
    assert [h["code"] for h in got["bank"]["decision"]["holds"]] == ["bank"]
    assert got["lookalike"]["status"] == "held" and "vendor" in [h["code"] for h in got["lookalike"]["decision"]["holds"]]
    assert all(i["airlock"]["sandbox"]["backend"].startswith("none: written by lab.desk_local") for i in got.values())
    with pytest.raises(SystemExit, match="already has records"):
        desk_local.seed(folder)


def test_it_refuses_the_repository_and_the_current_folder():
    for folder in (Path("."), Path(desk_local.__file__).resolve().parent.parent / "data"):
        with pytest.raises(SystemExit, match="outside the repository"):
            desk_local._check(folder)
