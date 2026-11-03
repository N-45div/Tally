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
  removed afterwards). What comes back is checked before use: only the expected files, PNG headers, the exact report
  shape, the right file, a verdict that follows from the findings. The checks are tested. The microVM path needs KVM
  and has not yet been run on a KVM host; without one, Tally refuses to open files rather than open them unsandboxed.
- **Reading the invoice.** A vision model on Vultr Serverless Inference reads the page images, never the text layer,
  into fixed fields. Live tests on the clean invoice and two with hidden instructions: every checked field correct,
  the hidden bank account never read, under $0.01 per invoice.

## Run the tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests that call the model need `VULTR_INFERENCE_API_KEY`; tests that start microVMs need a host where
`python -m tally.sandbox doctor` says ready. Both are skipped otherwise.

## License

MIT
