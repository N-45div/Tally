"""How much one host can take: the same batch of invoices through the whole pipeline with 1, 2 and 4 workers.

    python -m lab.load <out_dir> [invoices] [workers ...]

Each run starts from an empty store, uploads `invoices` copies of the test invoice (each with its own number), and
lets that many workers work through the queue: a microVM per file, two readings, the fixed rules. It reports how
many invoices a minute went through, how long each took from upload to checked, and how long the first one took,
since a first microVM or a first model call can be slower than the rest. It writes load.json and load.txt.
"""
from __future__ import annotations

import json
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from tally import ledger
from tally.pipeline import work_in_parallel
from tally.store import Store
from tests.invoices import make


def _seconds(entry: dict) -> float:
    return datetime.fromisoformat(entry["at"]).timestamp()


def one_run(invoices: int, workers: int) -> dict:
    work = Path(tempfile.mkdtemp(prefix="tally-load-"))
    try:
        store = Store(work / "data")
        vid = store.add_vendor("load", "Northwind Office Supply Ltd", pay_to={"iban": "GB29 NWBK 6016 1331 9268 19"})
        store.add_po("load", "PO-55120", vid, "GBP", 10_000_000)
        ids = [store.add_invoice("load", f"{i}.pdf", make(work / f"{i}.pdf", invoice_number=f"LOAD-{workers}-{i}").read_bytes())
               for i in range(invoices)]
        started = time.monotonic()
        work_in_parallel(store.dir, workers=workers, once=True)
        wall = time.monotonic() - started
        latencies, sandbox, statuses = [], [], []
        for iid in ids:
            entries = ledger.entries(store.db, iid)
            up = next(e for e in entries if e["kind"] == "uploaded")
            done = next((e for e in entries if e["kind"] in ("checked", "failed")), None)
            if done:
                latencies.append(_seconds(done) - _seconds(up))
            opened = next((e["detail"] for e in entries if e["kind"] == "opened"), None)
            if opened:
                sandbox.append(opened["seconds"])
            statuses.append(store.invoice(iid)["status"])
        usd = sum(((inv["reading"] or {}).get("usd") or 0) + ((inv["check_reading"] or {}).get("usd") or 0)
                  for inv in (store.invoice(i) for i in ids))
        return {"workers": workers, "invoices": invoices, "wall_seconds": round(wall, 1),
                "per_minute": round(60 * invoices / wall, 1) if wall else None,
                "ready": statuses.count("ready"), "other": len(statuses) - statuses.count("ready"),
                "latency_median": statistics.median(latencies) if latencies else None,
                "latency_max": max(latencies) if latencies else None,
                "sandbox_first": sandbox[0] if sandbox else None,
                "sandbox_median": statistics.median(sandbox) if sandbox else None, "usd": round(usd, 4)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def report(res: dict) -> str:
    lines = [f"Load test, {res['at']}: {res['runs'][0]['invoices']} invoices per run, each opened in a microVM, read twice and checked.",
             "Upload-to-checked times are from the ledger, to the second.", "",
             f"{'workers':>7}  {'invoices/min':>12}  {'all done in':>11}  {'median wait':>11}  {'longest wait':>12}  "
             f"{'microVM, first':>14}  {'microVM, median':>15}  {'ready':>5}  {'cost':>8}"]
    for r in res["runs"]:
        lines.append(f"{r['workers']:>7}  {r['per_minute']:>12}  {r['wall_seconds']:>10}s  {r['latency_median']:>10}s  {r['latency_max']:>11}s  "
                     f"{r['sandbox_first']:>13}s  {r['sandbox_median']:>14}s  {r['ready']:>5}  ${r['usd']:>7.4f}")
    return "\n".join(lines) + "\n"


def run(out_dir: Path, invoices: int = 20, workers: tuple[int, ...] = (1, 2, 4)) -> dict:
    res = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "runs": [one_run(invoices, w) for w in workers]}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "load.json").write_text(json.dumps(res, indent=1))
    (out_dir / "load.txt").write_text(report(res))
    return res


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    args = [int(a) for a in sys.argv[2:]]
    r = run(Path(sys.argv[1]), *(args[:1] or [20]), **({"workers": tuple(args[1:])} if len(args) > 1 else {}))
    print(report(r))
