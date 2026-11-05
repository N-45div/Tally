#!/bin/bash
# Run the lab on the release the services run when the lab gets its turn, not the release that queued it, so a
# queue of releases costs one lab run on the newest code. Run by release.sh under the lab lock, as the tally user.
set -uo pipefail
WWW=/var/www/tally
REL=$(grep -o '/opt/tally/releases/[^/]*' "$HOME/.config/systemd/user/tally-web.service" | head -1)
[ -n "$REL" ] && [ -d "$REL" ] || { echo "no running release found"; exit 1; }
set -a; . /etc/tally/worker.env; set +a
export PATH="$HOME/.local/bin:$PATH" TALLY_GUEST_ROOT="$REL/guest"
cd "$REL" && exec "$REL/.venv/bin/python" -m lab "$WWW/lab" 3
