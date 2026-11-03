"""What a file opened by Tally can reach from inside its microVM. Runs only where a microVM can start.

PROBE runs inside a fresh microVM with exactly the limits every invoice gets, and reports what it can see and do.
A PDF that broke the parser would be in the same place, with the same reach.
"""
import json
import platform

import pytest

from tally import sandbox

pytestmark = pytest.mark.skipif(sandbox.available() is not None, reason=f"no sandbox here: {sandbox.available()}")

PROBE = r"""
import json, os, socket, platform

def connects(host, port):
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False

def resolves(name):
    try:
        socket.getaddrinfo(name, 443)
        return True
    except OSError:
        return False

def writable(folder):
    try:
        open(os.path.join(folder, "probe"), "w").close()
        return True
    except OSError:
        return False

print(json.dumps({
    "kernel": platform.release(),
    "hostname": socket.gethostname(),
    "cpus": os.cpu_count(),
    "mem_kb": int(open("/proc/meminfo").read().split()[1]),
    "internet_by_ip": connects("1.1.1.1", 443) or connects("8.8.8.8", 53),
    "dns": resolves("example.com"),
    "code_writable": writable("/opt/tally"),
    "invoice_writable": writable("/in"),
    "out_writable": writable("/out"),
    "host_paths": [p for p in ("/etc/tally", "/var/lib/tally", "/opt/tally/releases", "/var/www/tally") if os.path.exists(p)],
    "secret_env": sorted(k for k in os.environ if any(w in k for w in ("KEY", "TOKEN", "SECRET", "VULTR"))),
}))
"""


@pytest.fixture
def seen(tmp_path, monkeypatch):
    # A key in the host's environment, to show it does not reach the guest.
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "probe-not-a-real-key")
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "invoice.pdf").write_bytes(b"%PDF-1.7\n")
    ran = sandbox.run_in_guest(["python", "-c", PROBE], [(tmp_path / "in", "/in", True), (tmp_path / "out", "/out", False)])
    assert ran is not None and ran.returncode == 0, ran and ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


def test_the_guest_has_no_network(seen):
    assert seen["internet_by_ip"] is False
    assert seen["dns"] is False


def test_the_guest_runs_its_own_kernel_with_one_cpu_and_512_mb(seen):
    assert seen["kernel"] != platform.release()
    assert seen["hostname"].startswith("tally-")
    assert seen["cpus"] == 1
    assert seen["mem_kb"] <= 512 * 1024


def test_the_guest_cannot_change_the_code_or_the_invoice(seen):
    assert seen["code_writable"] is False
    assert seen["invoice_writable"] is False
    assert seen["out_writable"] is True  # the one place it may write; what it writes there is checked on the way out


def test_the_guest_sees_no_host_files_and_no_keys(seen):
    assert seen["host_paths"] == []
    assert seen["secret_env"] == []


def test_the_guest_cannot_use_more_than_its_memory(tmp_path):
    ran = sandbox.run_in_guest(["python", "-c", "b = b'x' * (1024 ** 3); print(len(b))"], [])
    assert ran is not None and ran.returncode != 0 and "1073741824" not in ran.stdout
