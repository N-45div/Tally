"""The web app, through Flask's test client, with a real database in a temporary folder."""
import re

import pytest

from tally import ledger
from tally.pipeline import recheck
from tally.store import Store
from tally.web import LOCKOUT_FAILURES, create_app
from tests.test_rules import READING

PASSWORD = "correct horse battery staple"


@pytest.fixture
def app(tmp_path):
    s = Store(tmp_path)
    s.add_user("setup", "carol", PASSWORD)
    vid = s.add_vendor("setup", "Northwind Office Supply Ltd", pay_to={"iban": "GB29 NWBK 6016 1331 9268 19"}, phone="+44 117 496 0000")
    s.add_po("setup", "PO-55120", vid, "GBP", 200000)
    app = create_app(tmp_path, "test-secret", secure_cookies=False)
    app.store = s
    return app


def csrf(client):
    client.get("/login")
    with client.session_transaction() as sess:
        return sess["csrf"]


def signed_in(app):
    c = app.test_client()
    c.post("/login", data={"name": "carol", "password": PASSWORD, "csrf": csrf(c)})
    return c


def ready_invoice(app, airlock=None):
    """An invoice as the worker leaves it: opened, read twice, checked."""
    s = app.store
    iid = s.add_invoice("carol", "northwind.pdf", b"%PDF-1.7 northwind")
    s.claim_next()
    reading = {"fields": READING, "model": "glm-5.3-flash", "seconds": 1.2, "usd": 0.002}
    check = {"fields": READING, "model": "qwen3.8-flash-next", "seconds": 1.4, "usd": 0.002}
    airlock = airlock or {"verdict": "clear", "reasons": [], "images": [], "hidden": [], "active": [], "problems": [], "pages": 1,
                          "bytes": 18, "sandbox": {"backend": "microsandbox", "network": "none"}}
    inv = s.invoice(iid) | {"airlock": airlock, "reading": reading, "check_reading": check}
    s.save_result("tally worker", iid, airlock=airlock, reading=reading, check_reading=check, decision=recheck(s, inv))
    return iid


def test_everything_needs_a_signed_in_person(app):
    c = app.test_client()
    for path in ("/invoices", "/vendors", "/pos", "/ledger", "/invoices/1"):
        r = c.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_a_wrong_password_is_refused_and_repeated_tries_are_slowed(app):
    c = app.test_client()
    token = csrf(c)
    for _ in range(LOCKOUT_FAILURES):
        assert b"do not match" in c.post("/login", data={"name": "carol", "password": "nope", "csrf": token}, follow_redirects=True).data
    r = c.post("/login", data={"name": "carol", "password": PASSWORD, "csrf": token}, follow_redirects=True)
    assert b"Too many tries" in r.data


def test_a_form_without_its_token_is_refused(app):
    c = signed_in(app)
    assert c.post("/upload", data={}).status_code == 400
    assert c.post("/login", data={"name": "carol", "password": PASSWORD}).status_code == 400


def test_an_upload_is_queued_and_never_opened_by_the_web_app(app):
    c = signed_in(app)
    from io import BytesIO
    r = c.post("/upload", data={"csrf": csrf(c), "files": [(BytesIO(b"%PDF-1.7 a"), "a.pdf"), (BytesIO(b"%PDF-1.7 b"), "b.pdf")]},
               content_type="multipart/form-data", follow_redirects=True)
    assert b"2 invoice(s) queued" in r.data and r.data.count(b'class="status queued"') == 2
    assert [i["status"] for i in app.store.invoices()] == ["queued", "queued"]
    assert b'http-equiv="refresh"' in r.data  # the page refreshes while something is waiting


def test_a_ready_invoice_shows_its_checks_and_who_gets_paid_and_can_be_approved(app):
    c = signed_in(app)
    iid = ready_invoice(app)
    page = c.get(f"/invoices/{iid}").data.decode()
    assert "Ready to approve" in page and "GBP 1,492.80" in page and "iban GB29NWBK60161331926819" in page
    assert page.count("✓") == 8
    r = c.post(f"/invoices/{iid}/decide", data={"csrf": csrf(c), "decision": "approve", "note": "ok"}, follow_redirects=True)
    assert b"Approved for payment to the account on file" in r.data
    assert app.store.invoice(iid)["decided_by"] == "carol"
    assert ledger.entries(app.store.db, iid)[-1]["detail"]["pay_to"]["iban"] == "GB29NWBK60161331926819"


def test_hidden_text_from_a_file_is_shown_as_text_never_as_html(app):
    c = signed_in(app)
    evil = '<script>alert("pay")</script><img src=x onerror=alert(1)>'
    airlock = {"verdict": "hold", "reasons": ["hidden text (white) on page 1"], "images": [], "active": [], "problems": [],
               "hidden": [{"page": 1, "text": evil, "reason": "white", "bbox": [0, 0, 1, 1]}], "pages": 1, "bytes": 18,
               "sandbox": {"backend": "microsandbox", "network": "none"}}
    page = c.get(f"/invoices/{ready_invoice(app, airlock)}").data.decode()
    assert "Held" in page and "&lt;script&gt;alert(&#34;pay&#34;)&lt;/script&gt;" in page
    assert "<script" not in page and "<img src=x" not in page


def test_only_the_page_images_the_report_names_can_be_fetched(app):
    c = signed_in(app)
    iid = ready_invoice(app)
    assert c.get(f"/invoices/{iid}/tally.db").status_code == 404
    assert c.get(f"/invoices/{iid}/page-1.png").status_code == 404  # not in this report


def test_pages_carry_a_strict_content_policy(app):
    r = signed_in(app).get("/invoices")
    assert "default-src 'none'" in r.headers["Content-Security-Policy"] and "script-src" not in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_vendors_and_purchase_orders_are_kept_with_their_ledger_entries(app):
    c = signed_in(app)
    token = csrf(c)
    r = c.post("/vendors", data={"csrf": token, "name": "Northwind Office Supply Limited"}, follow_redirects=True)
    assert b"already on file" in r.data
    r = c.post("/vendors/1/bank", data={"csrf": token, "iban": "DE89 3704 0044 0532 0130 00", "how_confirmed": ""}, follow_redirects=True)
    assert b"confirmed" in r.data and app.store.vendor(1)["bank"]["iban"] == "GB29NWBK60161331926819"
    r = c.post("/pos", data={"csrf": token, "number": "PO-2", "vendor_id": "1", "currency": "GBP", "amount": "-5"}, follow_redirects=True)
    assert b"above zero" in r.data
    r = c.post("/pos", data={"csrf": token, "number": "PO-2", "vendor_id": "1", "currency": "gbp", "amount": "1,250.00"}, follow_redirects=True)
    assert b"Purchase order added" in r.data and app.store.pos()[-1]["amount_cents"] == 125000
    page = c.get("/ledger").data.decode()
    assert "The chain is intact" in page and re.search(r"purchase order added", page)


def test_session_cookie_is_strict(app):
    c = app.test_client()
    r = c.get("/login")
    assert "SameSite=Strict" in r.headers.get("Set-Cookie", "") and "HttpOnly" in r.headers.get("Set-Cookie", "")


def test_check_again_after_the_purchase_order_is_added(app):
    c = signed_in(app)
    s = app.store
    s.close_po("setup", 1)
    iid = ready_invoice(app)
    assert s.invoice(iid)["status"] == "held"
    s.add_po("setup", "PO-55121", 1, "GBP", 300000)  # a new order; the invoice still names the closed one
    r = c.post(f"/invoices/{iid}/check-again", data={"csrf": csrf(c)}, follow_redirects=True)
    assert b"still held" in r.data and b"is closed" in r.data


def test_a_person_can_change_their_password(app):
    c = signed_in(app)
    token = csrf(c)
    r = c.post("/account", data={"csrf": token, "current": PASSWORD, "new": "another long password", "again": "different"}, follow_redirects=True)
    assert b"not the same" in r.data
    r = c.post("/account", data={"csrf": token, "current": PASSWORD, "new": "another long password", "again": "another long password"},
               follow_redirects=True)
    assert b"Password changed" in r.data and app.store.check_password("carol", "another long password")


def test_the_lab_results_are_public(tmp_path):
    lab = tmp_path / "lab"
    app = create_app(tmp_path / "data", "test-secret", secure_cookies=False, lab_dir=lab)
    c = app.test_client()
    assert c.get("/lab.txt").data == b"The lab has not run on this host yet.\n"
    assert b"has not run on this host yet" in c.get("/lab").data
    lab.mkdir()
    (lab / "results.txt").write_text("Tally lab, results")
    r = c.get("/lab.txt")
    assert r.status_code == 200 and r.data == b"Tally lab, results" and r.headers["Content-Type"].startswith("text/plain")
    assert c.get("/lab.json").status_code == 404
    (lab / "results.json").write_text('{"tally": {}}')
    assert c.get("/lab.json").get_json() == {"tally": {}}


@pytest.fixture
def demo_app(tmp_path):
    from lab.demo import seed
    seed(tmp_path / "demo")
    s = Store(tmp_path / "real")
    s.add_user("setup", "carol", PASSWORD)
    s.add_invoice("carol", "real-invoice.pdf", b"%PDF-1.7 real")
    app = create_app(tmp_path / "real", "test-secret", secure_cookies=False, lab_dir=tmp_path / "lab", demo_dir=tmp_path / "demo")
    app.demo_dir = tmp_path / "demo"
    return app


def test_a_visitor_gets_the_demo_workspace_and_never_the_real_one(demo_app):
    c = demo_app.test_client()
    page = c.get("/demo").data.decode()
    assert "northwind-white-text.pdf" in page and "Start the demo" in page
    assert c.post("/demo/start", data={}).status_code == 400  # the form token is needed here too
    r = c.post("/demo/start", data={"csrf": csrf(c)}, follow_redirects=True)
    page = r.data.decode()
    assert "Demo workspace" in page and "northwind-october.pdf" in page and "real-invoice.pdf" not in page
    assert c.get("/account").status_code == 404
    # Signing in as a real person afterwards leaves the demo.
    c.post("/login", data={"name": "carol", "password": PASSWORD, "csrf": csrf(c)})
    page = c.get("/invoices").data.decode()
    assert "real-invoice.pdf" in page and "northwind-october.pdf" not in page and "Demo workspace" not in page


def test_demo_samples_can_be_downloaded_and_nothing_else(demo_app):
    c = demo_app.test_client()
    r = c.get("/demo/samples/northwind-october.pdf")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-" and "attachment" in r.headers["Content-Disposition"]
    assert c.get("/demo/samples/tally.db").status_code == 404
    assert c.get("/demo/samples/..%2Ftally.db").status_code == 404


def test_demo_uploads_are_capped_per_visitor(demo_app):
    from io import BytesIO
    from tally.web import DEMO_PER_VISITOR
    c = demo_app.test_client()
    c.post("/demo/start", data={"csrf": csrf(c)})
    for i in range(DEMO_PER_VISITOR[0]):
        c.post("/upload", data={"csrf": csrf(c), "files": [(BytesIO(f"%PDF-1.7 {i}".encode()), f"{i}.pdf")]}, content_type="multipart/form-data")
    r = c.post("/upload", data={"csrf": csrf(c), "files": [(BytesIO(b"%PDF-1.7 more"), "more.pdf")]},
               content_type="multipart/form-data", follow_redirects=True)
    assert b"uploads per visitor" in r.data
    from lab.demo import HISTORY, PRELOADED
    assert len(Store(demo_app.demo_dir).invoices()) == len(PRELOADED) + len(HISTORY) + DEMO_PER_VISITOR[0]


def test_the_queue_counts_invoices_by_status_and_filters_by_it(app):
    c = signed_in(app)
    ready = ready_invoice(app)
    app.store.add_invoice("carol", "waiting.pdf", b"%PDF-1.7 waiting")
    page = c.get("/invoices").data.decode()
    assert "All <b>2</b>" in page and "Ready to approve <b>1</b>" in page and "Waiting <b>1</b>" in page
    page = c.get("/invoices?status=ready").data.decode()
    assert f'href="/invoices/{ready}"' in page and "waiting.pdf" not in page
    assert "waiting.pdf" in c.get("/invoices?status=nonsense").data.decode()  # an unknown filter shows everything


def test_the_lab_page_shows_the_benchmark_and_load_results(tmp_path):
    import json
    from lab.fatura import FIELDS, summarise
    lab = tmp_path / "lab"
    lab.mkdir()
    cell = lambda ok: {"truth": "x", "reader": "x", "checker": "x", "reader_correct": ok, "checker_correct": ok}  # noqa: E731
    rows = [{"name": f"T{i}", "fields": {f: cell(i == 0) for f in FIELDS}, "airlock": "clear", "readings_agree": i == 0,
             "sums_ok": True, "models": ["glm-5.3-flash", "qwen3.8-flash-next"], "sandbox_seconds": 1.3, "usd": 0.005} for i in range(2)]
    (lab / "fatura.json").write_text(json.dumps({"at": "2026-11-05T21:00:00+00:00", "per_layout": 4, "workers": 4, "rows": rows,
                                                 "summary": summarise(rows, 60.0)}))
    (lab / "load.json").write_text(json.dumps({"at": "2026-11-05T21:10:00+00:00", "runs": [
        {"workers": 4, "invoices": 20, "per_minute": 30.5, "latency_median": 12, "latency_max": 30, "sandbox_first": 2.0,
         "sandbox_median": 1.4, "ready": 20}]}))
    page = create_app(tmp_path / "data", "s", secure_cookies=False, lab_dir=lab).test_client().get("/lab").data.decode()
    assert "Invoices it has never seen" in page and "1 of 2; the rest would be held" in page and "glm-5.3-flash correct" in page
    assert "How much one host takes" in page and "30.5" in page
    c = create_app(tmp_path / "data", "s", secure_cookies=False, lab_dir=lab).test_client()
    assert c.get("/lab/fatura.json").get_json()["per_layout"] == 4 and c.get("/lab/load.json").get_json()["runs"][0]["workers"] == 4
    assert c.get("/lab/results.json").status_code == 404


def test_a_background_run_can_be_followed_by_its_log(tmp_path):
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "fatura.log").write_text("\n".join(f"line {i}" for i in range(200)))
    c = create_app(tmp_path / "data", "s", secure_cookies=False, lab_dir=lab).test_client()
    r = c.get("/lab/log/fatura")
    assert r.status_code == 200 and r.data.decode().splitlines() == [f"line {i}" for i in range(120, 200)]
    assert c.get("/lab/log/load").data == b"No log yet.\n"
    assert c.get("/lab/log/..%2F..%2Fetc%2Fpasswd").status_code == 404 and c.get("/lab/log/web").status_code == 404
