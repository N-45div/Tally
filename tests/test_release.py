"""A release, as deploy/host.py sends it, carries the files as committed: no CRLF line endings, whatever platform
it is sent from. Git for Windows converts them by default, and bash then fails on the first line of release.sh."""
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _host():
    spec = importlib.util.spec_from_file_location("deploy_host", ROOT / "deploy" / "host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT, capture_output=True).returncode != 0,
                    reason="not a git checkout (a release on the host is an unpacked archive)")
def test_a_release_has_no_crlf_line_endings():
    revision, tarball = _host()._release()
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as t:
        files = [m for m in t.getmembers() if m.isfile()]
        data = {m.name: t.extractfile(m).read() for m in files}
    # A binary file (a NUL byte near its start, as git judges it) can hold the bytes \r\n by chance, as a font does.
    crlf = [name for name, b in data.items() if b"\0" not in b[:8000] and b"\r\n" in b]
    assert len(revision) == 40
    assert any(m.name == "deploy/release.sh" for m in files)
    assert crlf == []


@pytest.mark.skipif(subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT, capture_output=True).returncode != 0,
                    reason="not a git checkout (a release on the host is an unpacked archive)")
def test_a_push_carries_the_advisor_settings_only_when_asked_and_nothing_else_changes(monkeypatch):
    host = _host()
    _, plain = host._release()
    monkeypatch.setenv("OPENSERV_API_KEY", "serv_test_value")
    carried = host.with_settings(plain)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as a, tarfile.open(fileobj=io.BytesIO(carried), mode="r:gz") as b:
        before = {m.name: a.extractfile(m).read() for m in a.getmembers() if m.isfile()}
        after = {m.name: b.extractfile(m).read() for m in b.getmembers() if m.isfile()}
        mode = b.getmember("settings/advisor.env").mode
    assert not any(n.startswith("settings/") for n in before)
    assert set(after) - set(before) == {"settings/advisor.env"} and all(after[n] == before[n] for n in before)
    assert after["settings/advisor.env"] == b"OPENSERV_API_KEY=serv_test_value\n" and mode == 0o600
