"""One invoice, start to finish: open it in a microVM, read it twice, run the fixed rules, record every step.

    python -m tally.pipeline <data_dir>     work through the queue until stopped

This worker is the only part of Tally that holds the model key. The web app only queues files and records
decisions; it never opens a file and never calls a model.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from tally import rules, sandbox
from tally.extract import CHECKER_MODELS, READER_MODELS, read_invoice
from tally.store import Store
from tally.vultr import Answer, InferenceError

WORKER = "tally worker"


def _reading(answer: Answer) -> dict:
    return {"fields": answer.data, "model": answer.model, "seconds": round(answer.seconds, 2), "usd": round(answer.usd, 5),
            "attempts": answer.attempts}


def recheck(store: Store, inv: dict) -> dict:
    """The rules, on what was read, against the vendors, purchase orders and invoices as they are now."""
    reading, check = inv["reading"] or {}, inv["check_reading"] or {}
    return rules.decide(inv["airlock"] or {}, reading.get("fields"), check.get("fields"),
                        reader_model=reading.get("model"), checker_model=check.get("model"),
                        vendors=store.vendors(), pos=store.pos(),
                        others=lambda vendor_id, number: store.others_like(inv["id"], inv["sha256"], vendor_id, number))


def process(store: Store, invoice_id: int) -> None:
    inv = store.invoice(invoice_id)
    try:
        opened = sandbox.open_invoice(store.file_path(inv), work_dir=store.dir)
    except sandbox.SandboxUnavailable as e:
        store.fail(WORKER, invoice_id, f"no sandbox could be started: {e}")
        return
    report = opened.report
    pages = store.pages_dir(invoice_id)
    pages.mkdir(parents=True, exist_ok=True)
    for name in report["images"]:
        shutil.move(opened.out_dir / name, pages / name)
    shutil.rmtree(opened.out_dir.parent, ignore_errors=True)
    store.log(WORKER, "opened", {"verdict": report["verdict"], "reasons": report["reasons"], "pages": len(report["images"]),
                                 "seconds": round(opened.seconds, 2), "sandbox": report["sandbox"]}, invoice_id)

    reading = check = None
    images = [pages / name for name in report["images"]]
    if images:
        try:
            first = read_invoice(images, READER_MODELS)
            reading = _reading(first)
            store.log(WORKER, "read", {k: reading[k] for k in ("model", "seconds", "usd")}, invoice_id)
            # The second reading must come from the other model family, or it is not a second opinion.
            second = read_invoice(images, [m for m in CHECKER_MODELS if m != first.model])
            check = _reading(second)
            store.log(WORKER, "read again", {k: check[k] for k in ("model", "seconds", "usd")}, invoice_id)
        except InferenceError as e:
            store.log(WORKER, "not read", {"why": str(e)[:500]}, invoice_id)

    inv = inv | {"airlock": report, "reading": reading, "check_reading": check}
    store.save_result(WORKER, invoice_id, airlock=report, reading=reading, check_reading=check, decision=recheck(store, inv))


def work(store: Store, poll_seconds: float = 2.0, once: bool = False) -> None:
    store.requeue_unfinished(WORKER)
    while True:
        invoice_id = store.claim_next()
        if invoice_id is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        try:
            process(store, invoice_id)
        except Exception as e:  # one bad invoice must not stop the queue; it is recorded as failed
            store.fail(WORKER, invoice_id, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m tally.pipeline <data_dir>")
    work(Store(Path(sys.argv[1])))
