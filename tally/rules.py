"""The fixed rules that decide whether an invoice is held or ready for a person to approve.

The model never decides. It copies fields off the page images, twice, by two different model families; these rules
compare what it copied with what Tally already knows: the vendors and the bank details on file for them, the
purchase orders, and the invoices seen before. Any failed check holds the invoice, and every check is reported,
passed or not, so the person approving sees why.

The bank account printed on an invoice is never used for payment. It is compared with the one on file, and a
difference holds the invoice until someone confirms the change with the vendor on the phone number on file.
"""
from __future__ import annotations

from collections.abc import Callable

from tally.normalize import bank, iban_ok, key, looks_like, money, to_cents, vendor_key

# The fields two readings must agree on, and how each is compared.
COMPARED = {
    "vendor": lambda r: vendor_key(r.get("vendor_name")),
    "invoice number": lambda r: key(r.get("invoice_number")),
    "purchase order": lambda r: key(r.get("po_number")),
    "currency": lambda r: key(r.get("currency")),
    "total": lambda r: to_cents(r.get("total")),
    "bank details": lambda r: tuple(sorted((k, v) for k, v in bank(r.get("pay_to")).items() if v)),
}
BANK_FIELDS = {"iban": "IBAN", "account_number": "account number", "sort_code": "sort code", "routing_number": "routing number"}


def _shown(field: str, value) -> str:
    if field == "total":
        return money(value)
    if field == "bank details":
        return _show_bank(dict(value))
    return value or "nothing"


class _Report:
    def __init__(self):
        self.checks: list[dict] = []

    def check(self, code: str, ok: bool, text: str) -> bool:
        self.checks.append({"code": code, "ok": ok, "text": text})
        return ok

    def result(self, facts: dict) -> dict:
        holds = [{"code": c["code"], "text": c["text"]} for c in self.checks if not c["ok"]]
        return {"ready": not holds, "holds": holds, "checks": self.checks, "facts": facts}


def _find_vendor(printed: str, vendors: list[dict]) -> tuple[dict | None, dict | None]:
    """The vendor whose name or alias is the printed name; or else one it could be mistaken for."""
    k = vendor_key(printed)
    for v in vendors:
        if k and k in {v["name_key"], *map(vendor_key, v["aliases"])}:
            return v, None
    for v in vendors:
        if any(looks_like(printed, n) for n in (v["name"], *v["aliases"])):
            return None, v
    return None, None


def _show_bank(b: dict) -> str:
    shown = [f"{BANK_FIELDS[k]} {v}" for k, v in b.items() if v]
    return ", ".join(shown) or "no bank details"


def decide(airlock: dict, reading: dict | None, check_reading: dict | None, *, reader_model: str | None, checker_model: str | None,
           vendors: list[dict], pos: list[dict], others: Callable[[int | None, str], dict]) -> dict:
    """Run every check. `others(vendor_id, invoice_number_key)` returns the other live invoices with the same file
    ("same_file") or the same vendor and number ("same_number")."""
    r = _Report()
    facts = {"vendor_id": None, "vendor_name": None, "po_id": None, "po_number": None, "invoice_number_key": None,
             "total_cents": None, "currency": None, "pay_to_on_file": None}

    reasons = airlock.get("reasons") or []
    r.check("airlock", airlock.get("verdict") == "clear",
            "The file is clean: nothing hidden, nothing active." if airlock.get("verdict") == "clear"
            else "The file was held when it was opened: " + "; ".join(reasons))

    if not r.check("read", bool(reading), "The invoice was read from its page images." if reading
                   else "The invoice could not be read, so nothing else could be checked."):
        return r.result(facts)

    facts["invoice_number_key"] = key(reading.get("invoice_number"))
    facts["total_cents"] = to_cents(reading.get("total"))
    facts["currency"] = key(reading.get("currency"))

    # Two readings, by two model families, must agree on everything that decides a payment.
    if not check_reading:
        r.check("second_reading", False, "There is no second reading to compare with.")
    elif reader_model and reader_model == checker_model:
        r.check("second_reading", False, f"Both readings came from the same model ({reader_model}), so they are not independent.")
    else:
        differ = [f"{name} ({_shown(name, show(reading))} vs {_shown(name, show(check_reading))})" for name, show in COMPARED.items()
                  if show(reading) != show(check_reading)]
        r.check("second_reading", not differ, f"A second model ({checker_model}) read the same values." if not differ
                else "The two readings disagree on " + "; ".join(differ) + ".")

    # The vendor must be one on file, by its exact name.
    printed_name = reading.get("vendor_name") or ""
    vendor, lookalike = _find_vendor(printed_name, vendors)
    if vendor:
        facts.update(vendor_id=vendor["id"], vendor_name=vendor["name"], pay_to_on_file=vendor["bank"])
        r.check("vendor", True, f"The vendor is {vendor['name']}, on file.")
    elif lookalike:
        r.check("vendor", False, f"The invoice is from \"{printed_name}\", which looks like {lookalike['name']} on file but is not the same name.")
    else:
        r.check("vendor", False, f"\"{printed_name or 'No vendor name'}\" is not a vendor on file.")

    # The bank details printed must be the ones on file, or absent.
    printed = bank(reading.get("pay_to"))
    if printed["iban"] and not iban_ok(printed["iban"]):
        r.check("iban", False, f"The printed IBAN {printed['iban']} fails its check digits: it was misread or altered.")
    if vendor:
        on_file = vendor["bank"]
        differ = [f for f in BANK_FIELDS if printed[f] and printed[f] != on_file[f]]
        if not any(printed.values()):
            r.check("bank", True, "No bank details are printed; payment goes to the account on file.")
        elif not differ:
            r.check("bank", True, "The bank details printed are the ones on file.")
        else:
            phone = vendor.get("phone") or "the number on file"
            r.check("bank", False, f"The invoice asks for payment to {_show_bank(printed)}, but the account on file is "
                                   f"{_show_bank(on_file)}. Confirm any change by phoning {phone}, not a number on the invoice.")

    # The purchase order must exist, be this vendor's, be open, be in this currency, and have room for this total.
    po_key = key(reading.get("po_number"))
    po = next((p for p in pos if p["number_key"] == po_key), None) if po_key else None
    total = facts["total_cents"]
    if not po_key:
        r.check("po", False, "No purchase order number is printed.")
    elif po is None:
        r.check("po", False, f"Purchase order {reading.get('po_number')} is not on file.")
    else:
        facts.update(po_id=po["id"], po_number=po["number"])
        left = po["amount_cents"] - po["spent_cents"]
        if vendor is None or po["vendor_id"] != vendor["id"]:
            r.check("po", False, f"Purchase order {po['number']} is for {po['vendor_name']}, not this vendor.")
        elif po["status"] != "open":
            r.check("po", False, f"Purchase order {po['number']} is closed.")
        elif po["currency"] != facts["currency"]:
            r.check("po", False, f"Purchase order {po['number']} is in {po['currency']}, the invoice in {facts['currency'] or 'no currency'}.")
        elif total is None or total > left:
            r.check("po", False, f"The total {money(total)} is more than the {money(left)} left on purchase order {po['number']}.")
        else:
            r.check("po", True, f"Purchase order {po['number']} has {money(left)} left; this uses {money(total)}.")

    # The numbers must add up.
    sub, tax = to_cents(reading.get("subtotal")), to_cents(reading.get("tax"))
    lines = reading.get("lines") or []
    wrong = []
    if total is None or total <= 0:
        wrong.append("there is no total")
    for i, line in enumerate(lines, 1):
        q, unit, amount = line.get("quantity"), to_cents(line.get("unit_price")), to_cents(line.get("amount"))
        if q is not None and unit is not None and amount is not None and abs(round(q * unit) - amount) > 1:
            wrong.append(f"line {i}: {q} x {money(unit)} is not {money(amount)}")
    amounts = [to_cents(line.get("amount")) for line in lines]
    if sub is not None and amounts and None not in amounts and abs(sum(amounts) - sub) > len(amounts):
        wrong.append(f"the lines add up to {money(sum(amounts))}, not the subtotal {money(sub)}")
    if sub is not None and tax is not None and total is not None and abs(sub + tax - total) > 1:
        wrong.append(f"{money(sub)} + {money(tax)} tax is not the total {money(total)}")
    r.check("sums", not wrong, "The lines, subtotal, tax and total add up." if not wrong else "The numbers do not add up: " + "; ".join(wrong) + ".")

    # Not seen before.
    seen = others(facts["vendor_id"], facts["invoice_number_key"])
    if seen["same_file"]:
        r.check("duplicate", False, "This file was uploaded before, as invoice " + ", ".join(f"#{i}" for i in seen["same_file"]) + ".")
    elif seen["same_number"]:
        r.check("duplicate", False, f"Invoice number {reading.get('invoice_number')} from this vendor is already invoice "
                + ", ".join(f"#{i}" for i in seen["same_number"]) + ".")
    else:
        r.check("duplicate", True, "This invoice has not been seen before.")
    return r.result(facts)
