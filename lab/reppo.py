"""An outside grade for the advisor: Reppo's panel of evaluators scores SERV's case files and payment plans.

    python -m lab.reppo <out_dir>      build the synthetic cases, ask SERV, send each answer to Reppo, save the scores

Tally's decisions come from code, so this grades only what the advisor proposes: is each case file supported by
its evidence, is its risk sound, are its steps the right ones; does a payment plan follow the policy and the cash?
Each case is graded in a workspace of its own under three wordings of the case-file instructions: the first; the
second, which named the signals of invoice fraud after one live answer called an unannounced switch to a foreign
account "medium, likely a mismatch"; and the one written after this lab's first grading, which showed a summary
whose explanation disagreed with its own risk. So each change is measured, not only made.

Only synthetic records built here are sent: Reppo's evaluations are public, so nothing from a real workspace ever
goes to it. Needs OPENSERV_API_KEY and REPPO_API_KEY; about 2 cents of SERV and 440 Reppo credits per answer.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from tally import cases, payruns
from tally.normalize import to_cents
from tally.pipeline import recheck
from tally.store import Store

REPPO = "https://eval.reppo.xyz/api/v1/agents/evals"
# The risk guidance as first written (commit 96456ba's first draft), before live answers showed it too lenient.
FIRST_RISK = (
    "risk: high when the evidence points to fraud or to paying the wrong party (a changed account on a vendor paid "
    "before, a name that imitates a vendor, hidden or active content in the file, repeated attempts); medium when "
    "money could be paid twice or wrongly but an honest mistake is likely; low when it is paperwork (a missing "
    "purchase order, readings to compare).\n")
# A third wording, tried after the first grading (Nov 13): Reppo gave one sums case file 53 to 56, and its summary
# called the likely cause an honest error while its risk was high, so this asked the explanation to agree with the
# risk. Graded on isolated cases it did no better (96.2 against 100), and the low score turned out to come from the
# lab itself (every held invoice shared one vendor, so each saw the others' holds), so it was not kept.
SUMMARY_BEFORE = "summary: two or three plain sentences: what is wrong, and what most likely explains it.\n"
SUMMARY_TRIED = ("summary: two or three plain sentences: what is wrong, and what most likely explains it. The explanation must "
                 "agree with the risk you give: a high risk names what points to fraud or to the wrong payee.\n")
END_BEFORE, END_TRIED = "choose the cautious step.", "choose the cautious step. Write no more than is asked for."
assert SUMMARY_BEFORE in cases.SYSTEM and END_BEFORE in cases.SYSTEM, "tally.cases.SYSTEM changed: update the wordings here"
SECOND_SYSTEM = cases.SYSTEM
FIRST_SYSTEM = (SECOND_SYSTEM[:SECOND_SYSTEM.index("risk: high")] + FIRST_RISK
                + SECOND_SYSTEM[SECOND_SYSTEM.index("findings: up to five"):])
TRIED_SYSTEM = SECOND_SYSTEM.replace(SUMMARY_BEFORE, SUMMARY_TRIED).replace(END_BEFORE, END_TRIED)
WORDINGS = {"first wording": FIRST_SYSTEM, "second wording (in use)": SECOND_SYSTEM, "tried after Reppo's feedback": TRIED_SYSTEM}
GRADE = ("Grade the AI response against the instructions and the evidence in the prompt. Check that every finding is "
         "supported by the evidence it cites, that the risk level follows the stated rules, and that the next steps are "
         "the right ones for these holds and are taken only from the allowed actions.")
PLAN_GRADE = ("Grade the AI response against the instructions in the prompt. Check that the plan follows each rule of the "
              "team's policy, stays within the cash available, uses only the invoices listed, and gives sound reasons.")
POLICY = ("Keep 5,000 back for payroll. Rent and utilities are never late. Pay small suppliers (under 1,000) before larger "
          "ones. Anything over 8,000 waits for the end-of-month run unless it is overdue.")

BLANK = {"account_name": "", "iban": "", "account_number": "", "sort_code": "", "routing_number": "", "swift_bic": ""}
CLEAR = {"verdict": "clear", "reasons": [], "images": [], "hidden": [], "active": [], "problems": [], "pages": 1, "bytes": 0,
         "sandbox": {"backend": "none: a record written by lab.reppo, no file opened", "network": "none"}}
VENDORS = {
    "Northwind Office Supply Ltd": ("GB29 NWBK 6016 1331 9268 19", "+44 117 496 0000"),
    "Thames Water Utilities Ltd": ("GB82 WEST 1234 5698 7654 32", "+44 345 737 2200"),
    "Greystone Properties Ltd": ("GB33 BUKB 2020 1555 5555 55", "+44 20 7946 0123"),
    "Bright Sparks Electrical": ("GB94 BARC 1020 1530 0934 59", "+44 117 496 0456"),
    "Acme Cloud Hosting Ltd": ("GB71 NWBK 6070 6012 3456 78", "+44 20 7946 0999"),
}


def _record(s: Store, vendor: str, number: str, total: float, *, due: str = "2026-11-27", po: str | None = None,
            iban: str = "", airlock: dict | None = None, subtotal: float | None = None, tax: float | None = None, approve: bool = False) -> int:
    f = {"vendor_name": vendor, "vendor_address": "", "bill_to": "Contoso Clinics", "invoice_number": number, "invoice_date": "2026-10-28",
         "due_date": due, "po_number": po if po is not None else f"PO-{list(VENDORS).index(vendor) if vendor in VENDORS else 0}",
         "currency": "GBP", "subtotal": subtotal, "tax": tax, "total": total, "pay_to": copy.deepcopy(BLANK) | ({"iban": iban} if iban else {}),
         "lines": []}
    iid = s.add_invoice("lab", f"{number}.pdf", f"%PDF-1.7 lab.reppo {number}".encode())
    s.claim_next()
    air = airlock or CLEAR
    r, c = {"fields": f, "model": "glm-5.3-flash"}, {"fields": f, "model": "qwen3.8-flash-next"}
    inv = s.invoice(iid) | {"airlock": air, "reading": r, "check_reading": c}
    s.save_result("lab.reppo", iid, airlock=air, reading=r, check_reading=c, decision=recheck(s, inv))
    if approve:
        s.decide("lab", iid, True, "", lambda inv: recheck(s, inv))
    return iid


# Six held invoices, each graded in a workspace of its own, so one case's evidence never includes another's hold.
HELD = {
    "new account abroad, 3.7 times the usual amount": ("Northwind Office Supply Ltd", "NW-3201", 4875.00, {"iban": "DE89 3704 0044 0532 0130 00"}),
    "a name one letter off a vendor on file": ("Northwlnd Office Supply Ltd", "NW-3202", 1260.00, {}),
    "no purchase order from a regular utility": ("Thames Water Utilities Ltd", "TW-8850", 318.40, {"po": ""}),
    "a copy of an approved invoice": ("Northwind Office Supply Ltd", "NW-3102", 1268.40, {}),
    "amounts that do not add up": ("Northwind Office Supply Ltd", "NW-3203", 1500.00, {"subtotal": 1200.00, "tax": 240.00}),
    "hidden text found when the file was opened": ("Northwind Office Supply Ltd", "NW-3204", 1290.00,
                                                   {"airlock": CLEAR | {"verdict": "hold", "reasons": ["hidden text (white on white) on page 1"]}}),
}


def held_workspace(d: Path, case: str) -> tuple[Store, int]:
    """Two suppliers with history (six and three approved invoices), and one invoice held for the reason named."""
    s = Store(d)
    for i, (name, (iban, phone)) in enumerate(VENDORS.items()):
        vid = s.add_vendor("lab", name, pay_to={"iban": iban}, phone=phone)
        s.add_po("lab", f"PO-{i}", vid, "GBP", to_cents("100000"))
    for i, total in enumerate((1180.00, 1356.20, 1268.40, 1302.10, 1240.00, 1399.90)):
        _record(s, "Northwind Office Supply Ltd", f"NW-{3100 + i}", total, approve=True)
    for i in range(3):
        _record(s, "Thames Water Utilities Ltd", f"TW-{8840 + i}", 310.00 + i, approve=True)
    vendor, number, total, extra = HELD[case]
    return s, _record(s, vendor, number, total, **extra)


def payrun_workspace(d: Path) -> tuple[Store, dict]:
    s = Store(d)
    for i, (name, (iban, phone)) in enumerate(VENDORS.items()):
        vid = s.add_vendor("lab", name, pay_to={"iban": iban}, phone=phone)
        s.add_po("lab", f"PO-{i}", vid, "GBP", to_cents("100000"))
    for vendor, number, total, due in [("Greystone Properties Ltd", "GP-11", 4200.00, "2026-11-21"), ("Thames Water Utilities Ltd", "TW-1", 318.40, "2026-11-18"),
                                       ("Northwind Office Supply Ltd", "NW-1", 1492.80, "2026-11-27"), ("Bright Sparks Electrical", "BS-1", 640.00, "2026-11-19"),
                                       ("Bright Sparks Electrical", "BS-2", 385.50, "2026-11-10"), ("Acme Cloud Hosting Ltd", "AC-1", 9600.00, "2026-12-03"),
                                       ("Northwind Office Supply Ltd", "NW-2", 2310.00, "2026-12-06")]:
        _record(s, vendor, number, total, due=due, approve=True)
    rid = s.add_payrun("lab", POLICY, "GBP", to_cents("13500"), "2026-11-20")
    return s, s.payrun(rid)


def submit(text: str) -> str:
    req = urllib.request.Request(REPPO, data=json.dumps({"taskType": "response", "input": text}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['REPPO_API_KEY']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["id"]


def result(eval_id: str, wait: float = 900) -> dict:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        req = urllib.request.Request(f"{REPPO}/{eval_id}", headers={"Authorization": f"Bearer {os.environ['REPPO_API_KEY']}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
        except urllib.error.URLError:
            d = {"status": "PROCESSING"}
        if d.get("status") in ("COMPLETED", "FAILED"):
            return d
        time.sleep(5)
    return {"status": "TIMED OUT", "id": eval_id}


def shown_case(case: dict) -> dict:
    """The case file as the person sees it, after code's checks."""
    cf = case["casefile"]
    return {"summary": cf["summary"], "risk": cf["risk"], "findings": cf["findings"],
            "next_steps": [{"action": s["action"], "why": s["why"]} for s in cf["next_steps"]]}


def run(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    jobs = []
    with tempfile.TemporaryDirectory() as tmp:
        for v, (version, system) in enumerate(WORDINGS.items()):
            for c, name in enumerate(HELD):
                s, iid = held_workspace(Path(tmp) / f"w{v}-c{c}", name)
                inv = s.invoice(iid)
                ev = cases.evidence(s, inv)
                cases.write_case(s, inv, ev, system=system)
                case = s.cases_for(iid)[0]
                s.db.close()
                if case["status"] != "written":
                    jobs.append({"kind": "case", "version": version, "case": name, "error": case["detail"]})
                    continue
                prompt = f"{system}\n\n{cases.request_text(ev)}"
                answer = shown_case(case)
                text = f"{GRADE}\n\nUser prompt:\n{prompt}\n\nAI response:\n{json.dumps(answer, indent=1)}"
                jobs.append({"kind": "case", "version": version, "case": name, "held_by": ev["held_by"], "risk": answer["risk"],
                             "steps": [x["action"] for x in answer["next_steps"]], "answer": answer, "serv": case["detail"], "eval": submit(text)})
                print(f"{version} / {name}: {answer['risk']}, sent to Reppo", flush=True)
        s, prun = payrun_workspace(Path(tmp) / "payrun")
        payruns.plan_run(s, prun)
        prun = s.payrun(prun["id"])
        if prun["status"] == "planned":
            plan = prun["plan"]
            shown = {"policy_reading": plan["policy_reading"], "pay": [{"invoice": p["invoice"], "why": p["why"]} for p in plan["pay"]],
                     "defer": [{"invoice": p["invoice"], "why": p["why"]} for p in plan["defer"]], "summary": plan["summary"]}
            cands = [{k: c[k] for k in ("invoice", "vendor", "amount", "invoice_date", "due_date", "days_until_due", "approved_on")}
                     for c in payruns.candidates(s, prun)]  # the run is not approved, so these are exactly what SERV was shown
            request = json.dumps({"policy": prun["policy"], "pay_date": prun["pay_date"], "cash_available": "13,500.00", "invoices": cands}, indent=1)
            text = f"{PLAN_GRADE}\n\nUser prompt:\n{payruns.SYSTEM}\n\n{request}\n\nAI response:\n{json.dumps(shown, indent=1)}"
            jobs.append({"kind": "payment run", "version": "payment plan", "case": "policy, 13,500 cash, seven approved invoices",
                         "pay": [p["invoice"] for p in plan["pay"]], "total_cents": plan["total_cents"], "answer": shown,
                         "serv": prun["detail"], "eval": submit(text)})
            print("payment run: planned, sent to Reppo", flush=True)
        s.db.close()
    for j in jobs:
        if "eval" in j:
            d = result(j["eval"])
            r = d.get("result") or {}
            j["reppo"] = {"status": d.get("status"), "score": r.get("score"), "route": r.get("route"), "decision": r.get("decision"),
                          "confidence": r.get("confidence"), "reason": r.get("reason"), "rubric": r.get("rubric_scores"),
                          "credits": d.get("creditsCharged")}
            print(f"{j['version']} / {j['case']}: Reppo {j['reppo']['score']} {j['reppo']['route']}", flush=True)
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "jobs": jobs}
    for version in WORDINGS:
        scores = [j["reppo"]["score"] for j in jobs if j["kind"] == "case" and j["version"] == version and (j.get("reppo") or {}).get("score") is not None]
        report[version] = {"cases": len(scores), "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
                           "ship": sum(1 for j in jobs if j["kind"] == "case" and j["version"] == version and (j.get("reppo") or {}).get("route") == "ship")}
    (out / "reppo.json").write_text(json.dumps(report, indent=1))
    return report


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    r = run(Path(sys.argv[1]))
    print(json.dumps({k: v for k, v in r.items() if k != "jobs"}, indent=1))
