"""What a file opened by Tally can reach from inside its microVM.

PROBE runs in a fresh microVM with exactly the limits every invoice gets (tally.sandbox.run_in_guest) and reports
what it can see and do. A PDF that broke the parser would be in the same place, with the same reach.

Network checks exchange data, not only connect. The guest's network is Microsandbox's user-space stack, which
answers some denied requests itself (a refusal, an empty DNS reply), so getting bytes back is not the same as
reaching the internet. Each check records what came back, and counts as reached only on a sign of the real service:
a Cloudflare reply from 1.1.1.1, instance data from the cloud metadata address, an answer record in DNS. Secrets are compared by hash: the guest reports the SHA-256 of each of its
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
        return {"result": "no connection: " + type(e).__name__, "reply": ""}
    try:
        if send is None:
            return {"result": "connected", "reply": ""}
        s.sendall(send)
        data = b""
        while len(data) < 1024:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        return {"result": "%d bytes back" % len(data), "reply": data[:300].decode("latin-1")}
    except OSError as e:
        return {"result": "connected, then " + type(e).__name__, "reply": ""}
    finally:
        s.close()

def dns_over_udp(server):
    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(query, (server, 53))
        data = s.recv(512)
        rcode, answers = data[3] & 0x0F, int.from_bytes(data[6:8], "big")
        return {"result": "%d bytes back, rcode %d, %d answer records" % (len(data), rcode, answers), "answers": answers}
    except OSError as e:
        return {"result": "no answer: " + type(e).__name__, "answers": 0}
    finally:
        s.close()

def resolves(name):
    try:
        return {"result": "resolved to " + socket.getaddrinfo(name, 443)[0][4][0], "resolved": True}
    except OSError as e:
        return {"result": "not resolved: " + type(e).__name__, "resolved": False}

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
    seen["reached"] = reached(seen["network"])
    return seen


def reached(network: dict) -> list[str]:
    """The checks whose reply could only have come from the real service, not from the sandbox's own stack."""
    signs = {
        "http 1.1.1.1:80": lambda r: "cloudflare" in r.get("reply", "").lower(),
        "cloud metadata 169.254.169.254:80": lambda r: any(w in r.get("reply", "").lower() for w in ("instance", "interfaces", "hostname")),
        "dns udp 8.8.8.8:53": lambda r: r.get("answers", 0) > 0,
        "name example.com": lambda r: r.get("resolved", False),
        "tcp 1.1.1.1:443": lambda r: False,  # a bare connect proves nothing either way; it is reported, not judged
    }
    return sorted(k for k, sign in signs.items() if k in network and sign(network[k]))
