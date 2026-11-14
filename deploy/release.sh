#!/bin/bash
# Install one release on a Tally host, check it, and run it if every check passes, as the tally user:
#   bash release.sh <release dir>
#
# It installs the release, prepares its microVM guest, runs Microsandbox's host check and the whole test suite (the
# microVM, containment, live model and end-to-end tests all run here: this host has KVM and the model key), probes
# what a guest can reach, and publishes everything it printed at /status.txt. A step that fails is reported, not
# hidden. Only a release whose tests all pass replaces the running web app and workers; otherwise the previous
# release keeps running.
set -uo pipefail
REL="$1"
WWW=/var/www/tally
DATA=/var/lib/tally/data
DEMO=/var/lib/tally/demo
export PATH="$HOME/.local/bin:$PATH" TALLY_GUEST_ROOT="$REL/guest" XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
OUT="$WWW/status.txt.new"
ADVISOR_DIR="$HOME/.config/tally"
ADVISOR_ENV="$ADVISOR_DIR/advisor.env"
# The page is published at every step, and every 15 seconds during the tests, so a release that hangs shows where.
step() { cp "$OUT" "$WWW/status.txt" 2>/dev/null; echo; echo "== $*"; }

run_services() {
  local units="$HOME/.config/systemd/user"
  mkdir -p "$units"
  cat > "$units/tally-web.service" <<EOF
[Unit]
Description=Tally web app ($(basename "$REL"))

[Service]
EnvironmentFile=/etc/tally/web.env
Environment=TALLY_DEMO=$DEMO
ExecStart=$REL/.venv/bin/gunicorn --bind 127.0.0.1:8080 --workers 2 --threads 4 --access-logfile - tally.web:from_env()
Restart=always

[Install]
WantedBy=default.target
EOF
  cat > "$units/tally-worker.service" <<EOF
[Unit]
Description=Tally worker ($(basename "$REL"))

[Service]
EnvironmentFile=/etc/tally/worker.env
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=TALLY_GUEST_ROOT=$REL/guest
ExecStart=$REL/.venv/bin/python -m tally.pipeline $DATA 2
Restart=always

[Install]
WantedBy=default.target
EOF
  # The desk worker answers the desk devices' questions and voices their announcements. It calls the speech models,
  # so it has the worker's settings (the model key); the web app only queues the recordings.
  cat > "$units/tally-desk.service" <<EOF
[Unit]
Description=Tally desk worker ($(basename "$REL"))

[Service]
EnvironmentFile=/etc/tally/worker.env
ExecStart=$REL/.venv/bin/python -m tally.desk $DATA
Restart=always

[Install]
WantedBy=default.target
EOF
  # The advisor asks SERV Reasoning for case files on held invoices and plans for payment runs. It has the SERV key
  # only (none of the workers' settings), and runs only once a push has carried that key.
  for pair in "tally-advisor:$DATA:1.00" "tally-demo-advisor:$DEMO:0.50"; do
    IFS=: read -r name dir budget <<< "$pair"
    cat > "$units/$name.service" <<UNIT
[Unit]
Description=Tally advisor for $dir ($(basename "$REL"))
ConditionPathExists=$ADVISOR_ENV

[Service]
EnvironmentFile=$ADVISOR_ENV
Environment=TALLY_REASONING_BUDGET_USD=$budget
ExecStart=$REL/.venv/bin/python -m tally.advisor $dir
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT
  done
  # The public demo: its own workspace, its own worker, and a timer that rebuilds it every three hours. The workers
  # are restarted after a rebuild, since they hold the old database open.
  cat > "$units/tally-demo-worker.service" <<UNIT
[Unit]
Description=Tally demo worker ($(basename "$REL"))

[Service]
EnvironmentFile=/etc/tally/worker.env
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=TALLY_GUEST_ROOT=$REL/guest
ExecStart=$REL/.venv/bin/python -m tally.pipeline $DEMO
Restart=always

[Install]
WantedBy=default.target
UNIT
  cat > "$units/tally-demo-reset.service" <<UNIT
[Unit]
Description=Rebuild the Tally demo workspace

[Service]
Type=oneshot
WorkingDirectory=$REL
ExecStart=$REL/.venv/bin/python -m lab.demo reset $DEMO
ExecStartPost=/usr/bin/systemctl --user restart tally-demo-worker tally-demo-advisor
UNIT
  cat > "$units/tally-demo-reset.timer" <<UNIT
[Unit]
Description=Rebuild the Tally demo workspace every three hours

[Timer]
OnCalendar=*-*-* 00/3:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  # Rebuilt by every release, as by the timer, so a release that changes what the demo holds shows it at once (the
  # demo's workers are restarted just below). "seed" builds it only where there is none yet, as on a first boot.
  "$REL/.venv/bin/python" -m lab.demo reset "$DEMO" 2>/dev/null || "$REL/.venv/bin/python" -m lab.demo seed "$DEMO"
  systemctl --user daemon-reload
  systemctl --user enable tally-web tally-worker tally-desk tally-demo-worker tally-advisor tally-demo-advisor tally-demo-reset.timer
  systemctl --user restart tally-web tally-worker tally-desk tally-demo-worker tally-advisor tally-demo-advisor tally-demo-reset.timer
  sleep 2
  for u in tally-web tally-worker tally-desk tally-demo-worker tally-advisor tally-demo-advisor tally-demo-reset.timer; do echo "$u: $(systemctl --user is-active "$u")"; done
  echo "next demo rebuild: $(systemctl --user list-timers tally-demo-reset.timer --no-legend 2>/dev/null | awk '{print $1, $2, $3}')"
}

{
  echo "release $(basename "$REL") ($(cat "$REL/REVISION" 2>/dev/null || echo 'no revision')), $(date -u +%FT%TZ)"
  echo "host: $(uname -srm), $(nproc) cpu, $(free -m | awk '/Mem:/ {print $2}') MB"
  # Settings a push may carry (deploy/host.py push --settings): only the advisor's SERV key. They move out of the
  # release folder before anything from it runs, into this user's own settings, readable by this user only.
  if [ -f "$REL/settings/advisor.env" ]; then
    install -d -m 700 "$ADVISOR_DIR" && install -m 600 "$REL/settings/advisor.env" "$ADVISOR_ENV" && rm -rf "$REL/settings" \
      && echo "advisor settings: updated (values not shown)"
  fi
  echo "advisor settings: $([ -r "$ADVISOR_ENV" ] && echo present || echo 'none yet, so the advisor does not run')"
  echo "this page is regenerated by every release, step by step while it is checked; a finished one ends with \"done\""
  step "python";  python3 --version
  step "install"; python3 -m venv "$REL/.venv" && "$REL/.venv/bin/pip" install -q -e "${REL}[dev]" && echo ok
  step "microsandbox"; msb --version; msb doctor; echo "msb doctor exit $?"
  echo "-- options of msb create and msb exec about disk, mounts and limits, for the record:"
  msb create --help 2>&1 | grep -iE -A2 "disk|size|tmpfs|rootfs|volume|mount|limit|overlay|read-only|readonly|quota"
  msb exec --help 2>&1 | grep -iE -A3 "rlimit|limit"
  cd "$REL"
  step "guest files"; .venv/bin/python -m tally.sandbox prepare
  step "sandbox doctor"; .venv/bin/python -m tally.sandbox doctor; echo "exit $?"
  # The tests run with the worker's settings, so the live tests run, and the probe checks that the real key
  # stays out of the guest. The key itself is never printed.
  set -a; . /etc/tally/worker.env; [ -r "$ADVISOR_ENV" ] && . "$ADVISOR_ENV"; set +a
  # A test that runs over 5 minutes has every thread's stack printed; the suite is interrupted at 20 minutes (and
  # pytest then prints where it was), well inside the deploy endpoint's 30, so a stuck release still reports.
  step "tests"
  ( while sleep 15; do cp "$OUT" "$WWW/status.txt"; done ) & PUBLISHER=$!
  PYTHONUNBUFFERED=1 timeout -s INT -k 60 1200 .venv/bin/pytest -q -rs -p no:cacheprovider -o faulthandler_timeout=300
  TESTS=$?; kill "$PUBLISHER" 2>/dev/null; echo "pytest exit $TESTS"
  step "what a file can reach from inside its microVM"; .venv/bin/python -m tally.sandbox probe; echo "exit $?"
  step "one invoice with a hidden instruction, end to end"
  .venv/bin/python -c "from pathlib import Path; from tests.invoices import HIDDEN, make; make(Path('/tmp/tally-sample.pdf'), HIDDEN['white_on_white'][0])" \
    && .venv/bin/python -m tally.sandbox open /tmp/tally-sample.pdf
  step "services"
  if [ "$TESTS" -eq 0 ]; then
    run_services
  else
    echo "not started: the tests failed, so the previous release keeps running"
  fi
  step "lab"
  if [ "$TESTS" -eq 0 ]; then
    # The lab takes minutes, so it runs on after this page is published; its results appear at /lab. It runs on the
    # release that is live when its turn comes (deploy/lab-latest.sh), so one waiting lab is enough: at most one runs
    # and one waits, however many releases come in between.
    mkdir -p "$WWW/lab"
    if [ "$(pgrep -u "$(id -u)" -c -f "flock /var/lib/tally/lab.lock bash .*lab-latest.sh")" -ge 2 ]; then
      echo "a lab is already waiting; it will run on the newest release"
    else
      nohup flock /var/lib/tally/lab.lock bash "$REL/deploy/lab-latest.sh" > "$WWW/lab/run.log" 2>&1 < /dev/null &
      echo "started in the background; results at /lab when it finishes"
    fi
  else
    echo "not run: the tests failed"
  fi
  step "FATURA benchmark"
  # About a dollar of model calls a run, so it runs only when what it measures has changed: the airlock, the sandbox,
  # the reader, the rules or the benchmark itself. It waits for the lab, and the lab waits for it.
  BENCH_VERSION=$(cat "$REL/lab/fatura.py" "$REL/tally/extract.py" "$REL/tally/vultr.py" "$REL/tally/rules.py" \
    "$REL"/tally/airlock/*.py "$REL/tally/sandbox/__init__.py" | sha256sum | cut -c1-16)
  if [ "$TESTS" -ne 0 ]; then
    echo "not run: the tests failed"
  elif [ "$(cat "$WWW/lab/fatura.version" 2>/dev/null)" = "$BENCH_VERSION" ]; then
    echo "up to date for this code ($BENCH_VERSION)"
  elif [ "$(cat "$WWW/lab/fatura.queued" 2>/dev/null)" = "$BENCH_VERSION" ] || pgrep -u "$(id -u)" -f "m lab\.fatura" > /dev/null; then
    echo "already queued or running; not queuing another"
  else
    echo "$BENCH_VERSION" > "$WWW/lab/fatura.queued"
    nohup flock /var/lib/tally/lab.lock sh -c "'$REL/.venv/bin/python' -m lab.fatura '$WWW/lab' 4 4 && echo $BENCH_VERSION > '$WWW/lab/fatura.version'; rm -f '$WWW/lab/fatura.queued'" \
      > "$WWW/lab/fatura.log" 2>&1 < /dev/null &
    echo "started in the background for $BENCH_VERSION; results at /lab when it finishes"
  fi
  step "load test"
  LOAD_VERSION=$(cat "$REL/lab/load.py" "$REL/tally/pipeline.py" "$REL/tally/sandbox/__init__.py" "$REL/tally/extract.py" \
    | sha256sum | cut -c1-16)
  if [ "$TESTS" -ne 0 ]; then
    echo "not run: the tests failed"
  elif [ "$(cat "$WWW/lab/load.version" 2>/dev/null)" = "$LOAD_VERSION" ]; then
    echo "up to date for this code ($LOAD_VERSION)"
  elif [ "$(cat "$WWW/lab/load.queued" 2>/dev/null)" = "$LOAD_VERSION" ] || pgrep -u "$(id -u)" -f "m lab\.load" > /dev/null; then
    echo "already queued or running; not queuing another"
  else
    echo "$LOAD_VERSION" > "$WWW/lab/load.queued"
    nohup flock /var/lib/tally/lab.lock sh -c "'$REL/.venv/bin/python' -m lab.load '$WWW/lab' 20 1 2 4 && echo $LOAD_VERSION > '$WWW/lab/load.version'; rm -f '$WWW/lab/load.queued'" \
      > "$WWW/lab/load.log" 2>&1 < /dev/null &
    echo "started in the background for $LOAD_VERSION; results at /lab when it finishes"
  fi
  # Keep the five newest releases, and always the one the services run.
  RUNNING=$(grep -o '/opt/tally/releases/[^/]*' "$HOME/.config/systemd/user/tally-web.service" 2>/dev/null | head -1)
  ls -1dt /opt/tally/releases/*/ | tail -n +6 | sed 's#/$##' | grep -vxF "${RUNNING:-none}" | xargs -r rm -rf
  echo; echo "done $(date -u +%FT%TZ)"
} > "$OUT" 2>&1
mv "$OUT" "$WWW/status.txt"
