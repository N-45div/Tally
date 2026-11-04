"""Turning what is printed on an invoice into values that can be compared exactly.

Money is whole cents (no floats). Identifiers keep letters and digits only, upper-cased, so "INV 2026-0418" and
"inv-2026-0418" are the same invoice number. Vendor names also drop company suffixes ("Ltd", "Inc"), and nothing
else: two names that differ in any other way are different vendors, which is the point.
"""
from __future__ import annotations

import difflib
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

COMPANY_SUFFIXES = {"LTD", "LIMITED", "INC", "INCORPORATED", "LLC", "LLP", "PLC", "GMBH", "CO", "CORP", "CORPORATION",
                    "COMPANY", "SA", "SARL", "BV", "AG", "PTY", "PVT"}


def key(text) -> str:
    return "".join(ch for ch in str(text or "").upper() if ch.isalnum())


def to_cents(value) -> int | None:
    """1492.8, "1,492.80" and "1492.80" are all 149280. None, "" and anything unreadable are None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def iban_ok(iban: str) -> bool:
    """The IBAN check digits (ISO 13616, mod 97). A misread or altered digit fails it."""
    s = key(iban)
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def vendor_key(name: str) -> str:
    words = re.findall(r"[A-Z0-9]+", str(name or "").upper().replace("&", " AND "))
    while words and words[-1] in COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def looks_like(a: str, b: str) -> bool:
    """Close enough to be mistaken for each other ("Northwind" and "Northwlnd"), but not the same name."""
    ka, kb = vendor_key(a), vendor_key(b)
    return ka != kb and difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.85


def bank(pay_to: dict | None) -> dict:
    p = pay_to or {}
    return {"iban": key(p.get("iban")), "account_number": key(p.get("account_number")),
            "sort_code": key(p.get("sort_code")), "routing_number": key(p.get("routing_number"))}
