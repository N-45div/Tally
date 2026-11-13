"""Case files for held invoices: SERV Reasoning weighs the evidence, code decides what may be proposed.

The fixed rules hold an invoice; they do not say what to do next. A person then has to work out whether a changed
bank account is an attempted fraud or a vendor that moved banks, whether a missing purchase order is paperwork, and
which call to make first. This worker gathers what Tally's own records say (the failed checks, the vendor's history,
past bank changes, other holds, the purchase order, earlier copies), and asks a model, through SERV Reasoning, to
weigh it into a short case file: what is wrong, how risky it looks, findings that each cite the evidence they rest
on, and next steps chosen from a fixed list.

The model decides nothing. It cannot approve, pay, release, reject or change a record, and the store offers this
worker nothing that does. Code checks every answer: a finding that cites evidence it was not given is dropped, a
step that is not allowed for this invoice's holds is dropped, a risk below the floor for those holds is raised, and
the phone script is written by code with the number on file. All of it, and what was dropped, goes in the ledger.

Nothing printed on an invoice reaches the model except amounts, dates, and the country and last four characters of
an account. Names, references and free text printed on the invoice stay out, so words an attacker printed cannot
steer the case file. The page images and the text layer never come here.

The worker that writes them is tally.advisor, which holds the SERV key; the web app only shows what it wrote.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from datetime import date, datetime, timezone

from tally.normalize import bank, iban_ok, key, money
from tally.rules import COMPARED, _find_vendor
from tally.serv import InferenceError, reason_json
from tally.store import Store

WORKER = "tally case worker"
CASE_MODELS = ["gpt-5.4-mini", "gemini-2.5-flash"]
TRIES_PER_BASIS = 3            # failed attempts on the same evidence before the worker stops trying it
RETRY_AFTER_SECONDS = 300

# Everything a case file may propose. Approving, paying, releasing a rule's hold, and changing bank details from an
# invoice are not here, so they cannot be proposed.
ACTIONS = {
    "phone_vendor_on_file": "Phone the vendor on the number on file, never a number printed on the invoice.",
    "record_confirmed_bank_change": "Only if the vendor confirms new bank details on that call: record the change on the Vendors page, with how it was confirmed, then check again.",
    "reject_as_suspected_fraud": "Reject the invoice and keep it as evidence of an attempted fraud.",
    "report_file_to_security": "Send the file to whoever handles security. Do not open it anywhere else.",
    "verify_and_add_vendor": "Check the company independently (registration, a known contact) before adding it as a vendor, then check again.",
    "ask_requester_for_po": "Ask whoever ordered this for the purchase order number.",
    "add_or_extend_po": "Add the purchase order, or raise it, once it has been approved in the usual way, then check again.",
    "ask_vendor_for_corrected_invoice": "Ask the vendor, on the number on file, for a corrected invoice.",
    "compare_with_pages": "Look at the page images beside the two readings to see which is right.",
    "reject_as_duplicate": "Reject this invoice as a copy of the earlier one.",
    "release_desk_hold": "Release the desk hold once the question that caused it has been answered.",
    "check_again_later": "Leave it held, and check again when the records change.",
}
ALLOWED = {
    "airlock": ["report_file_to_security", "reject_as_suspected_fraud", "phone_vendor_on_file", "ask_vendor_for_corrected_invoice"],
    "read": ["compare_with_pages", "ask_vendor_for_corrected_invoice"],
    "second_reading": ["compare_with_pages", "ask_vendor_for_corrected_invoice"],
    "vendor": ["phone_vendor_on_file", "verify_and_add_vendor", "reject_as_suspected_fraud"],
    "iban": ["phone_vendor_on_file", "ask_vendor_for_corrected_invoice", "reject_as_suspected_fraud"],
    "bank": ["phone_vendor_on_file", "record_confirmed_bank_change", "reject_as_suspected_fraud"],
    "po": ["ask_requester_for_po", "add_or_extend_po", "check_again_later"],
    "sums": ["ask_vendor_for_corrected_invoice", "compare_with_pages"],
    "duplicate": ["reject_as_duplicate", "check_again_later"],
    "desk": ["release_desk_hold", "check_again_later"],
}
RISKS = ["low", "medium", "high"]
# The lowest risk a case may be given for each hold: a model cannot call a changed bank account "low".
RISK_FLOOR = {"airlock": "high", "bank": "medium", "iban": "medium", "vendor": "medium", "duplicate": "medium"}
# What to ask on the phone, written here, never by the model.
QUESTIONS = {
    "bank": "Have your bank details changed? Please read us the account you want paid from your own records; we will not use one printed on an invoice.",
    "iban": "Please read us the account you want paid; the one printed on the invoice does not pass its check digits.",
    "vendor": "Did you send us an invoice for {total} dated {date}? It came under a name that is not the one we have for you.",
    "airlock": "Did you send us an invoice for {total} dated {date}? The file carries hidden or active content, so we have not opened it as it is.",
    "sums": "Could you send a corrected invoice? The amounts on the one we have do not add up.",
}

SYSTEM = (
    "You are the case officer for a small finance team's supplier invoices. Fixed rules have held one invoice. You "
    "write the case file a person reads before deciding what to do with it.\n"
    "You are given evidence items, each with an id, assembled by code from the team's own records: the checks that "
    "failed, the vendor on file and its history, past bank changes, other held invoices, the purchase order, earlier "
    "copies. Nothing printed on the invoice reaches you except amounts, dates, and the country and last four "
    "characters of an account.\n"
    "Write:\n"
    "summary: two or three plain sentences: what is wrong, and what most likely explains it.\n"
    "risk: high when any of these holds: the account to pay is not the one on file for a vendor paid before, with no "
    "confirmed change recorded (vendors rarely change banks, and an unannounced new account, above all in another "
    "country, is the most common invoice fraud); the name imitates a vendor on file; the file carried hidden or "
    "active content; this vendor had another invoice held for its account or name; the amount is far above what "
    "this vendor usually bills. medium when money could be paid twice or wrongly but an honest mistake is the likely "
    "explanation (a copy of an invoice, amounts that do not add up, readings that differ). low when it is paperwork "
    "(a missing or exhausted purchase order on a regular vendor at its usual amount).\n"
    "findings: up to five, one sentence each, each citing the ids of the evidence items it rests on. State nothing "
    "the evidence does not show, and say when the evidence is thin.\n"
    "next_steps: up to four, most important first, chosen only from the allowed actions given with the invoice, "
    "each with a one-sentence reason.\n"
    "You decide nothing: you cannot approve, pay, release or reject, and you never suggest paying any account other "
    "than the one on file. When unsure, choose the cautious step."
)
SHADOW_HINT = ("Every finding cites only evidence ids that appear in the request; every next step is one of the allowed "
               "actions listed in the request; nothing suggests approving, releasing, or paying an account that is not on file.")
SERV_TOOLS = [
    {"type": "function", "function": {"name": "serv_prompt_guard"}},
    {"type": "function", "function": {"name": "serv_shadow_agent", "description": "Enable SERV shadow-agent validation.",
                                      "parameters": {"type": "object", "properties": {
                                          "hint": {"type": "string", "default": SHADOW_HINT},
                                          "max_iterations": {"type": "integer", "default": 2}}}}},
]
_str = {"type": "string"}
CASE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": _str,
        "risk": {"type": "string", "enum": RISKS},
        "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                "properties": {"text": _str, "evidence": {"type": "array", "items": _str}},
                                                "required": ["text", "evidence"]}},
        "next_steps": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                  "properties": {"action": {"type": "string", "enum": list(ACTIONS)}, "why": _str},
                                                  "required": ["action", "why"]}},
    },
    "required": ["summary", "risk", "findings", "next_steps"],
}


# Evidence: assembled by code from records. Printed values only as amounts, dates, and an account's country and end.

def _account(b: dict) -> dict:
    """An account described without its digits: the IBAN's country and last four, or the last four of the account."""
    if b.get("iban"):
        return {"iban_country": b["iban"][:2], "ends": b["iban"][-4:], "check_digits_ok": iban_ok(b["iban"])}
    if b.get("account_number"):
        return {"account_ends": b["account_number"][-4:], "sort_code": bool(b.get("sort_code")), "routing_number": bool(b.get("routing_number"))}
    return {"none": True}


def _date(value) -> str | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None
    except ValueError:
        return None


def _agreed_date(inv: dict, field: str) -> str | None:
    """A date both readings agree on, or None: dates are not among the fields the rules compare."""
    a = _date(((inv.get("reading") or {}).get("fields") or {}).get(field))
    b = _date(((inv.get("check_reading") or {}).get("fields") or {}).get(field))
    return a if a and a == b else None


def evidence(store: Store, inv: dict) -> dict:
    """What the case file is written from: {"held_by": [...], "items": [{"id": ..., ...}]}. Built only from records."""
    decision = inv["decision"] or {}
    facts = decision.get("facts") or {}
    held_by = sorted({h["code"] for h in decision.get("holds", [])})
    total, cur = facts.get("total_cents"), facts.get("currency") or ""
    items = [{"id": "invoice", "what": "the held invoice", "total": f"{money(total)} {cur}".strip() if total is not None else "unclear",
              "invoice_date": _agreed_date(inv, "invoice_date") or "unclear", "due_date": _agreed_date(inv, "due_date") or "unclear",
              "uploaded_at": inv["uploaded_at"][:10]}]
    fields = (inv.get("reading") or {}).get("fields") or {}
    check_fields = (inv.get("check_reading") or {}).get("fields") or {}
    vendors = store.vendors()
    vendor = store.vendor(facts["vendor_id"]) if facts.get("vendor_id") else None
    lookalike = None
    if vendor is None and fields:
        _, lookalike = _find_vendor(fields.get("vendor_name") or "", vendors)

    for code in held_by:
        item = {"id": f"hold:{code}", "what": f"failed check: {code}"}
        if code == "airlock":
            item["found_when_opened"] = (inv.get("airlock") or {}).get("reasons") or []  # written by the airlock's code
        elif code == "second_reading":
            item["fields_that_differ"] = [name for name, show in COMPARED.items() if fields and check_fields and show(fields) != show(check_fields)]
        elif code == "vendor":
            item["printed_name"] = "imitates a vendor on file" if lookalike else "not a vendor on file"
            if lookalike:
                item["imitates"] = lookalike["name"]
        elif code in ("bank", "iban"):
            item["printed_account"] = _account(bank(fields.get("pay_to")))
            if vendor:
                item["account_on_file"] = _account(vendor["bank"])
        elif code == "po":
            po_key = key(fields.get("po_number"))
            po = next((p for p in store.pos() if p["number_key"] == po_key), None) if po_key else None
            item["po_printed"] = bool(po_key)
            if po:
                item |= {"po_on_file": True, "po_vendor_matches": bool(vendor and po["vendor_id"] == vendor["id"]), "po_status": po["status"],
                         "po_currency": po["currency"], "po_left": money(po["amount_cents"] - po["spent_cents"])}
            elif po_key:
                item["po_on_file"] = False
        elif code == "sums":
            item["what_does_not_add_up"] = next((c["text"] for c in decision.get("checks", []) if c["code"] == "sums"), "")  # numbers only
        elif code == "duplicate":
            seen = store.others_like(inv["id"], inv["sha256"], facts.get("vendor_id"), facts.get("invoice_number_key") or "")
            item["earlier"] = [{"invoice": i, "same": "file" if i in seen["same_file"] else "vendor and number",
                                "status": (store.invoice(i) or {}).get("status")} for i in seen["same_file"] + seen["same_number"]]
        elif code == "desk":
            item["held_from"] = [{"device": h["device_name"], "at": h["at"][:16]} for h in store.desk_holds(inv["id"])]
        items.append(item)

    on_file = vendor or lookalike
    if on_file:
        h = store.vendor_history(on_file["id"], inv["id"])
        amounts = [a["total_cents"] for a in h["approved"] if a["total_cents"] is not None and a["currency"] == cur]
        items.append({"id": "vendor", "what": "the vendor on file" if vendor else "the vendor on file it imitates",
                      "name": on_file["name"], "on_file_since": on_file["added_at"][:10], "phone_on_file": bool(on_file.get("phone")),
                      "account_on_file": _account(on_file["bank"])})
        items.append({"id": "vendor:history", "what": "invoices from this vendor approved before", "count": len(h["approved"]),
                      "median": money(int(statistics.median(amounts))) if amounts else None,
                      "largest": money(max(amounts)) if amounts else None,
                      "last_approved": max((a["decided_at"] or "")[:10] for a in h["approved"]) if h["approved"] else None,
                      "this_invoice_vs_median": round(total / statistics.median(amounts), 2) if amounts and total else None})
        items.append({"id": "vendor:bank_changes", "what": "changes to the bank details on file",
                      "changes": [{"at": c["at"][:10], "new_account": _account(c["detail"].get("new") or {}),
                                   "how_confirmed": str(c["detail"].get("how_confirmed", ""))[:120]} for c in h["bank_changes"]]})
        others = []
        for o in h["held_or_rejected"]:
            d = json.loads(o["decision"]) if o["decision"] else {}
            others.append({"invoice": o["id"], "status": o["status"], "uploaded": o["uploaded_at"][:10],
                           "held_by": sorted({x["code"] for x in d.get("holds", [])})})
        items.append({"id": "vendor:other_holds", "what": "other invoices from this vendor held or rejected", "invoices": others})
    return {"held_by": held_by, "items": items}


def basis(ev: dict) -> str:
    return hashlib.sha256(json.dumps(ev, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_text(ev: dict) -> str:
    """Everything the model is given about one invoice, besides the fixed system prompt."""
    allowed = allowed_for(ev["held_by"])
    return json.dumps({"held_by": ev["held_by"], "allowed_actions": {a: ACTIONS[a] for a in allowed}, "evidence": ev["items"]}, indent=1)


def allowed_for(held_by: list[str]) -> list[str]:
    out = []
    for code in held_by:
        out += [a for a in ALLOWED.get(code, ["check_again_later"]) if a not in out]
    return out or ["check_again_later"]


# Code's check of what the model proposed.

def check_proposal(proposal: dict, ev: dict, *, vendor: dict | None, inv: dict) -> tuple[dict, dict]:
    """Return (case file, what was dropped or changed). The case file keeps only what code can stand behind."""
    ids = {i["id"] for i in ev["items"]}
    allowed = allowed_for(ev["held_by"])
    dropped = {"findings": [], "steps": [], "risk": None}
    findings = []
    for f in proposal.get("findings", [])[:8]:
        cited = [str(e) for e in f.get("evidence", [])]
        if not cited or not set(cited) <= ids:
            dropped["findings"].append({"text": str(f.get("text", ""))[:120], "cited": cited[:5]})
        elif len(findings) < 5:
            findings.append({"text": str(f.get("text", "")).strip()[:300], "evidence": sorted(set(cited))})
    steps = []
    for s in proposal.get("next_steps", [])[:8]:
        action = s.get("action")
        if action not in allowed:
            dropped["steps"].append(action)
        elif action not in [x["action"] for x in steps] and len(steps) < 4:
            steps.append({"action": action, "says": ACTIONS[action], "why": str(s.get("why", "")).strip()[:200]})
    if not steps:  # nothing usable: code's own first step for each hold
        steps = [{"action": a, "says": ACTIONS[a], "why": "Tally's default first step for this hold."}
                 for a in dict.fromkeys(ALLOWED.get(c, ["check_again_later"])[0] for c in ev["held_by"])][:4]
    risk = proposal.get("risk") if proposal.get("risk") in RISKS else "high"
    floor = max((RISK_FLOOR.get(c, "low") for c in ev["held_by"]), key=RISKS.index, default="low")
    if RISKS.index(risk) < RISKS.index(floor):
        dropped["risk"] = f"{risk} raised to {floor}, the floor for {', '.join(c for c in ev['held_by'] if RISK_FLOOR.get(c) == floor)}"
        risk = floor
    casefile = {"summary": str(proposal.get("summary", "")).strip()[:500], "risk": risk, "findings": findings, "next_steps": steps,
                "call": call_script(ev, vendor, inv) if any(s["action"] == "phone_vendor_on_file" for s in steps) else None}
    return casefile, dropped


def call_script(ev: dict, vendor: dict | None, inv: dict) -> dict | None:
    """Who to phone, on which number, and what to ask: all from records, none of it from the invoice or the model."""
    if vendor is None or not vendor.get("phone"):
        return None
    facts = (inv["decision"] or {}).get("facts") or {}
    total = f"{money(facts.get('total_cents'))} {facts.get('currency') or ''}".strip() if facts.get("total_cents") is not None else "an amount"
    when = _agreed_date(inv, "invoice_date") or "an unclear date"
    asks = [QUESTIONS[c].format(total=total, date=when) for c in ev["held_by"] if c in QUESTIONS]
    return {"vendor": vendor["name"], "phone": vendor["phone"], "ask": asks or ["Did you send us this invoice?"]}


# The worker.

def _case_vendor(store: Store, inv: dict) -> dict | None:
    """The vendor on file to phone: this invoice's, or the one its printed name imitates."""
    facts = (inv["decision"] or {}).get("facts") or {}
    if facts.get("vendor_id"):
        return store.vendor(facts["vendor_id"])
    fields = (inv.get("reading") or {}).get("fields") or {}
    return _find_vendor(fields.get("vendor_name") or "", store.vendors())[1] if fields else None


def needs_case(store: Store, inv: dict, ev: dict) -> bool:
    attempts = store.case_attempts(inv["id"], basis(ev))
    if any(a["status"] == "written" for a in attempts):
        return False
    if len(attempts) >= TRIES_PER_BASIS:
        return False
    if attempts:
        last = datetime.fromisoformat(attempts[-1]["at"])
        return (datetime.now(timezone.utc) - last).total_seconds() > RETRY_AFTER_SECONDS
    return True


def write_case(store: Store, inv: dict, ev: dict | None = None, models: list[str] | None = None) -> int:
    """Ask SERV for a case file on one held invoice, check it, and save it with its ledger entry."""
    ev = ev or evidence(store, inv)
    try:
        a = reason_json(SYSTEM, request_text(ev), CASE_SCHEMA, models or CASE_MODELS, name="case_file", effort="low", max_tokens=1600,
                        tools=SERV_TOOLS)
    except InferenceError as e:
        return store.save_case(WORKER, inv["id"], basis(ev), evidence=ev, proposal=None, casefile=None, detail={"why": str(e)[:300]})
    casefile, dropped = check_proposal(a.data, ev, vendor=_case_vendor(store, inv), inv=inv)
    detail = {"model": a.model, "serv_request_id": a.request_id, "seconds": round(a.seconds, 2), "usd": round(a.usd, 6),
              "tokens": [a.input_tokens, a.output_tokens], "risk": casefile["risk"], "steps": [s["action"] for s in casefile["next_steps"]],
              "findings": len(casefile["findings"]), "dropped": dropped, "serv": ["prompt guard", "shadow agent"]}
    return store.save_case(WORKER, inv["id"], basis(ev), evidence=ev, proposal=a.data, casefile=casefile, detail=detail)
