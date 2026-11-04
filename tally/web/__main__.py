"""    python -m tally.web adduser <data_dir> <name>   add a person who can sign in (the password is read from the terminal or stdin)
    python -m tally.web serve <data_dir> [port]     run the app on 127.0.0.1 to try it out; on the internet, use gunicorn
"""
from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

from tally.store import Store
from tally.web import create_app


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "adduser":
        password = getpass.getpass(f"Password for {argv[2]}: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        Store(Path(argv[1])).add_user("admin", argv[2], password)
        print(f"{argv[2]} can sign in")
        return 0
    if argv[:1] == ["serve"] and len(argv) in (2, 3):
        app = create_app(Path(argv[1]), secrets.token_urlsafe(32), secure_cookies=False)
        app.run(host="127.0.0.1", port=int(argv[2]) if len(argv) == 3 else 8080)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
