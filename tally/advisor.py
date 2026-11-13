"""The advisor: the worker that asks SERV Reasoning for judgement, and nothing more.

    python -m tally.advisor <data_dir>          plan the payment runs people ask for, and write a case file for each
                                                held invoice that needs one, until stopped
    python -m tally.advisor <data_dir> once     the same, once

It does two things, and both are only proposals: a plan for each payment run a person asks for
(tally.payruns), and a case file for each held invoice (tally.cases). Code checks every answer before it is saved,
and a person decides. The store offers this worker nothing that approves, rejects, releases or pays.

It holds the SERV key (OPENSERV_API_KEY), and only this worker does. Spending is capped per workspace and per day
(TALLY_REASONING_BUDGET_USD, $1 unless set): when today's case files and plans have cost that much, it waits for the
next day, and the pages say so.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from tally import cases, payruns
from tally.store import Store

BUDGET_USD_PER_DAY = float(os.environ.get("TALLY_REASONING_BUDGET_USD", "1.00"))


def over_budget(store: Store) -> bool:
    return store.spent_on_reasoning_today() >= BUDGET_USD_PER_DAY


def once(store: Store) -> int:
    """One pass: queued payment runs first (a person is waiting on the page), then held invoices. Returns calls made."""
    made = 0
    while (run := store.claim_payrun()) is not None:
        if over_budget(store):
            return made
        payruns.plan_run(store, run)
        print(f"payment run {run['id']}: {store.payrun(run['id'])['status']}", flush=True)
        made += 1
    for inv in store.invoices("held"):
        if over_budget(store):
            return made
        ev = cases.evidence(store, inv)
        if cases.needs_case(store, inv, ev):
            cid = cases.write_case(store, inv, ev)
            print(f"invoice {inv['id']}: case {cid}", flush=True)
            made += 1
    return made


def work(store: Store, poll_seconds: float = 3.0) -> None:
    while True:
        once(store)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    s = Store(Path(sys.argv[1]))
    once(s) if len(sys.argv) > 2 and sys.argv[2] == "once" else work(s)
