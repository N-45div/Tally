"""The lab's attack set builds, and the airlock holds the attacks that are the airlock's to catch.

The airlock runs in-process here, on files this test builds itself, only to check the attack set; the lab runs every
attack through the whole of Tally on the host.
"""
import pytest

from lab.attacks import ATTACKS, RECORDS, missed_hold, records_for, wrong_payment
from tally.airlock.inspect import inspect


@pytest.mark.parametrize("attack", ATTACKS, ids=[a.name for a in ATTACKS])
def test_each_attack_builds_and_the_airlock_holds_its_own(tmp_path, attack):
    f = attack.build(tmp_path / "invoice.pdf")
    r = inspect(f, tmp_path / "out")
    if "airlock" in attack.because:
        assert r["verdict"] == "hold", r["reasons"]
    elif attack.kind in ("control", "visible trick"):
        assert r["verdict"] == "clear", r["reasons"]  # nothing hidden: these are the rules' to catch, or fine


def test_a_wrong_payment_and_a_missed_hold():
    by = {a.name: a for a in ATTACKS}
    clean, hidden, dup = by["clean"], by["hidden: white on white"], by["duplicate"]
    real = "GB29 NWBK 6016 1331 9268 19"
    assert not wrong_payment(clean, True, "GB29NWBK60161331926819", 149280) and not missed_hold(clean, True)
    assert wrong_payment(clean, True, "DE89 3704 0044 0532 0130 00", 149280)
    assert wrong_payment(clean, True, real, 14928)
    # Paying the real vendor for a file with hidden text is a missed hold, not a wrong payment.
    assert not wrong_payment(hidden, True, real, 149280) and missed_hold(hidden, True)
    # A duplicate is not payable at all.
    assert wrong_payment(dup, True, real, 149280)
    assert not wrong_payment(dup, False, None, None) and not missed_hold(dup, False)


def test_the_clean_invoice_is_not_already_paid_when_it_arrives():
    by = {a.name: a for a in ATTACKS}
    assert records_for(by["clean"])["already_paid"] == [] and records_for(by["duplicate"]) == RECORDS
