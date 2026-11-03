"""What a file opened by Tally can reach from inside its microVM.

PROBE runs in a fresh microVM with exactly the limits every invoice gets (tally.sandbox.run_in_guest) and reports
what it can see and do. A PDF that broke the parser would be in the same place, with the same reach.

Network checks exchange data, not only connect: a connect() that succeeds can still lead nowhere, so each check
says whether any bytes came back. Secrets are compared by hash: the guest reports the SHA-256 of each of its
environment values, and the host compares them with its own secret values and with a key planted for the probe, so
no secret is ever sent into the guest to look for it.

    python -m tally.sandbox probe
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path

from tally import sandbox

PLANTED_KEY = "probe-planted-key-not-real"

PROBE = r"""
import hashlib, json, os, platform, socket

def tcp(host, port, send=None):
    try:
        s = socket.create_connection((host, port), timeout=3)
    except OSError as e:
        return "no connection: " + type(e).__name__
    try:
        if send is None:
            return "connected"
        s.sendall(send)
        data = s.recv(64)
        return "data: %d bytes" % len(data) if data else "connected, nothing came back"
    except OSError as e:
        return "connected, then " + type(e).__name__
    finally:
        s.close()

def dns_over_udp(server):
    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(query, (server, 53))
        return "data: %d bytes" % len(s.recv(512))
    except OSError as e:
        return "no answer: " + type(e).__name__
    finally:
        s.close()

def resolves(name):
    try:
        return "resolved: " + socket.getaddrinfo(name, 443)[0][4][0]
    except OSError as e:
        return "not resolved: " + type(e).__name__

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
    "interfaces": sorted(os.listdir("/sys/class/net")),
    "network": {
        "tcp 1.1.1.1:443": tcp("1.1.1.1", 443),
        "http 1.1.1.1:80": tcp("1.1.1.1", 80, b"GET / HTTP/1.0\r\nHost: 1.1.1.1\r\n\r\n"),
        "dns udp 8.8.8.8:53": dns_over_udp("8.8.8.8"),
        "cloud metadata 169.254.169.254:80": tcp("169.254.169.254", 80, b"GET /v1.json HTTP/1.0\r\n\r\n"),
        "name example.com": resolves("example.com"),
    },
    "writable": {d: writable(d) for d in ("/opt/tally", "/in", "/out")},
    "host_paths": [p for p in ("/etc/tally", "/var/lib/tally", "/opt/tally/releases", "/var/www/tally") if os.path.exists(p)],
    "env_sha256": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in os.environ.items()},
}))
"""


def _secret_hashes() -> dict[str, str]:
    """SHA-256 of every value in this process's environment that looks like a secret, plus the planted key."""
    secret = {k: v for k, v in os.environ.items() if any(w in k for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    secret["(planted for the probe)"] = PLANTED_KEY
    return {hashlib.sha256(v.encode()).hexdigest(): k for k, v in secret.items() if v}


def probe(work_dir: Path | None = None) -> dict:
    """Run PROBE in a fresh microVM, with a key planted in this process's environment, and say what it saw."""
    why = sandbox.available()
    if why:
        raise sandbox.SandboxUnavailable(why)
    job = Path(tempfile.mkdtemp(prefix="tally-probe-", dir=work_dir))
    try:
        (job / "in").mkdir()
        (job / "out").mkdir()
        (job / "in" / "invoice.pdf").write_bytes(b"%PDF-1.7\n")
        planted = os.environ.get("TALLY_PROBE_PLANTED_KEY")
        os.environ["TALLY_PROBE_PLANTED_KEY"] = PLANTED_KEY
        try:
            ran = sandbox.run_in_guest(["python", "-c", PROBE], [(job / "in", "/in", True), (job / "out", "/out", False)])
        finally:
            if planted is None:
                os.environ.pop("TALLY_PROBE_PLANTED_KEY", None)
            else:
                os.environ["TALLY_PROBE_PLANTED_KEY"] = planted
    finally:
        shutil.rmtree(job, ignore_errors=True)
    if ran is None or ran.returncode != 0:
        raise RuntimeError("the probe did not finish: " + ("timed out" if ran is None else ran.stderr.strip()[-500:]))
    seen = json.loads(ran.stdout.strip().splitlines()[-1])
    secrets = _secret_hashes()
    seen["leaked_secrets"] = sorted(secrets[h] for h in seen.pop("env_sha256").values() if h in secrets)
    seen["host_kernel"] = platform.release()
    seen["reached"] = sorted(k for k, v in seen["network"].items() if v.startswith(("data:", "resolved:")))
    return seen
