"""Payment runs: code's checks on a proposed plan, approval of the exact plan, the payment file, and the pages."""
import copy
import csv
import io
import os

import pytest

from tally import advisor, ledger, payruns
from tally.normalize import to_cents
from tally.pipeline import recheck
from tally.store import Store
from tests.test_cases import checked
from tests.test_rules import READING

live = pytest.mark.skipif(not os.environ.get("OPENSERV_API_KEY"), reason="needs OPENSERV_API_KEY")
VENDORS = {
    "Greystone Properties Ltd": "GB33 BUKB 2020 1555 5555 55",
    "Thames Water Utilities Ltd": "GB82 WEST 1234 5698 7654 32",
    "=Bright Sparks Electrical": "GB94 BARC 1020 1530 0934 59",
    "Acme Cloud Hosting Ltd": "GB71 NWBK 6070 6012 3456 78",
}
ROWS = [("Greystone Properties Ltd", 4200.00, "2026-11-21"), ("Thames Water Utilities Ltd", 318.40, "2026-11-18"),
        ("=Bright Sparks Electrical", 640.00, "2026-11-19"), ("Acme Cloud Hosting Ltd", 9600.00, "2026-12-03")]
POLICY = ("Keep 5,000 back for payroll. Rent and utilities are never late. Pay small suppliers before larger ones. "
          "Anything over 8,000 waits for the end-of-month run unless it is overdue.")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    for i, (name, iban) in enumerate(VENDORS.items()):
        vid = s.add_vendor("setup", name, pay_to={"iban": iban}, phone="+44 20 7946 0000")
        s.add_po("setup", f"PO-{i}", vid, "GBP", 5_000_000)
    for name, total, due in ROWS:
        f = copy.deepcopy(READING) | {"vendor_name": name, "po_number": f"PO-{list(VENDORS).index(name)}", "due_date": due,
                                      "subtotal": None, "tax": None, "total": total, "lines": []}
        f["pay_to"]["iban"] = ""
        iid = checked(s, f, number=f"INV-{len(s.invoices()) + 1}")
        assert s.decide("carol", iid, True, "", lambda inv: recheck(s, inv))["status"] == "approved"
    return s


def ids(store):
    return {inv["decision"]["facts"]["vendor_name"]: inv["id"] for inv in store.invoices("approved")}


def a_run(store, cash="13500"):
    return store.payrun(store.add_payrun("carol", POLICY, "GBP", to_cents(cash), "2026-11-20"))


def test_code_takes_amounts_and_payees_from_records_drops_unknown_ids_and_keeps_to_the_cash(store):
    run, by = a_run(store, cash="5000"), ids(store)
    cands = payruns.candidates(store, run)
    proposal = {"summary": "s", "policy_reading": [],
                "pay": [{"invoice": by["Thames Water Utilities Ltd"], "why": "utility"}, {"invoice": 999, "why": "not on the list"},
                        {"invoice": by["Greystone Properties Ltd"], "why": "rent"}, {"invoice": by["=Bright Sparks Electrical"], "why": "small"}],
                "defer": []}
    plan, changed = payruns.check_plan(proposal, cands, run)
    assert [p["invoice"] for p in plan["pay"]] == [by["Thames Water Utilities Ltd"], by["Greystone Properties Ltd"]]
    assert changed["unknown_ids"] == [999] and changed["over_cash"] == [by["=Bright Sparks Electrical"]]
    assert plan["total_cents"] == 451840 and plan["total_cents"] <= run["cash_cents"]
    rent = plan["pay"][1]
    assert rent["amount_cents"] == 420000 and rent["pay_to"]["iban"] == "GB33BUKB20201555555555"  # from the records
    deferred = {p["invoice"]: p for p in plan["defer"]}
    assert set(deferred) == {by["=Bright Sparks Electrical"], by["Acme Cloud Hosting Ltd"]}  # every candidate is placed
    assert deferred[by["=Bright Sparks Electrical"]]["flag"] == "overdue by 1 day(s) on the pay date"
    assert deferred[by["=Bright Sparks Electrical"]]["why"] == "Code took it out: the plan went over the cash."


def test_an_overdue_invoice_left_out_is_flagged_and_code_has_its_own_plan(store):
    run, by = a_run(store), ids(store)
    plan, changed = payruns.check_plan({"summary": "", "policy_reading": [], "pay": [], "defer": []}, payruns.candidates(store, run), run)
    flags = {f["invoice"]: f["flag"] for f in changed["flags"]}
    assert flags[by["Thames Water Utilities Ltd"]] == "overdue by 2 day(s) on the pay date"
    assert by["Acme Cloud Hosting Ltd"] not in flags
    assert plan["baseline"][:3] == [by["Thames Water Utilities Ltd"], by["=Bright Sparks Electrical"], by["Greystone Properties Ltd"]]


def planned(store, cash="13500", pay=("Thames Water Utilities Ltd", "Greystone Properties Ltd")):
    run, by = a_run(store, cash), ids(store)
    proposal = {"summary": "s", "policy_reading": [{"rule": "r", "applied": "a"}], "pay": [{"invoice": by[n], "why": "w"} for n in pay], "defer": []}
    plan, _ = payruns.check_plan(proposal, payruns.candidates(store, run), run)
    store.save_payrun_plan("tally advisor", run["id"], plan=plan, plan_hash=payruns.plan_hash(run, plan["pay"]), detail={"usd": 0.007})
    return store.payrun(run["id"])


def approve(store, run, plan_hash=None):
    return store.approve_payrun("carol", run["id"], plan_hash or run["plan_hash"], check=lambda r: payruns.rebuild(store, r))


def test_only_the_exact_plan_is_approved_and_each_payment_goes_to_the_account_on_file(store):
    run = planned(store)
    with pytest.raises(ValueError, match="not the plan that was shown"):
        approve(store, run, "0" * 64)
    done = approve(store, run)
    assert done["status"] == "approved"
    pays = store.payments(run["id"])
    assert [(p["vendor"], p["amount_cents"], p["pay_to"]["iban"]) for p in pays] == [
        ("Thames Water Utilities Ltd", 31840, "GB82WEST12345698765432"), ("Greystone Properties Ltd", 420000, "GB33BUKB20201555555555")]
    kinds = [e["kind"] for e in ledger.entries(store.db)]
    assert kinds[-3:] == ["payment scheduled", "payment scheduled", "payment run approved"] and ledger.verify(store.db)[0]
    with pytest.raises(ValueError):
        approve(store, done)  # once only
    assert {i["id"] for i in store.payable("GBP")} == {ids(store)["=Bright Sparks Electrical"], ids(store)["Acme Cloud Hosting Ltd"]}


def test_a_bank_change_after_the_plan_refuses_the_run(store):
    run = planned(store)
    store.change_vendor_bank("carol", run["plan"]["pay"][1]["vendor_id"], {"iban": "GB94 BARC 1020 1530 0934 59"}, "phoned")
    with pytest.raises(ValueError, match="records changed"):
        approve(store, run)
    assert store.payrun(run["id"])["status"] == "planned" and store.payments(run["id"]) == []


def test_an_invoice_paid_by_another_run_refuses_a_plan_that_still_pays_it(store):
    first, second = planned(store), planned(store)
    approve(store, first)
    with pytest.raises(ValueError, match="records changed"):
        approve(store, second)


def test_the_payment_file_quotes_anything_a_spreadsheet_would_run(store):
    run = planned(store, pay=("=Bright Sparks Electrical",))
    approve(store, run)
    rows = list(csv.reader(io.StringIO(payruns.payment_file(store, store.payrun(run["id"])))))
    assert rows[0][:3] == ["payee", "iban", "account_number"]
    assert rows[1][0] == "'=Bright Sparks Electrical" and rows[1][1] == "GB94BARC10201530093459" and rows[1][5] == "640.00"


def test_the_pages_queue_a_run_show_the_plan_and_approve_it(tmp_path, store):
    from tally.web import create_app
    from tests.test_web import PASSWORD, csrf, signed_in
    store.add_user("setup", "carol", PASSWORD)
    c = signed_in(create_app(tmp_path, "test-secret", secure_cookies=False))
    assert "4 approved invoice(s), GBP 14,758.40" in c.get("/payruns").get_data(as_text=True)
    r = c.post("/payruns", data={"csrf": csrf(c), "policy": POLICY, "cash": "13,500.00", "currency": "GBP", "pay_date": "2026-11-20"})
    rid = int(r.headers["Location"].rsplit("/", 1)[1])
    assert "Planning" in c.get(f"/payruns/{rid}").get_data(as_text=True)
    run, by = store.payrun(rid), ids(store)
    plan, _ = payruns.check_plan({"summary": "<b>s</b>", "policy_reading": [], "pay": [{"invoice": by["Thames Water Utilities Ltd"], "why": "w"}],
                                  "defer": []}, payruns.candidates(store, run), run)
    store.save_payrun_plan("tally advisor", rid, plan=plan, plan_hash=payruns.plan_hash(run, plan["pay"]), detail={"model": "gpt-5.4-mini"})
    page = c.get(f"/payruns/{rid}").get_data(as_text=True)
    assert "Approve this plan" in page and "&lt;b&gt;s&lt;/b&gt;" in page and "Thames Water Utilities Ltd" in page
    assert c.get(f"/payruns/{rid}/payments.csv").status_code == 404  # not before approval
    c.post(f"/payruns/{rid}/approve", data={"csrf": csrf(c), "plan_hash": store.payrun(rid)["plan_hash"]})
    assert store.payrun(rid)["status"] == "approved"
    f = c.get(f"/payruns/{rid}/payments.csv")
    assert f.status_code == 200 and "GB82WEST12345698765432,,,,318.40,GBP" in f.get_data(as_text=True)


@live
def test_serv_plans_a_run_that_follows_the_policy(store):
    run, by = a_run(store), ids(store)
    advisor.once(store)
    run = store.payrun(run["id"])
    assert run["status"] == "planned", run["detail"]
    paid = [p["invoice"] for p in run["plan"]["pay"]]
    assert by["Thames Water Utilities Ltd"] in paid and by["Greystone Properties Ltd"] in paid  # utilities and rent are never late
    assert by["Acme Cloud Hosting Ltd"] not in paid                                            # over 8,000 and not overdue
    assert run["plan"]["total_cents"] <= 850000                                                # 5,000 kept back for payroll
    assert run["plan"]["policy_reading"] and run["detail"]["usd"] < 0.03 and run["detail"]["serv_request_id"]
