"""What a file opened by Tally can reach from inside its microVM. Runs only where a microVM can start.

The probe (tally.sandbox.probe) runs in a fresh microVM with exactly the limits every invoice gets. A PDF that broke
the parser would be in the same place, with the same reach.
"""
import platform

import pytest

from tally import sandbox
from tally.sandbox.probe import probe

pytestmark = pytest.mark.skipif(sandbox.available() is not None, reason=f"no sandbox here: {sandbox.available()}")


@pytest.fixture(scope="module")
def seen():
    return probe()


def test_nothing_on_the_network_answers_the_guest(seen):
    # Every check sends data and waits for a reply. Which ones connected at all is in the message, for the record.
    assert seen["reached"] == [], seen["network"]


def test_the_guest_runs_its_own_kernel_with_one_cpu_and_its_own_memory(seen):
    assert seen["kernel"] != platform.release()
    assert seen["hostname"].startswith("tally-")
    assert seen["cpus"] == 1
    # The 512M setting shows as about 517 MB of total memory inside the guest; the host has 8 GB.
    assert seen["mem_kb"] < 600 * 1024


def test_the_guest_cannot_change_the_code_or_the_invoice(seen):
    assert seen["writable"] == {"/opt/tally": False, "/in": False, "/out": True}
    # /out is the one place it may write; what it writes there is checked on the way out.


def test_the_guest_sees_no_host_files_and_no_secrets(seen):
    assert seen["host_paths"] == []
    assert seen["leaked_secrets"] == []


def test_the_guest_cannot_use_more_than_its_memory():
    ran = sandbox.run_in_guest(["python", "-c", "b = b'x' * (1024 ** 3); print(len(b))"], [])
    assert ran is not None and ran.returncode != 0 and "1073741824" not in ran.stdout
