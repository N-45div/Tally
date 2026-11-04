# Tally

An airlock for supplier invoices, for small finance teams.

Supplier invoices are files from strangers, and they now reach AI agents that can pay money. A PDF can carry
instructions a person never sees: white text, text under a shape, text off the page, a font too small to read. An
agent that reads the PDF's text layer reads those instructions too.

Tally opens every invoice in a throwaway sandbox with no network and no keys, and lets only two things out: the
pages as images, which are what a person would see, and a report of anything hidden or active in the file. The model
reads the images, never the text layer. Fixed rules, not the model, decide whether an invoice is held, and nothing is
paid until a person approves it.

This README grows with the project. The status of each part is stated as it lands.

## Status

- **Airlock: hidden text.** Finds text that is in the file but not visible on the page: white on white, under a
  shape, off the page, invisible render mode, transparent, under 2pt, dark on dark. Tested on generated invoices,
  including that readable fine print and white text on a dark banner are not flagged.
- **Airlock: active content.** Finds JavaScript, launch and form-submit actions, embedded files, XFA forms and
  encryption, which hold the invoice. Links are reported but do not hold it.
- **Airlock: what leaves.** One command turns a file into page images and a report. Anything that is not a readable
  PDF of at most 10 pages and 20 MB is held without being rendered. The text layer is never written out.
- **Sandbox.** Each file is opened in its own Microsandbox microVM (no network, 1 CPU, 512 MB, a time limit,
  removed afterwards), on a Vultr VX1 host. One invoice takes about 1.5 seconds there, start to finish. What comes
  back is checked before use: only the expected files, PNG headers, the exact report shape, the right file, a verdict
  that follows from the findings. Without KVM, Tally refuses to open files rather than open them unsandboxed.
- **What a file can reach from inside.** Every release on the host runs a probe in a microVM with the same limits.
  Measured on the VX1 host: the guest runs its own kernel (6.12; the host runs 6.8), sees 1 CPU and about 517 MB,
  cannot write the code or the invoice, sees no host files and none of the host's secrets, and is stopped when it
  tries to use 1 GB. On the network, TCP is refused, HTTP to the internet and to the cloud metadata address gets
  Microsandbox's own `403 network_policy_denied`, and DNS gets "no such name": nothing reaches a real service.
  The probe judges fail-closed: a reply it does not recognise as a denial counts as a leak. Its first version only
  checked whether bytes came back and reported a leak; the bytes were these denials, which is why it now records
  every reply.
- **Reading the invoice.** A vision model on Vultr Serverless Inference reads the page images, never the text layer,
  into fixed fields. Live tests on the clean invoice and two with hidden instructions: every checked field correct,
  the hidden bank account never read, under $0.01 per invoice.
- **Two readings, fixed rules.** Each invoice is read twice, by two model families (GLM, then Qwen), and is held if
  the readings differ on the vendor, invoice number, purchase order, currency, total or bank details. Then fixed
  code, not a model, checks: the vendor is on file by its exact name (a lookalike name is called out); the bank
  details printed are the ones on file; the IBAN's check digits hold; the purchase order exists, is this vendor's,
  is open, is in the same currency and has enough left; the numbers add up; and the invoice is not a later copy of
  one seen before. Every check is shown, passed or failed.
- **Approval.** Only an invoice that passed every check can be approved, and the checks run again at that moment.
  Payment always goes to the bank details on file, never to an account printed on an invoice; a change of bank
  details needs a note of how it was confirmed, and the old details stay in the ledger. Tally does not move money
  itself: an approval records what to pay, to whom and to which account.
- **Ledger.** Every step of every invoice, and every change to vendors and purchase orders, goes in an append-only
  ledger where each entry carries the hash of the one before; the ledger page checks the chain.
- **Web app and worker.** People sign in, upload PDFs, see why each invoice is held or ready (both readings side by
  side, the page images, any hidden text quoted as plain text), and approve or reject. The web app never opens a
  file and never has the model key; a separate worker opens, reads and checks. Measured on the VX1 host: three
  invoices uploaded together (a clean one, one with a hidden instruction, one asking for payment to a new account)
  were opened, read twice and checked in 20 seconds in all. The clean one was ready to approve; the other two were
  held, for the hidden text and for the new account. Two readings cost $0.0046 per invoice.

## Run the tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests that call the model need `VULTR_INFERENCE_API_KEY`; tests that start microVMs need a host where
`python -m tally.sandbox doctor` says ready. Both are skipped otherwise.

## Host

`deploy/` sets up a Vultr VX1 instance (Ubuntu 24.04) and sends it releases:

```bash
python deploy/host.py create    # needs VULTR_API_KEY and TALLY_DEPLOY_TOKEN
python deploy/host.py push      # sends the current commit; needs TALLY_HOST and TALLY_DEPLOY_TOKEN
python deploy/host.py status    # prints the host's latest checks
```

The host opens only ports 80 and 443, has SSH turned off, and serves the web app over HTTPS with Caddy. Each
release is installed, tested and probed, and the output is published at `/status.txt`. On the host every test runs,
with none skipped: the microVM, containment, live model and end-to-end ones too. A release replaces the running
web app and worker only if all of its tests pass; otherwise the previous release keeps running.

## Known limits

- The web app and the worker run as the same system user. The model key is kept out of the web app's environment,
  but a break-in to the web app process could still read the worker's settings file.
- The lockout after repeated wrong passwords is kept per server process, so it slows guessing rather than stops it.
- If the sandbox itself were broken into, the files it hands back are checked, but a consistent lie (a clean report
  and clean-looking pages for a bad file) would pass those checks. The rules about vendors, bank details and
  purchase orders do not depend on the sandbox, and nothing is approved without a person.
- The ledger shows a quiet edit; it cannot stop someone who rewrites the whole database file from the start.

## License

MIT
