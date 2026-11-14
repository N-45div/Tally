# Working on Tally

This file is for anyone, person or coding agent, picking up the repository. Read **In one minute**, **Rules that
must not be broken** and **Where things stand** before changing anything; the rest is reference, to look up as you
need it. The README is the public description with every measured number; this file is how to work on the code.

## In one minute

Tally is an airlock for supplier invoices, for small finance teams. Every invoice is opened in its own throwaway
microVM with no network and no keys; only the page images and a report of anything hidden or active come out. Two
vision models of different families read the images (never the PDF's text layer) into fixed fields. Fixed rules, not
a model, decide whether an invoice is held or ready: vendor on file, bank details on file, purchase order, sums,
duplicates. A person approves, payment is always recorded to the bank details on file, and every step goes in a
hash-chained ledger. A desk device (an ESP32-S3 board with a mic, speaker and screen) announces holds and answers
spoken questions about the queue; it can hold an invoice but never approve one.

Where code cannot judge, an advisor worker asks SERV Reasoning (OpenServ) and only proposes: a case file for each
held invoice (what is wrong, how risky it looks, findings citing evidence, next steps from a fixed list) and a plan
for each payment run a person asks for (which approved invoices to pay now, read against the team's policy in its
own words). Code checks every answer, a person decides, and a person approves the exact plan before Tally writes
the payment file for the bank.

It runs on one Vultr VX1 instance (the app, the workers, a Microsandbox microVM per invoice) and calls Vultr
Serverless Inference for reading and speech: vision and chat to read invoices and sort spoken questions, speech to
text and text to speech for the desk device. The advisor calls SERV Reasoning. Reppo grades the advisor's answers in
the lab, on synthetic invoices only.

Read these first, in this order: `tally/pipeline.py` (what happens to an invoice), `tally/rules.py` (why it is held),
`tally/store.py` (the records and the ledger), `tally/sandbox/__init__.py` (the microVM), `tally/cases.py` and
`tally/payruns.py` (what the advisor may propose, and how code checks it), `tally/desk.py` (the desk device's brain),
`device/src/main.cpp` (the board).

## Rules that must not be broken

These are the product. A change that weakens one needs a very good reason, written in the commit and the README.

1. **No untrusted file is parsed outside a microVM.** `tally.sandbox.open_invoice` refuses (raises
   `SandboxUnavailable`) when no microVM can start; there is no in-process fallback. The airlock code
   (`tally/airlock/`) is the only code that runs on a stranger's bytes, and only inside the guest.
2. **The text layer never leaves the guest.** Only `report.json` and `page-N.png` come out, and
   `tally.sandbox.check.check_output` checks them before anything uses them (names, plain files, PNG headers without
   decoding, exact report shape, the right file's SHA-256, a verdict that follows from the findings).
3. **The model decides nothing.** `tally/extract.py` only copies fields from images; it has no tools. Holds come from
   `tally/rules.py`. Two readings by two model families must agree on the fields that decide a payment. The
   advisor's answers are proposals that code checks before saving: a case file keeps only findings that cite
   evidence it was given and steps allowed for its holds (approving, paying, releasing and changing bank details are
   not on the list), and its risk is never below the floor for its holds; a payment plan chooses only among approved
   invoices and never gives an amount or a payee. SERV is given no tool it could call (only SERV's own `serv_*`).
4. **Payment goes to the bank details on file**, never to an account printed on an invoice. Changing bank details on
   file needs a note of how it was confirmed, and the ledger keeps the old ones. A payment run is approved by the
   hash of the exact plan shown and rebuilt from the records inside that transaction; any change refuses it. The
   payment file is written from the bank details on file; a person sends it to the bank.
5. **Approval re-runs the checks** inside the same transaction (`Store.decide`), and only a ready invoice can be
   approved. Tally records approvals; it does not move money.
6. **The web app never opens a file and never has a model key.** It stores uploads and records decisions; the
   workers (`tally/pipeline.py`, `tally/desk.py`, `tally/advisor.py`) open, read, listen, speak and advise. The Vultr
   key is in the workers' settings only; the SERV key is the advisor's alone.
7. **The ledger is append-only.** Triggers refuse UPDATE and DELETE; `ledger.verify` recomputes the chain.
8. **Pages run no JavaScript**, under a strict content policy (`default-src 'none'`), and every value from an invoice
   is escaped by the templates. Every form carries a token; cookies are SameSite=Strict and HttpOnly.
9. **The demo never sees the real workspace.** Visitor sessions use `TALLY_DEMO`'s store; people sign in against the
   real one only.
10. **A desk device can read and hold, never approve.** Each device has its own token, kept as a hash. Its calls in
    `tally/web/device.py` are the whole list, and the store offers a device nothing that approves, rejects or
    releases. The model that sorts a spoken question sees only the person's words; answers are written by code from
    records, and nothing printed on an invoice is spoken. Every question and every hold goes in the ledger.
11. **Nothing printed on an invoice steers the advisor, and nothing real goes to Reppo.** A case file's evidence is
    built by code from records (`cases.evidence`); of what an invoice prints, only amounts, dates, and an account's
    country and last four characters reach SERV (`tests/test_cases.py` checks it). Reppo's evaluations are public, so
    only the synthetic records `lab/reppo.py` builds are ever sent to it.

## How it fits together

### One invoice, through the code

1. `POST /upload` (`tally/web/__init__.py`) → `Store.add_invoice`: the file is saved as `files/<sha256>.pdf` and
   queued. The web app never opens it.
2. The invoice worker (`python -m tally.pipeline <data_dir> [workers]`) → `Store.claim_next` → `pipeline.process`.
3. `sandbox.open_invoice` starts a microVM (`msb`), runs `python -m tally.airlock.inspect <pdf> <out>` inside it,
   and `sandbox.check.check_output` checks what came out. Page images go to `pages/<invoice id>/`.
4. `extract.read_invoice` twice: `READER_MODELS` (GLM first) and `CHECKER_MODELS` (Qwen first), through
   `vultr.chat_json`, which insists on a strict JSON schema and tries the next model when an answer does not parse.
5. `pipeline.recheck` → `rules.decide`: every check, passed or failed, with a code (`airlock`, `read`,
   `second_reading`, `vendor`, `iban`, `bank`, `po`, `sums`, `duplicate`). Any failed check holds. A hold placed from
   a desk device is added on top as a `desk` check (`store.with_desk_holds`) until a person releases it.
6. `Store.save_result` writes the result and its ledger entry in one transaction (`ledger.append`).
7. A person approves at `POST /invoices/<id>/decide` → `Store.decide(..., recheck=pipeline.recheck)`: the checks run
   again inside the transaction, and only a ready invoice can be approved.

### One spoken question, through the code

1. The board records while BOOT is held (`device/src/audio.h`), filters the microphone's slow drift, and sends 16 kHz
   mono WAV to `POST /device/ask` (`tally/web/device.py`) → `Store.add_desk_request`.
2. The desk worker (`python -m tally.desk <data_dir>`) → `Store.claim_desk_request` → `desk.answer_request`:
   `speech.transcribe` (Granite, then Whisper, then Qwen3-ASR), `desk.understand` (a chat model sorts the words
   into the fixed `MENU`; two fixed rules follow it), `desk.answer` (sentences written by code from records; may call
   `Store.hold_from_desk`), `speech.speak` (Qwen3-TTS, then Chatterbox).
3. `Store.answer_desk_request` stores the answer, its audio (`desk/answer-<id>.wav`) and a ledger entry with the
   heard words, the timings and the cost.
4. The board waits on `GET /device/ask/<id>?wait=15`, then fetches and plays the audio.

Announcements: `desk.scan_news` reads new `checked` and `held at approval` ledger entries, writes one announcement per
newly held invoice (`Store.add_desk_news`), and `desk.speak_news` voices it. The board polls `GET /device/news` every
8 seconds and plays what is new.

### A held invoice's case file, and a payment run, through the code

The advisor (`python -m tally.advisor <data_dir>`) loops: queued payment runs first, then held invoices, while the
day's SERV spend is under `TALLY_REASONING_BUDGET_USD` (`Store.spent_on_reasoning_today`).

1. Case file: `cases.evidence` assembles items with ids from records (failed checks, vendor, its history and bank
   changes, other holds, the purchase order, earlier copies). If no case was written from that evidence
   (`cases.basis`, a hash; three failed tries at most, five minutes apart), `cases.write_case` calls
   `serv.reason_json` with SERV's prompt guard and shadow agent on, `cases.check_proposal` keeps what code can stand
   behind and writes the phone script from the number on file, and `Store.save_case` saves it with a `case file
   written` ledger entry. The invoice page shows the latest one; the queue shows its risk.
2. Payment run: `POST /payruns` → `Store.add_payrun` (queued). `payruns.plan_run`: `candidates` (approved, unpaid,
   this currency, from records), SERV's proposal, `check_plan` (ids on the list only, trimmed to the cash in the
   model's order, overdue flags, code's own `baseline`), `plan_hash`, `Store.save_payrun_plan`. A person approves at
   `POST /payruns/<id>/approve` with the hash → `Store.approve_payrun(..., check=payruns.rebuild)`: one `payments` row
   per invoice (unique, so never paid twice) and a `payment scheduled` ledger entry each. `GET
   /payruns/<id>/payments.csv` is the file for the bank (`payruns.payment_file`).

### Processes

| Process | Command | Model key | On the host |
|---|---|---|---|
| Web app | `gunicorn 'tally.web:from_env()'` (or `python -m tally.web serve <data_dir> <port>` locally) | no | `tally-web` (user service) |
| Invoice worker | `python -m tally.pipeline <data_dir> 2` | yes | `tally-worker` |
| Desk worker | `python -m tally.desk <data_dir>` | yes | `tally-desk` |
| Advisor | `python -m tally.advisor <data_dir>` | the SERV key only | `tally-advisor` and `tally-demo-advisor`, once a push has carried the key |
| Demo worker | `python -m tally.pipeline <demo_dir>` | yes | `tally-demo-worker`, and `tally-demo-reset.timer` every 3 hours |
| Deploy endpoint | `/opt/tally/agent.py` (`deploy/agent.py`, copied at first boot only) | no | `tally-deploy` (system service, runs as `tally`) |

### Data

A workspace is a folder: `tally.db` (SQLite, WAL), `files/` (uploads by SHA-256), `pages/<id>/` (page images from
the microVM), `desk/` (questions and spoken audio; answers kept a day, announcements a week). Tables, all in
`tally/store.py`: `vendors`, `purchase_orders`, `invoices`, `users`, `devices`, `desk_requests`, `desk_holds`,
`desk_news`, `desk_state`, `cases`, `payruns`, `payments`, and `ledger` (`tally/ledger.py`). Payment runs added
tables only: an invoice stays `approved` once paid (the live database's CHECK constraint and `pos()` depend on it);
`payments` says it was paid. Every change goes through a `Store` method that writes
its ledger entry in the same transaction; never write to the tables directly.

## Map of the code

| Path | What it is |
|---|---|
| `tally/airlock/` | Runs inside the guest only. `inspect.py` is the entry point (`python -m tally.airlock.inspect <pdf> <out>`); `hidden.py` finds text a person cannot see (per-character checks with pypdfium2); `active.py` finds JavaScript, launch and submit actions, embedded files, XFA, encryption (pypdf). Limits: 20 MB, 10 pages, 150 dpi, 40 Mpx per page, checked before any page is drawn. |
| `tally/sandbox/` | `__init__.py`: one Microsandbox microVM per file (`run_in_guest`, `open_invoice`): `--no-net`, 1 CPU, 512 MB, 60 s, a 512 MB guest disk, a 251 MB quota on `/out`, the guest code read-only, removed afterwards, plus a host-side watch that stops a guest writing too much. `check.py`: the output checks. `probe.py`: what a guest can reach (network by data, not connect; fail-closed). `__main__.py`: `prepare`, `doctor`, `open`, `probe`. |
| `tally/vultr.py` | Where a chat model is called: Vultr Serverless Inference (OpenAI-compatible), JSON that must match a schema, tried across models, with the cost of each call. `ask_json` is the loop every service shares. |
| `tally/serv.py` | SERV Reasoning (OpenServ, OpenAI-compatible): `reason_json`, with a system prompt always, `max_completion_tokens`, only SERV's own `serv_*` tools, and the `x-openserv-request-id` of each call. |
| `tally/cases.py` | Case files for held invoices: `evidence` (from records), `request_text`, `check_proposal`, `call_script`, `write_case`, the fixed `ACTIONS` and `ALLOWED` per hold, `RISK_FLOOR`. |
| `tally/payruns.py` | Payment runs: `candidates`, `baseline`, `check_plan`, `plan_hash`, `rebuild` (for approval), `plan_run`, `payment_file`. |
| `tally/advisor.py` | The advisor worker: plans queued runs, writes case files, within the day's budget. |
| `tally/speech.py` | Speech for the desk device, on the same service: Granite Speech, then Whisper, then Qwen3-ASR to listen; Qwen3-TTS, then Chatterbox to speak (licences in the docstring). Only the desk worker calls it. |
| `tally/extract.py` | The reader: page images to fixed fields. Reader models GLM then Qwen; the checker is always the other family. |
| `tally/normalize.py` | Exact comparison: cents, identifiers, IBAN check digits, vendor names without company suffixes, lookalike names, currencies by ISO code. |
| `tally/rules.py` | Every check, reported passed or not: airlock, read, second reading, vendor, IBAN, bank, purchase order, sums, duplicate. |
| `tally/store.py` | SQLite: vendors, purchase orders, invoices, users, desk devices, their questions, holds from the desk, announcements. Every change writes its ledger entry in the same transaction. |
| `tally/ledger.py` | The hash-chained ledger: `append`, `entries`, `verify`. |
| `tally/pipeline.py` | The invoice worker: claim, open in a microVM, read twice, check, record. `recheck` is the one function that turns an invoice into a decision. |
| `tally/desk.py` | The desk worker: hears a question, sorts it into a fixed menu, answers from records, holds an invoice if told to, and writes and voices an announcement for each new hold. |
| `tally/web/` | Flask app. `__init__.py`: home, demo, lab pages, sign-in, queue, invoice review, vendors, purchase orders, ledger, account, devices. `device.py`: the desk device's calls, by bearer token, never a session. `labdata.py`: reads the lab's published results. `templates/` (icons as inline SVG in `_icons.html`), `static/` (`tally.css`, the Inter font, the favicon: built from `ui/`, never edited by hand). |
| `ui/` | The stylesheet's source, built with Vite and Tailwind CSS into `tally/web/static/` and committed, so the host needs no Node. Pages still run no JavaScript. See `ui/README.md`. |
| `device/` | The desk device's firmware (PlatformIO, Arduino core 2.0.17): `src/main.cpp` (screen, button, Wi-Fi, server calls, USB console), `src/audio.h` (I2S speaker and mic, WAV in and out), `include/board.h` (pins), `include/roots.h` (trusted CAs), `provision.py` (sets server and token over USB). See `device/README.md`. |
| `lab/` | Evaluation, not shipped in the package: `attacks.py` (24 attack files with expected outcomes), `naive.py` (a text-layer agent with a pay tool, to compare with), `guest.py` (11 attacks from inside the microVM), `run_tally.py`, `__main__.py` (`python -m lab`), `fatura.py` (the FATURA benchmark), `load.py` (the load test), `demo.py` (builds the public demo workspace, with a supplier history), `desk_local.py` (Tally on a laptop for the desk device), `reppo.py` (Reppo's panel grades the advisor's case files and a plan on synthetic invoices, first wording against current). |
| `deploy/` | `host.py` (create a host, push a release, read status), `host-setup.sh` (first boot, as root), `release.sh` (every release, as the tally user), `agent.py` (the deploy endpoint), `lab-latest.sh` (runs the lab on the live release). |
| `tests/` | Everything above. `invoices.py` builds the test invoices, including the attacks. |

## Set up a machine

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # Linux and macOS
.venv/bin/pytest
```

On Windows (the laptop used now), with uv: `uv venv .venv --python 3.12`, then
`uv pip install --python .venv\Scripts\python.exe -e ".[dev]" platformio pyserial esptool`. Python lives on C:; large
caches and toolchains live on D: (uv and pip caches in `D:\ml-cache`, PlatformIO in `D:\platformio` through the user
variable `PLATFORMIO_CORE_DIR`). In Git Bash the venv's programs are `.venv/Scripts/python.exe` and
`.venv/Scripts/pio.exe`.

Node is needed only to change the stylesheet: `cd ui && npm ci && npm run build`, then commit the built files in
`tally/web/static/` (on this laptop Node is in `%LOCALAPPDATA%\Programs\node-v26.13.0-win-x64`, npm's cache in
`D:\ml-cache\npm`).

Useful commands:

```bash
python -m tally.web adduser <data_dir> <name>      # someone who can sign in (password from the terminal)
python -m tally.web serve <data_dir> 8080          # the app on 127.0.0.1, for trying it out
python -m lab.demo seed <folder outside the repo>  # a demo workspace (refuses the repository itself)
python -m lab <out_dir> 1                          # the lab; without KVM only the naive agent runs
```

Without KVM the worker cannot open anything: uploads fail with "no sandbox could be started", by design. To see the
pages with data locally, a throwaway script can fill a data folder with the airlock run in-process on files you built
yourself; never do that with a file from anyone else, and never put it in the repository.

### Private settings

They are never in the repository. Keep them in an env file outside it (on this laptop
`C:\Users\ndivij\.tally\tally-local.env`) and load it only into the command that needs it, never printing a value:

```bash
set -a; . ~/.tally/tally-local.env; set +a
```

| Name | For |
|---|---|
| `VULTR_INFERENCE_API_KEY` | live tests, the desk worker on a laptop, and a new host's workers |
| `OPENSERV_API_KEY` | SERV Reasoning: the advisor, its live tests, `lab.reppo`; reaches the host with `deploy/host.py push --settings` |
| `REPPO_API_KEY` | `lab.reppo` only (Reppo's evaluations are public: synthetic records only) |
| `TALLY_HOST` | `deploy/host.py push` and `status`, and the device's server (`https://$TALLY_HOST`) |
| `TALLY_DEPLOY_TOKEN` | `deploy/host.py push` (the host keeps only its SHA-256) |
| `TALLY_INSTANCE_ID` | the instance in the Vultr dashboard |
| `VULTR_API_KEY` | only `deploy/host.py create`; the account key, not in the file unless added for that |
| `TALLY_FIRST_USER`, `TALLY_FIRST_PASSWORD` | only `create`: the first person who can sign in on a new host |

## Tests

`pytest` runs everything it can. Tests that call a model need `VULTR_INFERENCE_API_KEY` (about $0.01 a run), and the
two live advisor tests need `OPENSERV_API_KEY` (about $0.015 a run); tests that start microVMs need a Linux host with KVM where `python -m tally.sandbox doctor` says ready (Microsandbox
installed, guest files prepared with `python -m tally.sandbox prepare <dir>` and `TALLY_GUEST_ROOT` pointing at
them). Both are skipped otherwise. On the host nothing is skipped: every release runs the whole suite.

Measured on the Windows laptop, Nov 8: 149 passed and 24 skipped without the key; with it, the 12 live speech, desk
and reader tests pass too (the other 12 skips need microVMs). Two failures are Windows' own and not bugs: path separators in `test_sandbox`, and symlinks needing a
privilege in `test_sandbox_check`.

## The desk device

The board: an ESP32-S3 with 16 MB flash and 8 MB PSRAM, an INMP441 microphone, a MAX98357A amplifier and speaker, a
1.8" ST7735 screen and the BOOT button (the OceanLabz "AI Explorer ESP32-S3-CAM"). Pins are in
`device/include/board.h`. On USB it is an Espressif device (USB ID 303A, here `COM3`); `py -m esptool flash-id`
finds the port.

```bash
cd device
pio run                  # build (the first build downloads the pinned toolchain, about 10 minutes)
pio run -t upload        # flash; the Wi-Fi network saved on the board is kept
pio device monitor       # the USB console, 115200; type status, mic, beep, ask 4, news, reboot
python provision.py --port COM3 --server https://$TALLY_HOST     # token at a hidden prompt, or TALLY_DEVICE_TOKEN
```

A device token comes from the **Devices** page of the Tally it talks to, shown once. Opening the serial port resets
the board (it reconnects in a few seconds). The console's `status` prints the firmware, Wi-Fi, server, whether a token
is set (never the token), the last HTTP code, the queue's counts, and whether the speaker and mic started.

To try it without the host, run Tally on a laptop on the same Wi-Fi (`lab/desk_local.py`, no KVM needed, only the
model key):

```bash
python -m lab.desk_local <folder outside the repo> seed            # records, a person, a device and its token
python -m lab.desk_local <folder> serve                             # port 8765, the desk worker beside it
TALLY_DEVICE_TOKEN=$(cat <folder>/device-token.txt) python device/provision.py --server http://<laptop address>:8765
python -m lab.desk_local <folder> add bank                          # a held invoice: the board announces it
```

The board's original firmware (a full 16 MB backup, `full-flash-before-tally.bin`) is on the first laptop, in
`tally-device-backup` in the home folder; `esptool write-flash 0x0 <file>` restores it.

## The host

A Vultr VX1 (`vx1-g-2c-8g-120s`: 2 vCPU, 8 GB, 120 GB, KVM inside), Ubuntu 24.04, created by
`python deploy/host.py create`. There is no SSH; everything after the first boot arrives as a release.

- **First boot** (`deploy/host-setup.sh`, root): firewall with 80 and 443 only, SSH off; Caddy with HTTPS on
  `<ip>.sslip.io` (`/deploy*` to the deploy endpoint, `/status.txt` and `/setup.txt` as files from `/var/www/tally`,
  everything else to the app); a `tally` user in the `kvm` group with lingering user services; Microsandbox for that
  user; settings in `/etc/tally/web.env` (`TALLY_DATA`, `TALLY_SECRET_KEY`) and `/etc/tally/worker.env`
  (`TALLY_DATA` and the model key); the first person who can sign in. Changing any of this needs a new host: create
  one, check it, then delete the old instance.
- **A release** (`python deploy/host.py push`): `git archive HEAD` of committed files only, sent to the deploy
  endpoint with the deploy token. `push --settings` also carries `settings/advisor.env` (OPENSERV_API_KEY, nothing
  else, never committed); `release.sh` moves it to `~/.config/tally/advisor.env` (0600) before anything from the
  release runs. A plain push leaves it in place; the advisors do not start until it exists. `deploy/release.sh` installs it under `/opt/tally/releases/`, prepares its guest,
  runs `msb doctor` and the whole test suite with the worker's settings, probes a guest, and publishes everything at
  `/status.txt`, step by step. Only if every test passes does it replace the services (see Processes).
- **Background runs**, one at a time under `/var/lib/tally/lab.lock`, after a passing release: the lab (one waiting
  at most, run on the live release by `lab-latest.sh`, about $0.21 of model calls), the FATURA benchmark (about $1.10,
  only when the airlock, sandbox, reader, rules or benchmark change) and the load test (about $0.30, only when the
  pipeline, sandbox or reader change). Lock waiters do not wake in order.
- **Public pages**: `/` (home), `/demo`, `/lab`, `/lab.txt`, `/lab.json`, `/lab/fatura.json`, `/lab/load.json`,
  `/lab/log/<lab|fatura|load>`, `/status.txt`, `/setup.txt`.

### When a release does not show up

What the deploy endpoint's answers mean: `202` the release unpacked and `release.sh` was started (not that it ran);
`400 could not unpack` it was not a tarball; `401` wrong token; `409` a release is still being checked. A body that is
not a tarball, sent with the token, is a quick way to ask whether a release is still running (409) or has ended (400);
it leaves an empty folder in `/opt/tally/releases`.

`release.sh` writes `/status.txt` at its first step, so a page that still shows an older release a minute after a
`202` means the script stopped before it could write. The reason is in the deploy endpoint's journal, which only the
instance's web console can read (Vultr dashboard, View Console, `root`, password on the instance's page). This line
publishes it, with permissions and disk space, on the status page, leaving out request lines:

```bash
cd /var/www/tally && (date; journalctl -u tally-deploy -n 40 --no-pager -o cat | grep -v HTTP/; ls -la . lab; df -h /; df -i /) > status.txt 2>&1; chown tally:tally status.txt
```

To run a release by hand from the console, name its folder (the newest folder may be an empty one from the check
above): `runuser -u tally -- env XDG_RUNTIME_DIR=/run/user/$(id -u tally) bash -x <release>/deploy/release.sh <release>`.

## How work is done here

- **Small commits, one logical change each**, explained in the message: what changed and why, and for a fix, what
  was seen. Prefixes as in the log: `feat`, `fix`, `test`, `docs`, `chore`, `refactor`, `build`, with a scope.
- **Author: `N DIVIJ <ndivij2004@gmail.com>`.** No co-author or tool trailers. On a new machine, set it in the
  repository's git config first.
- **Add files by name** (`git add path ...`), never everything; runtime data (databases, uploads, page images,
  samples, `.pio/`) is ignored and must stay out.
- **Real runs, no mocks.** A test drives the real code; a live test runs against the real service or is skipped.
- **Every claim is measured**, in the README and on the pages, and limits are stated beside the results. Nothing is
  described as working before it has run where it says.
- **FATURA is held out**: never tune the reader's instructions or the rules on it. A bug it exposes in Tally may be
  fixed (as the currency comparison was); say so.
- **Writing**: plain sentences, the reader in mind, no marketing words, and no event or competition names anywhere in
  the repository, with one exception the owner asked for on Nov 13: the README's "How this was built" section names
  the event the SERV Reasoning work was built for, and says plainly what existed before it and what was added during
  it. Leave that section as it is. Material written for an outside audience (slides, a video script, a submission
  form) lives outside the repository.
- **Secrets**: never committed, never printed, never passed into a sandbox. A deploy is a production change: push
  only what has passed its tests locally.

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
- `202 accepted` from the deploy endpoint means the release unpacked, not that it ran (see "When a release does not
  show up").
- Releases stopped running from Nov 6 to Nov 8 because they were pushed from Windows: Git for Windows sets
  `core.autocrlf=true`, `git archive` honours it, and bash failed on `release.sh`'s first lines (`set: pipefail:
  invalid option name`, `` syntax error near unexpected token `$'{\r'' ``) before it could write the status page.
  Only the deploy journal showed it. `deploy/host.py` now archives with `core.autocrlf=false`, `.gitattributes`
  keeps `*.sh` LF, and `tests/test_release.py` fails if a release carries CRLF.
- Windows: when its "allow access?" prompt for Python is dismissed, the firewall adds rules that *block* that
  python.exe on that network type, and a block beats any allow. The board then gets "connection refused" from a
  laptop server. Fix, in an admin PowerShell: `Get-NetFirewallRule -Direction Inbound | Where-Object { $_.DisplayName
  -eq 'python.exe' -and $_.Action -eq 'Block' } | Set-NetFirewallRule -Action Allow -RemoteAddress LocalSubnet`.
- The desk board froze whenever no computer was reading its USB console: this Arduino core's USB console waits for
  ever for room once its buffer is full, and an announcement's long log lines filled it. Opening the console
  "fixed" it, which hid the cause for hours. `logLine` now writes only what fits (firmware 0.1.5).
- The board's Arduino core is compiled with a 5,760-byte TCP window (`CONFIG_LWIP_TCP_WND_DEFAULT`), so over a long
  round trip (India to New Jersey, 277 ms) it reads at most about 20 KB/s, often less. A 614 KB announcement took
  36 s. 8 kHz mu-law streamed while playing (`speech.for_device`, `streamWav`) broke up into noise when the download
  fell behind; the device is served plain 24 kHz speech, and is best with Tally on its network or a nearby region.
- After the Wi-Fi dropped once, firmware 0.1.0 crash-looped in the Wi-Fi driver (`scan_build_chan_list`) on every
  join: a reconnect started while the library was already reconnecting, then a corrupted saved network. The board now
  joins only the network in Tally's own settings (`provision.py --wifi`), and only the firmware reconnects.
- The board's speaker is 16 ohm, 0.25 W. Turning the volume past 100% clipped and overdrove it ("not clear"); the
  voice is shaped for it on the server instead (`speech.for_speaker`): 400 Hz high-pass, +6 dB above 2 kHz, a 5:1
  compressor, -10 dBFS.
- Live tests that ask the menu model are not perfectly repeatable; a release failed twice on "how many" questions
  until a fixed rule made "how many" a count (`desk.fixed_rules`).
- On the first laptop, port 8080 belonged to another service; the laptop tool uses 8765.
- SERV Reasoning refuses a request without a system prompt (400), and its OpenAI models refuse `max_tokens` (400,
  "use max_completion_tokens"); Gemini and Claude accept `max_completion_tokens` too, so `tally/serv.py` always sends
  that. SERV has no Qwen, GLM or DeepSeek (removed in Aug and Sep), so the advisor's families are OpenAI and Google.
  Image input is not documented, which is one reason reading stays on Vultr.
- SERV generates its reasoning plan from the system prompt and caches it per organisation for 30 days, so the
  advisor's system prompts are fixed text and everything about one invoice or run goes in the user message.
- The first wording of the case-file risk guidance called an unannounced switch to a German account, on a vendor
  paid six times from a UK one and for 3.75 times its usual amount, "medium, likely a mismatch". Naming the signals
  of invoice fraud made it "high". Graded by Reppo on six isolated cases (Nov 13): first wording 95.2, the one in use
  100 (all "ship"). A third, written after Reppo marked down one summary, graded 96.2 and was not kept.
- A lab can mislead its grader: the first Reppo run put every held invoice under one vendor, so each case file saw
  the others' holds (a bank-change attempt made a sums error "high"), and Reppo marked it 53 to 56. Each case now
  gets a workspace of its own. Reppo's evaluators also disagree run to run ("review" with low confidence on answers
  scored 94 to 100), so compare means over several cases, never one answer.

## Where things stand (Nov 9)

- **Code**: committed and on GitHub; the suite passes locally (see Tests).
- **Host** (`TALLY_HOST`): running the latest release, whole suite passing there, the desk worker beside the web app.
  The person who can sign in is `ndivij`; the device "Frontdesk" is registered there.
- **Board**: firmware `tally-desk/0.1.5`, volume 100%, currently set to Tally on the laptop
  (`http://192.168.1.4:8765`, the `lab/desk_local.py` workspace in `C:/Users/ndivij/tally-desk-local`) because of the
  download limit above. To point it at the host: `provision.py --server https://$TALLY_HOST` with
  `TALLY_DEVICE_TOKEN` from the private settings.
- **Video and submission material** live outside the repository: `D:/tally-video` (a HyperFrames project; `build2.py`
  builds it, `narrate.py` makes the narration with ElevenLabs) and `C:/Users/ndivij/tally-submission`.

### What is next

1. A host in a Vultr region near the desk, so the board can use it directly (see the TCP window above), or audio
   the board can stream at its link speed without breaking up.
2. Faster answers: the voice is about half of the wait; the speech models can stream.
3. Updating the device's firmware over the network instead of USB.

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
  step. The host stopped running releases.
- **Nov 8**: moved to a new laptop (Windows, uv, PlatformIO on D:); the firmware builds there; the README explains how
  Tally runs on Vultr; this file reorganised so a newcomer can find their way; the host's stuck releases traced to
  CRLF line endings from Windows and fixed; the host running the desk code, and the board on it.
- **Nov 13**: judgement from SERV Reasoning, decisions from code: the SERV client beside the Vultr one; case files for
  held invoices; payment runs planned from a policy in plain words, approved by hash, with a payment file for the
  bank; the advisor worker and its services on the host, its key carried by `push --settings`; a supplier history
  in the demo; Reppo grading the advisor in the lab, which kept one wording and turned down another. How the
  options were chosen is in the README's "How this was built".
