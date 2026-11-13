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
