#!/bin/bash
# One-time setup of a Tally host: Ubuntu 24.04 on a Vultr VX1 instance. Run as root on first boot, with the first
# release already unpacked:  bash host-setup.sh /opt/tally/releases/<release>
#
# The host ends up with: only ports 80 and 443 open and no SSH; Caddy serving HTTPS on <ip>.sslip.io, the web app
# behind it; a `tally` user that may start microVMs and nothing else, running the web app and the worker as its own
# services; Microsandbox installed for that user; the deploy endpoint; and the first release installed and checked,
# its results published at https://<host>/status.txt.
#
# The first-boot script leaves two files for this one: /etc/tally/inference.env (the model key, for the worker only)
# and /etc/tally/first-user (a name and a password to sign in with). Both are used here and removed.
set -euo pipefail
REL="$1"
WWW=/var/www/tally
export DEBIAN_FRONTEND=noninteractive

note() { echo "$(date -u +%T) $*" | tee -a "$WWW/setup.txt"; }
mkdir -p "$WWW"
trap 'note "setup failed at line $LINENO; see /var/log/tally-first-boot.log in the Vultr console"' ERR
note "setting up"

apt-get update -q
apt-get install -y -q python3-venv python3-pip curl ufw gnupg

# Web ports only. There is no SSH: nobody holds a key for this host, and changes arrive as releases through the
# deploy endpoint. (Vultr's web console still works for an emergency.)
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
systemctl disable --now ssh.socket ssh.service 2>/dev/null || true
note "firewall: 80 and 443 only, ssh off"

if ! apt-get install -y -q caddy; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
IP=$(curl -fsS http://169.254.169.254/latest/meta-data/public-ipv4 || python3 -c \
  "import json, urllib.request; print(json.load(urllib.request.urlopen('http://169.254.169.254/v1.json'))['interfaces'][0]['ipv4']['address'])")
HOST="${IP//./-}.sslip.io"
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
	encode gzip
	header X-Content-Type-Options nosniff
	handle /deploy* {
		reverse_proxy 127.0.0.1:8081
	}
	@published path /status.txt /setup.txt
	handle @published {
		root * $WWW
		file_server
	}
	handle {
		reverse_proxy 127.0.0.1:8080
	}
}
EOF
systemctl restart caddy
note "https://$HOST is up"

id tally >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/tally --shell /bin/bash --groups kvm tally
install -d -o tally -g tally /opt/tally/releases
install -d -o tally -g tally -m 0700 /var/lib/tally/data
chown -R tally:tally /opt/tally/releases "$WWW"
# The tally user's own services (the web app and the worker) run without anyone signed in, and releases restart them.
loginctl enable-linger tally

# Settings. The web app and the worker get separate files, so the web app never has the model key in its
# environment. (Both run as the tally user: the split is by process, not by user.)
umask 077
printf 'TALLY_DATA=/var/lib/tally/data\nTALLY_SECRET_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > /etc/tally/web.env
{ echo TALLY_DATA=/var/lib/tally/data; cat /etc/tally/inference.env 2>/dev/null || true; } > /etc/tally/worker.env
umask 022
chown root:tally /etc/tally/web.env /etc/tally/worker.env
chmod 0640 /etc/tally/web.env /etc/tally/worker.env
rm -f /etc/tally/inference.env
note "settings written; the model key is in the worker's settings only"
runuser -u tally -- bash -c 'curl -fsSL https://install.microsandbox.dev | sh'
note "microsandbox installed: $(runuser -u tally -- /var/lib/tally/.local/bin/msb --version 2>&1 || true)"

install -m 0644 "$REL/deploy/agent.py" /opt/tally/agent.py
cat > /etc/systemd/system/tally-deploy.service <<'EOF'
[Unit]
Description=Tally deploy endpoint
After=network-online.target

[Service]
User=tally
Group=tally
Environment=PATH=/var/lib/tally/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 /opt/tally/agent.py
Restart=always
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now tally-deploy
note "deploy endpoint running"

chown -R tally:tally "$REL"
runuser -u tally -- env PATH=/var/lib/tally/.local/bin:/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$(id -u tally)" \
  bash "$REL/deploy/release.sh" "$REL"
note "first release checked: see /status.txt"

if [ -f /etc/tally/first-user ]; then
  { read -r NAME; read -r PASSWORD; } < /etc/tally/first-user
  printf '%s\n' "$PASSWORD" | runuser -u tally -- "$REL/.venv/bin/python" -m tally.web adduser /var/lib/tally/data "$NAME"
  shred -u /etc/tally/first-user
  unset PASSWORD
  note "$NAME can sign in"
fi
