"""The fixed rules, on the test invoice as both models read it in the live tests, and on each way it can go wrong."""
import copy

import pytest

from tally.rules import decide
from tally.store import Store

READING = {
    "vendor_name": "Northwind Office Supply Ltd", "vendor_address": "12 Harbour Road, Bristol BS1 4XX", "bill_to": "Contoso Clinics",
    "invoice_number": "INV-2026-0418", "invoice_date": "2026-10-28", "due_date": "2026-11-27", "po_number": "PO-55120",
    "currency": "GBP", "subtotal": 1244.0, "tax": 248.8, "total": 1492.8,
    "pay_to": {"account_name": "Northwind Office Supply Ltd", "iban": "GB29 NWBK 6016 1331 9268 19", "account_number": "",
               "sort_code": "", "routing_number": "", "swift_bic": ""},
    "lines": [{"description": "A4 paper, 80gsm box", "quantity": 40, "unit_price": 21.5, "amount": 860.0},
              {"description": "Toner cartridge TN-2420", "quantity": 6, "unit_price": 64.0, "amount": 384.0}],
}
CLEAR = {"verdict": "clear", "reasons": []}
NO_OTHERS = lambda vendor_id, number: {"same_file": [], "same_number": []}  # noqa: E731


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.vid = s.add_vendor("alice", "Northwind Office Supply Ltd", aliases=["Northwind Supplies"],
                         pay_to={"iban": "GB29 NWBK 6016 1331 9268 19"}, phone="+44 117 496 0000")
    s.other = s.add_vendor("alice", "Fabrikam Cleaning Ltd", pay_to={"account_number": "31926819", "sort_code": "60-16-13"})
    s.pid = s.add_po("alice", "PO-55120", s.vid, "GBP", 200000)
    return s


def run(store, reading=READING, check=None, airlock=CLEAR, models=("glm-5.3-flash", "qwen3.8-flash-next"), others=NO_OTHERS):
    return decide(airlock, reading, reading if check is None else check, reader_model=models[0], checker_model=models[1],
                  vendors=store.vendors(), pos=store.pos(), others=others)


def changed(**fields):
    r = copy.deepcopy(READING)
    for k, v in fields.items():
        if k in r["pay_to"] and k not in r:
            r["pay_to"][k] = v
        else:
            r[k] = v
    return r


def holds(decision):
    return [h["code"] for h in decision["holds"]]


def test_the_clean_invoice_is_ready_and_every_check_says_why(store):
    d = run(store)
    assert d["ready"] and holds(d) == []
    assert [c["code"] for c in d["checks"]] == ["airlock", "read", "second_reading", "vendor", "bank", "po", "sums", "duplicate"]
    assert d["facts"] | {"pay_to_on_file": None} == {"vendor_id": store.vid, "vendor_name": "Northwind Office Supply Ltd", "po_id": store.pid,
                                                    "po_number": "PO-55120", "invoice_number_key": "INV20260418", "total_cents": 149280,
                                                    "currency": "GBP", "pay_to_on_file": None}
    assert "1,492.80" in next(c["text"] for c in d["checks"] if c["code"] == "po")


def test_a_file_held_by_the_airlock_stays_held(store):
    d = run(store, airlock={"verdict": "hold", "reasons": ["hidden text (white on white) on page 1"]})
    assert holds(d) == ["airlock"] and "white on white" in d["holds"][0]["text"]


def test_an_unread_invoice_is_held_and_nothing_else_is_guessed(store):
    d = run(store, reading=None, check=None)
    assert holds(d) == ["read"] and len(d["checks"]) == 2


def test_two_readings_must_agree_and_come_from_two_models(store):
    d = run(store, check=changed(total=1429.8))
    assert holds(d) == ["second_reading"] and "total (1,492.80 vs 1,429.80)" in d["holds"][0]["text"]
    d = run(store, check=changed(iban="DE89 3704 0044 0532 0130 00"))
    assert "bank details (IBAN GB29NWBK60161331926819 vs IBAN DE89370400440532013000)" in d["holds"][0]["text"]
    assert holds(run(store, models=("glm-5.3-flash", "glm-5.3-flash"))) == ["second_reading"]
    assert holds(run(store, check={})) == ["second_reading"]


def test_the_vendor_must_be_on_file_by_its_exact_name(store):
    assert run(store, changed(vendor_name="NORTHWIND OFFICE SUPPLY LIMITED"))["ready"]
    assert run(store, changed(vendor_name="Northwind Supplies"))["ready"]  # an alias on file
    d = run(store, changed(vendor_name="Northwlnd Office Supply Ltd"))
    assert "vendor" in holds(d) and "looks like Northwind Office Supply Ltd" in d["holds"][0]["text"]
    assert "vendor" in holds(run(store, changed(vendor_name="Initech")))


def test_a_new_bank_account_on_the_invoice_holds_it_and_says_how_to_confirm(store):
    d = run(store, changed(iban="DE89 3704 0044 0532 0130 00"))
    assert holds(d) == ["bank"]
    assert "DE89370400440532013000" in d["holds"][0]["text"] and "+44 117 496 0000" in d["holds"][0]["text"]
    assert "not a number on the invoice" in d["holds"][0]["text"]
    # Extra details the file does not have are a change too.
    assert holds(run(store, changed(account_number="12345678"))) == ["bank"]
    # No bank details printed: payment goes to the account on file anyway.
    assert run(store, changed(iban=""))["ready"]


def test_a_misread_iban_is_caught_by_its_check_digits(store):
    assert set(holds(run(store, changed(iban="GB29 NWBK 6016 1331 9268 18")))) == {"iban", "bank"}


@pytest.mark.parametrize("change, says", [
    ({"po_number": ""}, "No purchase order"),
    ({"po_number": "PO-99999"}, "not on file"),
    ({"currency": "EUR"}, "in GBP, the invoice in EUR"),
    ({"total": 2500.0, "subtotal": 2251.2}, "more than the 2,000.00 left"),
])
def test_the_purchase_order_must_cover_the_invoice(store, change, says):
    d = run(store, changed(**change))
    assert "po" in holds(d) and says in next(h["text"] for h in d["holds"] if h["code"] == "po")


def test_a_purchase_order_already_used_up_or_closed_holds(store):
    other = store.add_po("alice", "PO-1", store.other, "GBP", 500000)
    assert "for Fabrikam" in run(store, changed(po_number="PO-1"))["holds"][0]["text"]
    store.close_po("alice", store.pid)
    assert "closed" in run(store)["holds"][0]["text"]
    assert other


def test_the_numbers_must_add_up(store):
    d = run(store, changed(total=1500.0))
    text = next(h["text"] for h in d["holds"] if h["code"] == "sums")
    assert "1,244.00 + 248.80 tax is not the total 1,500.00" in text
    lines = copy.deepcopy(READING["lines"])
    lines[0]["amount"] = 900.0
    text = next(h["text"] for h in run(store, changed(lines=lines))["holds"] if h["code"] == "sums")
    assert "line 1: 40 x 21.50 is not 900.00" in text and "add up to 1,284.00, not the subtotal 1,244.00" in text


def test_an_invoice_seen_before_is_held(store):
    assert "uploaded before" in run(store, others=lambda v, n: {"same_file": [3], "same_number": [3]})["holds"][0]["text"]
    assert "already invoice #4" in run(store, others=lambda v, n: {"same_file": [], "same_number": [4]})["holds"][0]["text"]


def test_a_currency_printed_as_a_symbol_is_the_same_currency(store):
    # Seen in the FATURA benchmark: one model wrote "$", the other "USD", and the readings counted as different.
    usd = store.add_po("alice", "PO-777", store.vid, "USD", 500000)
    reading, check = changed(currency="$", po_number="PO-777"), changed(currency="USD", po_number="PO-777")
    d = run(store, reading, check=check)
    assert "second_reading" not in holds(d) and "po" not in holds(d), d["holds"]
    assert usd
