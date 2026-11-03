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

- **Airlock: hidden text.** Finds text that is in the file but not visible on the page. Tested on generated invoices.

## Run the tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## License

MIT
