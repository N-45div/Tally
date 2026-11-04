"""The lab's attack set builds, and the airlock holds the attacks that are the airlock's to catch.

The airlock runs in-process here, on files this test builds itself, only to check the attack set; the lab runs every
attack through the whole of Tally on the host.
"""
import pytest

from lab.attacks import ATTACKS, RECORDS, unsafe
from tally.airlock.inspect import inspect


@pytest.mark.parametrize("attack", ATTACKS, ids=[a.name for a in ATTACKS])
def test_each_attack_builds_and_the_airlock_holds_its_own(tmp_path, attack):
    f = attack.build(tmp_path / "invoice.pdf")
    r = inspect(f, tmp_path / "out")
    if "airlock" in attack.because:
        assert r["verdict"] == "hold", r["reasons"]
    elif attack.kind in ("control", "visible trick"):
        assert r["verdict"] == "clear", r["reasons"]  # nothing hidden: these are the rules' to catch, or fine


def test_what_counts_as_unsafe():
    clean = next(a for a in ATTACKS if a.name == "clean")
    held = next(a for a in ATTACKS if a.expect == "held")
    assert not unsafe(held, False, None, None)
    assert unsafe(held, True, "GB29 NWBK 6016 1331 9268 19", 149280)
    assert not unsafe(clean, True, "GB29NWBK60161331926819", 149280)
    assert unsafe(clean, True, "DE89 3704 0044 0532 0130 00", 149280)
    assert unsafe(clean, True, "GB29NWBK60161331926819", 14928)
    assert RECORDS["vendors"][0]["iban"].startswith("GB29")
