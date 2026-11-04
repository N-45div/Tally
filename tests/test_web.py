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
    for path in ("/", "/vendors", "/pos", "/ledger", "/invoices/1"):
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
    assert b"2 invoice(s) queued" in r.data and r.data.count(b"Waiting") == 2
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
    r = signed_in(app).get("/")
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
