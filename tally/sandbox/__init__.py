"""Open an untrusted invoice inside a throwaway sandbox. Only its page images and a report come back.

The airlock (tally.airlock) parses PDFs, and PDF parsers have had memory-safety bugs. So it never runs in the process
that holds keys, the vendor list or the payment queue. Each file gets its own fresh sandbox:

  - no network,
  - only the airlock's code and its PDF libraries inside, mounted read-only (no model client, no keys),
  - the one file, mounted read-only,
  - one CPU, 512 MB of memory and a time limit,

and the sandbox is removed afterwards. What comes back is checked here before anything uses it (tally.sandbox.check).

Backend: Microsandbox, one microVM per file with its own kernel, on KVM (https://docs.microsandbox.dev). There is no
in-process fallback: when no sandbox can be started, open_invoice raises SandboxUnavailable and nothing is parsed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from tally.airlock.inspect import MAX_BYTES, MAX_PAGES
from tally.sandbox.check import MAX_PNG_BYTES, MAX_REPORT_BYTES, BadOutput, check_output

IMAGE = os.environ.get("TALLY_SANDBOX_IMAGE", "python:3.12-slim")
GUEST_ROOT = Path(os.environ.get("TALLY_GUEST_ROOT", "/var/lib/tally/guest"))
CPUS = 1
MEMORY = "512M"
TIME_LIMIT_S = 60
GUEST_ENV = ("-e", "PYTHONPATH=/opt/tally:/opt/tally/site", "-e", "PYTHONDONTWRITEBYTECODE=1")
# The most a real airlock run can write: every page at the largest size the checks accept, and the report. A guest
# that writes more is stopped while it runs (the lab measured 2 GB reaching the host in 6 seconds before this).
OUT_LIMIT = MAX_PAGES * MAX_PNG_BYTES + MAX_REPORT_BYTES
WATCH_SECONDS = 0.2


class SandboxUnavailable(RuntimeError):
    """No sandbox can be started on this host, so no file is opened."""


@dataclass
class Opened:
    report: dict      # the airlock's report, checked; or a hold saying why there is none
    out_dir: Path     # holds the checked page images, named in report["images"]
    seconds: float


def _msb(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["msb", *args], capture_output=True, text=True, timeout=timeout)


_doctor_ok = False


def available() -> str | None:
    """Why no sandbox can run here, or None when one can."""
    global _doctor_ok
    if not shutil.which("msb"):
        return "msb (Microsandbox) is not installed"
    if not os.path.exists("/dev/kvm"):
        return "no /dev/kvm: this host cannot run microVMs"
    if not (GUEST_ROOT / "tally" / "airlock" / "inspect.py").exists():
        return f"the guest files are not prepared at {GUEST_ROOT} (python -m tally.sandbox prepare)"
    if not _doctor_ok:
        done = _msb("doctor")
        if done.returncode != 0:
            return "msb doctor failed: " + (done.stdout + done.stderr).strip()[-500:]
        _doctor_ok = True
    return None


def _hold(reason: str, out_dir: Path, size: int) -> dict:
    for p in out_dir.iterdir():  # nothing from a failed run is used
        p.unlink() if p.is_file() or p.is_symlink() else shutil.rmtree(p)
    return {"file": "invoice.pdf", "bytes": size, "pages": 0, "images": [], "active": [], "hidden": [],
            "problems": [reason], "verdict": "hold", "reasons": [reason]}


def _written(folders: list[Path]) -> int:
    total = 0
    for folder in folders:
        for root, _, names in os.walk(folder, followlinks=False):
            for n in names:
                try:
                    total += os.lstat(os.path.join(root, n)).st_size
                except OSError:
                    pass
    return total


def _empty(folder: Path) -> None:
    for p in folder.iterdir():
        p.unlink() if p.is_file() or p.is_symlink() else shutil.rmtree(p, ignore_errors=True)


def _exec_watched(name: str, argv: list[str], writable: list[Path]) -> subprocess.CompletedProcess | None:
    """msb exec, watched from here: the microVM is removed if it runs past the time limit (None) or writes more than
    OUT_LIMIT into its writable folders (returncode -1, and what it wrote is deleted at once)."""
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(["msb", "exec", "--timeout", f"{TIME_LIMIT_S}s", *GUEST_ENV, name, "--", *argv],
                                stdout=out, stderr=err, text=True)
        deadline = time.monotonic() + TIME_LIMIT_S + 30
        stopped = None
        while proc.poll() is None:
            written = _written(writable)
            if written > OUT_LIMIT:
                stopped = f"stopped: wrote {written // 2 ** 20} MB, more than the {OUT_LIMIT // 2 ** 20} MB a real run can"
            elif time.monotonic() > deadline:
                stopped = "timed out"
            if stopped:
                _msb("rm", "--force", name, timeout=60)
                proc.kill()
                proc.wait()
                for folder in writable:
                    _empty(folder)
                break
            time.sleep(WATCH_SECONDS)
        if stopped == "timed out":
            return None
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(proc.args, -1 if stopped else proc.returncode, out.read(), stopped or err.read())


def run_in_guest(argv: list[str], mounts: list[tuple[Path, str, bool]]) -> subprocess.CompletedProcess | None:
    """Run one command in a fresh microVM with Tally's limits, then remove the microVM. None if it timed out; a
    returncode of -1, with the reason as stderr, if it was stopped for writing too much.

    Every microVM gets the same limits: no network, CPUS, MEMORY, a time limit, and the guest code read-only at
    /opt/tally. `mounts` adds folders as (host path, guest path, read-only).
    """
    name = f"tally-{uuid.uuid4().hex[:16]}"
    volumes = ["-v", f"{GUEST_ROOT}:/opt/tally:ro"]
    for host, guest, read_only in mounts:
        volumes += ["-v", f"{host}:{guest}" + (":ro" if read_only else "")]
    try:
        made = _msb("create", "--name", name, "--no-net", "--cpus", str(CPUS), "--memory", MEMORY,
                    "--max-duration", f"{TIME_LIMIT_S + 30}s", *volumes, *GUEST_ENV, IMAGE, timeout=180)
        if made.returncode != 0:
            raise SandboxUnavailable("msb create failed: " + (made.stdout + made.stderr).strip()[-500:])
        return _exec_watched(name, argv, [host for host, _, read_only in mounts if not read_only])
    except subprocess.TimeoutExpired:
        raise SandboxUnavailable("msb create did not finish in time") from None
    finally:
        try:
            _msb("rm", "--force", name, timeout=60)
        except subprocess.TimeoutExpired:
            pass  # --max-duration stops it regardless


def open_invoice(pdf_path: Path, work_dir: Path | None = None) -> Opened:
    """Run the airlock on one file in a fresh microVM and return its checked report and page images."""
    why = available()
    if why:
        raise SandboxUnavailable(why)
    started = time.monotonic()
    job = Path(tempfile.mkdtemp(prefix="tally-", dir=work_dir))
    in_dir, out_dir = job / "in", job / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    size = pdf_path.stat().st_size
    if size > MAX_BYTES:  # refused without opening it anywhere
        report = _hold(f"larger than {MAX_BYTES // (1024 * 1024)} MB", out_dir, size)
        report["sandbox"] = {"backend": None, "image": None, "network": None}
        return Opened(report, out_dir, time.monotonic() - started)
    pdf_bytes = pdf_path.read_bytes()
    (in_dir / "invoice.pdf").write_bytes(pdf_bytes)

    ran = run_in_guest(["python", "-m", "tally.airlock.inspect", "/in/invoice.pdf", "/out"],
                       [(in_dir, "/in", True), (out_dir, "/out", False)])
    if ran is None or ran.returncode != 0:
        why = "timed out" if ran is None else ran.stderr if ran.returncode == -1 else f"exited with {ran.returncode}"
        report = _hold(f"the sandbox did not finish ({why})", out_dir, size)
    else:
        try:
            report = check_output(out_dir, pdf_bytes)
        except BadOutput as e:
            report = _hold(f"the sandbox's output failed a check: {e}", out_dir, size)
    report["sandbox"] = {"backend": "microsandbox", "image": IMAGE, "network": "none"}
    return Opened(report, out_dir, time.monotonic() - started)
