#!/bin/bash
# One-time setup of a Tally host: Ubuntu 24.04 on a Vultr VX1 instance. Run as root on first boot, with the first
# release already unpacked:  bash host-setup.sh /opt/tally/releases/<release>
#
# The host ends up with: only ports 80 and 443 open and no SSH; Caddy serving HTTPS on <ip>.sslip.io; a `tally`
# user that may start microVMs and nothing else; Microsandbox installed for that user; the deploy endpoint; and the
# first release installed and checked, its results published at https://<host>/status.txt.
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
	handle {
		root * $WWW
		file_server
	}
}
EOF
systemctl restart caddy
note "https://$HOST is up"

id tally >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/tally --shell /bin/bash --groups kvm tally
install -d -o tally -g tally /opt/tally/releases /var/lib/tally/guest
chown -R tally:tally /opt/tally/releases "$WWW"
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
runuser -u tally -- env PATH=/var/lib/tally/.local/bin:/usr/bin:/bin bash "$REL/deploy/release.sh" "$REL"
note "first release checked: see /status.txt"
