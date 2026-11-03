"""The deploy endpoint of a Tally host. Standard library only; runs as the tally user on 127.0.0.1:8081, behind Caddy.

    POST /deploy
    Authorization: Bearer <token>
    X-Tally-Revision: <git commit>
    body: the repository as a .tar.gz (git archive)

The release is unpacked into /opt/tally/releases/ and checked in the background by its own deploy/release.sh, which
publishes its output at /status.txt. Only code from a release runs: there is no endpoint that runs a command. The
host keeps only the SHA-256 of the token, in /etc/tally/deploy-token.sha256.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import re
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_SHA256 = Path("/etc/tally/deploy-token.sha256").read_text().strip()
RELEASES = Path("/opt/tally/releases")
MAX_BODY = 5 * 1024 * 1024
busy = threading.Lock()


def check(release: Path) -> None:
    try:
        subprocess.run(["bash", str(release / "deploy" / "release.sh"), str(release)], timeout=1800)
    finally:
        busy.release()


class Deploy(BaseHTTPRequestHandler):
    def reply(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/deploy":
            return self.reply(404, "not found\n")
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), TOKEN_SHA256):
            return self.reply(401, "no\n")
        revision = self.headers.get("X-Tally-Revision", "")
        if not re.fullmatch(r"[0-9a-f]{7,40}", revision):
            return self.reply(400, "X-Tally-Revision must be a git commit\n")
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BODY:
            return self.reply(413, f"the release must be under {MAX_BODY} bytes\n")
        body = self.rfile.read(size)
        if not busy.acquire(blocking=False):
            return self.reply(409, "a release is being checked; try again when /status.txt says done\n")
        try:
            release = RELEASES / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{revision[:12]}"
            release.mkdir()
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as t:
                t.extractall(release, filter="data")
            (release / "REVISION").write_text(revision + "\n")
        except Exception as e:
            busy.release()
            return self.reply(400, f"could not unpack the release: {type(e).__name__}\n")
        threading.Thread(target=check, args=(release,), daemon=True).start()
        self.reply(202, f"release {release.name} accepted; its checks are published at /status.txt\n")

    def log_message(self, fmt: str, *args) -> None:  # one line per request, without headers
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8081), Deploy).serve_forever()
