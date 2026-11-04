import pytest

from tally.normalize import bank, iban_ok, key, looks_like, money, to_cents, vendor_key


def test_identifiers_compare_on_letters_and_digits():
    assert key("INV 2026-0418") == key("inv-2026-0418") == "INV20260418"
    assert key(None) == key("") == ""


@pytest.mark.parametrize("value, cents", [(1492.8, 149280), ("1,492.80", 149280), ("1492.80", 149280), (0.1 + 0.2, 30),
                                          (19.995, 2000), (-5, -500), (None, None), ("", None), ("n/a", None), ("NaN", None)])
def test_money_is_whole_cents(value, cents):
    assert to_cents(value) == cents


def test_money_prints_with_thousands():
    assert money(149280) == "1,492.80" and money(5) == "0.05" and money(-500) == "-5.00" and money(None) == "-"


def test_iban_check_digits():
    assert iban_ok("GB29 NWBK 6016 1331 9268 19")        # the test invoice's account
    assert iban_ok("DE89 3704 0044 0532 0130 00")        # the attacker's: valid, which is why a checksum is not enough
    assert not iban_ok("GB29 NWBK 6016 1331 9268 18")    # one digit misread
    assert not iban_ok("GB29 NWBK 6016 1331 9286 19")    # two digits swapped
    assert not iban_ok("") and not iban_ok("12345")


def test_vendor_names_drop_company_suffixes_and_nothing_else():
    assert vendor_key("Northwind Office Supply Ltd") == vendor_key("NORTHWIND OFFICE SUPPLY LIMITED") == "NORTHWIND OFFICE SUPPLY"
    assert vendor_key("Smith & Sons Co.") == "SMITH AND SONS"
    assert vendor_key("Northwind Office Supplies Ltd") != vendor_key("Northwind Office Supply Ltd")


def test_lookalike_names_are_noticed():
    assert looks_like("Northwlnd Office Supply Ltd", "Northwind Office Supply Ltd")
    assert looks_like("Northwind Office Supplies", "Northwind Office Supply")
    assert not looks_like("Northwind Office Supply Ltd", "Northwind Office Supply Limited")  # the same vendor
    assert not looks_like("Contoso Clinics", "Northwind Office Supply")


def test_bank_details_compare_without_spaces():
    assert bank({"iban": "gb29 nwbk 6016 1331 9268 19", "sort_code": "60-16-13"})["iban"] == "GB29NWBK60161331926819"
    assert bank(None) == {"iban": "", "account_number": "", "sort_code": "", "routing_number": ""}
