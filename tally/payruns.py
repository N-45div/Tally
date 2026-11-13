"""Payment runs: SERV Reasoning reads the team's policy and proposes what to pay; code checks it; a person approves.

A small finance team pays its approved invoices in runs: on Friday, with the cash it has, in an order that follows
rules it often only states in words ("keep the utilities current", "pay the small suppliers first", "hold anything
over 10,000 to the end of the month"). A person writes that policy when asking for a run, with the cash available and
the pay date. A model, through SERV Reasoning, reads the policy against the approved invoices and proposes which to
pay now and which to leave for a later run, saying how it read each rule.

The model chooses among invoices only. Every invoice it may choose has passed every check and been approved by a
person. It never gives an amount or a payee: code fills both from the records (the amount approved, the bank details
on file). Code drops an id that is not on the list, trims the plan to the cash in the model's own order of priority,
flags any invoice the plan leaves overdue, and shows its own plan (due soonest first) beside the model's. A person
approves the exact plan they saw, by its hash, and the plan is rebuilt from the records inside that transaction: if a
vendor's bank details, an invoice or the cash changed since, the run is refused.

Tally then writes a payment file for the bank from the bank details on file. It records payments; it does not move
money: a person sends that file to the bank.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date

from tally.cases import _agreed_date
from tally.normalize import money
from tally.serv import InferenceError, reason_json
from tally.store import Store

WORKER = "tally advisor"
PLAN_MODELS = ["gpt-5.4-mini", "gemini-2.5-flash"]
SOON_DAYS = 7  # an invoice due within this many days of the pay date, if deferred, is flagged

SYSTEM = (
    "You plan one payment run for a small finance team. Every invoice you are given has already passed the team's "
    "checks and been approved by a person; you decide only which of them to pay on this run and which to leave for a "
    "later one.\n"
    "Follow the team's policy, written in their own words. Stay within the cash available. Where the policy does not "
    "settle a choice, pay what is due soonest first and avoid letting an invoice become overdue, and say what you "
    "assumed. Vendors are named as they are on file; judge from the name what kind of supplier each is when the "
    "policy needs it (a utility, a landlord, a small supplier), and say so.\n"
    "Write:\n"
    "policy_reading: each rule in the policy in a few words, and how you applied it, or why it did not apply.\n"
    "pay: the invoices to pay now, most important first, each with a one-sentence reason.\n"
    "defer: every other invoice given, each with a one-sentence reason.\n"
    "summary: two plain sentences.\n"
    "Use only invoice ids from the list. You give no amounts and no bank details: code takes both from the records, "
    "checks your plan against them and the cash, and a person approves it."
)
_reasoned = {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                        "properties": {"invoice": {"type": "integer"}, "why": {"type": "string"}},
                                        "required": ["invoice", "why"]}}
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "policy_reading": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                      "properties": {"rule": {"type": "string"}, "applied": {"type": "string"}},
                                                      "required": ["rule", "applied"]}},
        "pay": _reasoned, "defer": _reasoned, "summary": {"type": "string"},
    },
    "required": ["policy_reading", "pay", "defer", "summary"],
}


def _days(a: str | None, b: str) -> int | None:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days if a else None


def candidates(store: Store, run: dict) -> list[dict]:
    """The approved, unpaid invoices this run may pay, from the records: vendor and bank details on file, amount approved."""
    out = []
    for inv in store.payable(run["currency"]):
        vendor = store.vendor(inv["vendor_id"])
        due = _agreed_date(inv, "due_date")
        out.append({"invoice": inv["id"], "vendor": vendor["name"], "vendor_id": vendor["id"], "amount_cents": inv["total_cents"],
                    "amount": money(inv["total_cents"]), "invoice_date": _agreed_date(inv, "invoice_date"), "due_date": due,
                    "days_until_due": _days(due, run["pay_date"]), "approved_on": (inv["decided_at"] or "")[:10],
                    "reference": (inv["invoice_number_key"] or "")[:18], "pay_to": vendor["bank"]})
    return out


def baseline(cands: list[dict], cash_cents: int) -> list[int]:
    """Code's own plan, to compare with: due soonest first (no due date last), as many as the cash covers."""
    picked, left = [], cash_cents
    for c in sorted(cands, key=lambda c: (c["due_date"] is None, c["due_date"] or "", c["invoice"])):
        if c["amount_cents"] <= left:
            picked.append(c["invoice"])
            left -= c["amount_cents"]
    return picked


def plan_hash(run: dict, pay: list[dict]) -> str:
    """What a person approves: this run, its date and cash, and each payment's invoice, payee on file and amount."""
    body = {"payrun": run["id"], "currency": run["currency"], "pay_date": run["pay_date"], "cash_cents": run["cash_cents"],
            "pay": [[p["invoice"], p["vendor_id"], p["amount_cents"], p["pay_to"]] for p in pay]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_plan(proposal: dict, cands: list[dict], run: dict) -> tuple[dict, dict]:
    """Return (plan, what code changed). Payments carry amounts and payees from the records, never from the model."""
    by_id = {c["invoice"]: c for c in cands}
    changed = {"unknown_ids": [], "over_cash": [], "flags": []}
    pay, total = [], 0
    for p in proposal.get("pay", []):
        c = by_id.get(p.get("invoice"))
        if c is None or c["invoice"] in [x["invoice"] for x in pay]:
            changed["unknown_ids"].append(p.get("invoice"))
            continue
        if total + c["amount_cents"] > run["cash_cents"]:
            changed["over_cash"].append(c["invoice"])
            continue
        total += c["amount_cents"]
        pay.append(c | {"why": str(p.get("why", "")).strip()[:200]})
    why_deferred = {d.get("invoice"): str(d.get("why", "")).strip()[:200] for d in proposal.get("defer", [])}
    defer = []
    for c in cands:
        if c["invoice"] in [x["invoice"] for x in pay]:
            continue
        why = ("Code took it out: the plan went over the cash." if c["invoice"] in changed["over_cash"]
               else why_deferred.get(c["invoice"]) or "The plan did not choose it.")
        flag = None
        if c["days_until_due"] is not None and c["days_until_due"] < 0:
            flag = f"overdue by {-c['days_until_due']} day(s) on the pay date"
        elif c["days_until_due"] is not None and c["days_until_due"] <= SOON_DAYS:
            flag = f"due {c['days_until_due']} day(s) after the pay date"
        if flag:
            changed["flags"].append({"invoice": c["invoice"], "flag": flag})
        defer.append(c | {"why": why, "flag": flag})
    plan = {"summary": str(proposal.get("summary", "")).strip()[:400],
            "policy_reading": [{"rule": str(r.get("rule", ""))[:160], "applied": str(r.get("applied", ""))[:240]}
                               for r in proposal.get("policy_reading", [])[:8]],
            "pay": pay, "defer": defer, "total_cents": total, "cash_left_cents": run["cash_cents"] - total,
            "baseline": baseline(cands, run["cash_cents"])}
    return plan, changed


def rebuild(store: Store, run: dict) -> tuple[dict, str]:
    """The plan's payments rebuilt from the records as they are now, for approval: same invoices, amounts and payees
    on file today. Anything that changed makes a different hash, and the store refuses the run."""
    cands = {c["invoice"]: c for c in candidates(store, run)}
    pay = []
    for p in run["plan"]["pay"]:
        c = cands.get(p["invoice"])
        pay.append(c if c is not None else p | {"amount_cents": -1})  # gone from the payable list: cannot match
    if sum(p["amount_cents"] for p in pay) > run["cash_cents"]:
        return {"pay": pay}, "over the cash"
    return {"pay": pay}, plan_hash(run, pay)


def plan_run(store: Store, run: dict, models: list[str] | None = None) -> None:
    """Ask SERV for a plan for one queued run, check it, and save it with its ledger entry."""
    cands = candidates(store, run)
    if not cands:
        store.save_payrun_plan(WORKER, run["id"], plan=None, plan_hash=None,
                               detail={"why": f"no approved invoice in {run['currency']} is waiting to be paid"})
        return
    shown = [{k: c[k] for k in ("invoice", "vendor", "amount", "invoice_date", "due_date", "days_until_due", "approved_on")} for c in cands]
    request = json.dumps({"policy": run["policy"], "pay_date": run["pay_date"], "currency": run["currency"],
                          "cash_available": money(run["cash_cents"]), "invoices": shown}, indent=1)
    try:
        a = reason_json(SYSTEM, request, PLAN_SCHEMA, models or PLAN_MODELS, name="payment_run", effort="low", max_tokens=2000)
    except InferenceError as e:
        store.save_payrun_plan(WORKER, run["id"], plan=None, plan_hash=None, detail={"why": str(e)[:300]})
        return
    plan, changed = check_plan(a.data, cands, run)
    detail = {"model": a.model, "serv_request_id": a.request_id, "seconds": round(a.seconds, 2), "usd": round(a.usd, 6),
              "tokens": [a.input_tokens, a.output_tokens], "pay": [p["invoice"] for p in plan["pay"]],
              "total_cents": plan["total_cents"], "changed": changed}
    store.save_payrun_plan(WORKER, run["id"], plan=plan, plan_hash=plan_hash(run, plan["pay"]), detail=detail)


def payment_file(store: Store, run: dict) -> str:
    """A bulk-payment CSV for the bank, from the payments as approved: payee and account on file, amount, reference."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["payee", "iban", "account_number", "sort_code", "routing_number", "amount", "currency", "reference", "payment_date"])

    def cell(v) -> str:  # a cell a spreadsheet would run as a formula is quoted
        v = str(v or "")
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v

    for p in store.payments(run["id"]):
        inv = store.invoice(p["invoice_id"])
        b = p["pay_to"]
        w.writerow([cell(p["vendor"]), b.get("iban", ""), b.get("account_number", ""), b.get("sort_code", ""), b.get("routing_number", ""),
                    f"{p['amount_cents'] // 100}.{p['amount_cents'] % 100:02d}", p["currency"],
                    cell((inv["invoice_number_key"] or f"TALLY{inv['id']}")[:18]), p["pay_date"]])
    return out.getvalue()
