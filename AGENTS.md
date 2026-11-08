# Working on Tally

This file is for anyone, person or coding agent, picking up the repository: what Tally is, what is built and how it
fits together, the rules that must not be broken, how to run, test and deploy it, and what is next. The README is
the public description with every measured number; this file is how to work on it.

## What Tally is

An airlock for supplier invoices, for small finance teams. Every invoice is opened in its own throwaway microVM with no
network and no keys; only the page images and a report of anything hidden or active come out. Two vision models of
different families read the images (never the PDF's text layer) into fixed fields. Fixed rules, not a model, decide
whether an invoice is held or ready: vendor on file, bank details on file, purchase order, sums, duplicates. A person
approves, payment is always recorded to the bank details on file, and every step goes in a hash-chained ledger.

## Rules that must not be broken

These are the product. A change that weakens one needs a very good reason, written in the commit and the README.

1. **No untrusted file is parsed outside a microVM.** `tally.sandbox.open_invoice` refuses (raises
   `SandboxUnavailable`) when no microVM can start; there is no in-process fallback. The airlock code
   (`tally/airlock/`) is the only code that runs on a stranger's bytes, and only inside the guest.
2. **The text layer never leaves the guest.** Only `report.json` and `page-N.png` come out, and
   `tally.sandbox.check.check_output` checks them before anything uses them (names, plain files, PNG headers without
   decoding, exact report shape, the right file's SHA-256, a verdict that follows from the findings).
3. **The model decides nothing.** `tally/extract.py` only copies fields from images; it has no tools. Holds come from
   `tally/rules.py`. Two readings by two model families must agree on the fields that decide a payment.
4. **Payment goes to the bank details on file**, never to an account printed on an invoice. Changing bank details on
   file needs a note of how it was confirmed, and the ledger keeps the old ones.
5. **Approval re-runs the checks** inside the same transaction (`Store.decide`), and only a ready invoice can be
   approved. Tally records approvals; it does not move money.
6. **The web app never opens a file and never has the model key.** It stores uploads and records decisions; the worker
   (`tally/pipeline.py`) opens, reads and checks. The key is in the worker's settings only.
7. **The ledger is append-only.** Triggers refuse UPDATE and DELETE; `ledger.verify` recomputes the chain.
8. **Pages run no JavaScript**, under a strict content policy (`default-src 'none'`), and every value from an invoice
   is escaped by the templates. Every form carries a token; cookies are SameSite=Strict and HttpOnly.
9. **The demo never sees the real workspace.** Visitor sessions use `TALLY_DEMO`'s store; people sign in against the
   real one only.
10. **A desk device can read and hold, never approve.** Each device has its own token, kept as a hash. Its calls in
    `tally/web/device.py` are the whole list, and the store offers a device nothing that approves, rejects or
    releases. The model that sorts a spoken question sees only the person's words; answers are written by code from
    records, and nothing printed on an invoice is spoken. Every question and every hold goes in the ledger.

## Map of the code

| Path | What it is |
|---|---|
| `tally/airlock/` | Runs inside the guest only. `inspect.py` is the entry point (`python -m tally.airlock.inspect <pdf> <out>`); `hidden.py` finds text a person cannot see (per-character checks with pypdfium2); `active.py` finds JavaScript, launch and submit actions, embedded files, XFA, encryption (pypdf). Limits: 20 MB, 10 pages, 150 dpi, 40 Mpx per page, checked before any page is drawn. |
| `tally/sandbox/` | `__init__.py`: one Microsandbox microVM per file (`run_in_guest`, `open_invoice`): `--no-net`, 1 CPU, 512 MB, 60 s, a 512 MB guest disk, a 251 MB quota on `/out`, the guest code read-only, removed afterwards, plus a host-side watch that stops a guest writing too much. `check.py`: the output checks. `probe.py`: what a guest can reach (network by data, not connect; fail-closed). `__main__.py`: `prepare`, `doctor`, `open`, `probe`. |
| `tally/vultr.py` | Where a chat model is called: Vultr Serverless Inference, JSON that must match a schema, tried across models, with the cost of each call. |
| `tally/speech.py` | Speech for the desk device, on the same service: Granite Speech, then Whisper, then Qwen3-ASR to listen; Qwen3-TTS, then Chatterbox to speak (licences in the docstring). Only the desk worker calls it. |
| `tally/desk.py` | The desk worker (`python -m tally.desk <data_dir>`): hears a question, sorts it into a fixed menu, answers from records, holds an invoice if told to, and writes and voices an announcement for each new hold. |
| `tally/extract.py` | The reader: page images to fixed fields. Reader models GLM then Qwen; the checker is always the other family. |
| `tally/normalize.py` | Exact comparison: cents, identifiers, IBAN check digits, vendor names without company suffixes, lookalike names, currencies by ISO code. |
| `tally/rules.py` | Every check, reported passed or not: airlock, read, second reading, vendor, IBAN, bank, purchase order, sums, duplicate. |
| `tally/store.py` | SQLite: vendors, purchase orders, invoices, users, desk devices, their questions, holds from the desk, announcements. Every change writes its ledger entry in the same transaction. |
| `tally/ledger.py` | The hash-chained ledger. |
| `tally/pipeline.py` | The worker: claim, open in a microVM, read twice, check, record. `python -m tally.pipeline <data_dir> [workers]`. |
| `tally/web/` | Flask app: home, demo, lab pages, sign-in, queue, invoice review, vendors, purchase orders, ledger, account, devices. `labdata.py` reads the lab's published results; `device.py` is the desk device's calls, by bearer token, never a session. |
| `device/` | The desk device's firmware (PlatformIO, ESP32-S3 with a mic, speaker and screen) and `provision.py`, which sets its server and token over USB. See `device/README.md`. |
| `lab/` | Evaluation, not shipped in the package: `attacks.py` (24 attack files with expected outcomes), `naive.py` (a text-layer agent with a pay tool, to compare with), `guest.py` (11 attacks from inside the microVM), `run_tally.py`, `__main__.py` (`python -m lab`), `fatura.py` (the FATURA benchmark), `load.py` (the load test), `demo.py` (builds the public demo workspace), `desk_local.py` (Tally on a laptop for the desk device). |
| `deploy/` | `host.py` (create a host, push a release, read status), `host-setup.sh` (first boot, as root), `release.sh` (every release, as the tally user), `agent.py` (the deploy endpoint), `lab-latest.sh` (runs the lab on the live release). |
| `tests/` | Everything above. `invoices.py` builds the test invoices, including the attacks. |

## Run it locally

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests that call the model need `VULTR_INFERENCE_API_KEY`; tests that start microVMs need a Linux host with KVM where
`python -m tally.sandbox doctor` says ready (Microsandbox installed, guest files prepared with
`python -m tally.sandbox prepare <dir>` and `TALLY_GUEST_ROOT` pointing at them). Both are skipped otherwise, so a
laptop runs everything except the live and microVM tests; on the host, nothing is skipped.

Useful commands:

```bash
.venv/bin/python -m tally.web adduser <data_dir> <name>      # someone who can sign in (password from the terminal)
.venv/bin/python -m tally.web serve <data_dir> 8080          # the app on 127.0.0.1, for trying it out
.venv/bin/python -m lab.demo seed <folder outside the repo>  # a demo workspace (refuses the repository itself)
.venv/bin/python -m lab <out_dir> 1                          # the lab; without KVM only the naive agent runs
```

Without KVM the worker cannot open anything: uploads fail with "no sandbox could be started", by design. To see the
pages with data locally, a throwaway script can fill a data folder with the airlock run in-process on files you built
yourself; never do that with a file from anyone else, and never put it in the repository.

The desk worker needs no KVM, only the model key, so the desk device can be tried against a laptop with
`lab/desk_local.py`: `seed` a folder outside the repository (records, a person, a device and its token), `serve` it
on the local network with the desk worker beside it, `add ready|bank|lookalike` invoices (saved from a reading the
script writes, so no file is opened), and point the board at `http://<laptop>:8765` with `device/provision.py`. On
Windows, pytest has two failures of its own: path separators in `test_sandbox`, and symlinks needing a privilege in
`test_sandbox_check`.

## The host

A Vultr VX1 (`vx1-g-2c-8g-120s`: 2 vCPU, 8 GB, 120 GB, KVM inside), Ubuntu 24.04, created by
`python deploy/host.py create`. There is no SSH; everything after the first boot arrives as a release.

- **First boot** (`deploy/host-setup.sh`, root): firewall with 80 and 443 only, SSH off; Caddy with HTTPS on
  `<ip>.sslip.io` (`/deploy*` to the deploy endpoint, `/status.txt` and `/setup.txt` as files, everything else to
  the app); a `tally` user in the `kvm` group with lingering user services; Microsandbox for that user; settings in
  `/etc/tally/web.env` (`TALLY_DATA`, `TALLY_SECRET_KEY`) and `/etc/tally/worker.env` (`TALLY_DATA` and the model
  key); the first person who can sign in. Changing any of this needs a new host: create one, check it, then delete
  the old instance.
- **A release** (`python deploy/host.py push`): `git archive HEAD` of committed files only, sent to the deploy
  endpoint with the deploy token (the host keeps only its SHA-256). `deploy/release.sh` installs it under
  `/opt/tally/releases/`, prepares its guest, runs `msb doctor` and the whole test suite with the worker's settings,
  probes a guest, and publishes everything at `/status.txt`. Only if every test passes does it replace the services:
  `tally-web` (gunicorn on 127.0.0.1:8080), `tally-worker` (two workers), `tally-desk` (the desk worker, with the
  worker's settings), `tally-demo-worker`, and `tally-demo-reset.timer` (rebuilds the demo every three hours).
- **Background runs**, one at a time under `/var/lib/tally/lab.lock`, after a passing release: the lab (one waiting
  at most, run on the live release by `lab-latest.sh`, about $0.21 of model calls), the FATURA benchmark (about $1.10,
  only when the airlock, sandbox, reader, rules or benchmark change) and the load test (about $0.30, only when the
  pipeline, sandbox or reader change). Lock waiters do not wake in order.
- **Public pages**: `/` (home), `/demo`, `/lab`, `/lab.txt`, `/lab.json`, `/lab/fatura.json`, `/lab/load.json`,
  `/lab/log/<lab|fatura|load>`, `/status.txt`, `/setup.txt`.
- **Settings for `deploy/host.py`** come from the environment: `VULTR_API_KEY` (create only), `TALLY_DEPLOY_TOKEN`,
  `TALLY_HOST`, and for create also `VULTR_INFERENCE_API_KEY`, `TALLY_FIRST_USER`, `TALLY_FIRST_PASSWORD`. Keep them
  in a private env file outside the repository and load it into the shell; never commit them, never print them.

## How work is done here

- **Small commits, one logical change each**, explained in the message: what changed and why, and for a fix, what
  was seen. Prefixes as in the log: `feat`, `fix`, `test`, `docs`, `chore`, `refactor`, `build`, with a scope.
- **Author: `N DIVIJ <ndivij2004@gmail.com>`.** No co-author or tool trailers.
- **Add files by name** (`git add path ...`), never everything; runtime data (databases, uploads, page images,
  samples) is ignored and must stay out.
- **Real runs, no mocks.** A test drives the real code; a live test runs against the real service or is skipped.
- **Every claim is measured**, in the README and on the pages, and limits are stated beside the results. Nothing is
  described as working before it has run where it says.
- **FATURA is held out**: never tune the reader's instructions or the rules on it. A bug it exposes in Tally may be
  fixed (as the currency comparison was); say so.
- **Writing**: plain sentences, the reader in mind, no marketing words, and no event or competition names anywhere in
  the repository.

## Things learned the hard way

- Microsandbox `--no-net` answers denied traffic itself (HTTP 403 `network_policy_denied`, DNS "no such name"), so
  "bytes came back" is not a leak; the probe judges replies fail-closed.
- The `/out` quota is in whole megabytes (251M for a 250.95 MB limit).
- SQLite connections must be opened in the thread that uses them (`work_in_parallel` does).
- The lab's disk measure is host-wide, so a release installing at the same moment inflates it; leftovers are checked
  by the attack's own microVM name.
- The demo worker holds the demo database open; a rebuild restarts it.
- In a demo session, sign-in still checks passwords against the real workspace.
- FATURA's amounts do not add up on any of its layouts, so the sums check cannot be judged on it.
- Two models still disagree on about a fifth of FATURA invoices (a blank total, a vendor only in a logo, a currency
  printed only as a symbol); those are held, which is the safe side.
- pypdf 7.0 removed `add_js`, which the test invoices use, and every release installs afresh, so it would have
  failed the host's tests; pypdf is kept below 7 until the airlock is checked on it.
- The desk board's microphone (INMP441) wanders slowly: 99.9% of a quiet room's recording was below 100 Hz.
  Scaled to its peak, that drowned speech; the firmware filters below about 50 Hz first.
- Both voices misread "1042" ("ten forty-two", "one thousand and forty-two"), so spoken numbers are formatted by
  code: amounts in words for the currency, phone numbers digit by digit.
- The menu model sometimes files "why is invoice 12 held?" under "held"; a fixed rule moves any question naming an
  invoice number to that invoice, and a hold counts only if a word for holding was said.
- On the laptop used for the device, port 8080 belongs to another service, and Windows' firewall blocks Python on a
  network marked Public until allowed.
- `202 accepted` from the deploy endpoint means the release unpacked, not that it ran. The endpoint answers before
  release.sh starts, and release.sh used to write the status page only at its end, so a release that died early left
  no trace; it now publishes at every step.

## What has been built, in order

- **Nov 3**: the airlock (hidden text, active content, the entry point); the Vultr client and the image reader; the
  microVM runner with output checks; the VX1 host with releases and a status page; the containment probe, made
  fail-closed after Microsandbox's own denials looked like a leak.
- **Nov 4**: the ledger, normalisation, store, rules and worker; the web app; the host rebuilt to run the app and the
  worker with the model key for the worker only; releases gated on every test; fixes found by the host's end-to-end
  test (duplicates, a misread IBAN); the attack lab with a naive agent to compare with, and the attacks from inside
  the microVM, which found and fixed a 200-inch page that stalled the airlock and a guest that could fill the disk.
- **Nov 5**: the public home page, the lab as a page, the public demo with its own workspace and worker, the review
  page with the invoice beside its checks; parallel workers; the FATURA benchmark, which found the currency
  comparison bug; the load test; one waiting lab run per host instead of one per release.
- **Nov 6**: pypdf kept below 7; the desk device: speech in and out on Vultr, devices with their own tokens, holds
  from the desk that only a person releases, the desk worker and its fixed menu, the device calls and the Devices
  page, live tests against the real speech models, and the firmware, flashed on the board and tested with a person's
  voice against a laptop running Tally.

- **Nov 7**: `lab/desk_local.py`, so the device can be tried against a laptop; the release page published at every
  step. The host stopped running releases (below).

Measured results, all from the host, are in the README and live at `/lab`.

## Where things stand (Nov 8) and what to do next

**State.** Everything for the desk device is committed, and the suite passes on a laptop (161 passed; two failures
of Windows' own, see above). The board runs firmware `tally-desk/0.1.0` and has answered a spoken question against a
laptop. The live host still runs release 118c17f from Nov 5: none of the desk device's server code is live there
(`/device/hello` returns 404).

### 1. Get the host running releases again (this blocks everything that follows)

What was seen: since Nov 6, `python deploy/host.py push` gets `202 accepted`, but the release never writes
`/status.txt` and the services are not replaced. It is not the new code: a push of 63f5118 (Nov 5's code) did the
same. A restart from the Vultr dashboard on Nov 7 did not help. Each release ends within two minutes; a POST to
`/deploy` with a body that is not a tarball gets `400 could not unpack`, not `409` busy, so nothing is left running.
The host is otherwise fine: a demo upload was opened in a microVM, read and checked in 12 seconds.

There is no SSH, so look from the instance's web console in the Vultr dashboard (View Console; log in as `root`, the
password is on the instance's page):

```bash
journalctl -u tally-deploy -n 60 --no-pager            # the deploy endpoint's own errors
ls -lt /opt/tally/releases | head                      # did the releases unpack?
ls -la /var/www/tally                                  # can the tally user write status.txt(.new)?
df -h /; df -i /                                       # space and inodes
REL=$(ls -1dt /opt/tally/releases/*/ | head -1); REL=${REL%/}
runuser -u tally -- env XDG_RUNTIME_DIR=/run/user/$(id -u tally) bash -x "$REL/deploy/release.sh" "$REL" 2>&1 | head -40
```

The last line runs the newest release by hand, as the endpoint would, and shows where it stops. Once fixed, push
again: the status page now shows each step as it runs (commit 96e14f2), so a stuck release shows where it is.

### 2. Put the board on the live host and measure it there

1. Sign in on the host, open **Devices**, add a device, copy the token it shows once.
2. Board on USB: `py device/provision.py --server https://$TALLY_HOST` (asks for the token at a hidden prompt).
3. Upload a sample that should be held (e.g. a demo sample with a new bank account, to the real workspace): the board
   should announce it. Ask it questions; try "approve invoice N" (refused) and "put invoice N on hold".
4. Take the timings and costs from the Devices page and the ledger (each "desk question" entry has them), and replace
   "not yet measured against the host" in the README with what was measured.

Without the host, the same can be tried against a laptop with `lab/desk_local.py` (see Run it locally).

### 3. After that

- Faster answers: the voice is about half of the wait; the speech models can stream.
- Updating the device's firmware over the network instead of USB.
- A last pass over the README and the pages, a demo video and slides.

### Moving to another laptop

- Clone the repository and set up as in Run it locally (on Windows, `py` runs Python; PlatformIO with
  `pip install platformio` for the firmware).
- The private settings are not in the repository: copy the env file (`TALLY_HOST`, `TALLY_DEPLOY_TOKEN`,
  `TALLY_INSTANCE_ID`, `VULTR_INFERENCE_API_KEY`) to the new laptop, outside the repository, and load it into the shell
  before deploying. The Vultr account key (`VULTR_API_KEY`) was never in it.
- The board shows up as a USB serial device (Espressif, USB ID 303A); `py -m esptool flash-id` finds its port. It
  keeps its Wi-Fi network across flashing, but is set to the first laptop's test server, so set it up again with
  `device/provision.py`. On Windows, allow Python through the firewall when asked, and if port 8080 is taken, use
  another (the laptop tool uses 8765).
- The board's original firmware (a full 16 MB flash backup, `full-flash-before-tally.bin`) is on the first laptop, in
  `tally-device-backup` in the home folder. Copy it if the board may need to go back; `esptool write-flash 0x0` restores it.
