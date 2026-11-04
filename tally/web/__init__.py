"""Tally's web app: upload invoices, see why each is held or ready, approve, and keep vendors and purchase orders.

It never opens an uploaded file and never calls a model: it stores the bytes and queues them for the worker
(tally.pipeline), and it records people's decisions. Everything shown from an invoice is escaped by the templates,
including hidden text quoted from a file, which is a stranger's words.

    gunicorn 'tally.web:from_env()'       with TALLY_DATA and TALLY_SECRET_KEY set
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for

from tally import ledger
from tally.normalize import key, money, to_cents
from tally.pipeline import recheck
from tally.store import Store

MAX_UPLOAD = 20 * 1024 * 1024
LOCKOUT_FAILURES = 5
LOCKOUT_SECONDS = 60


def create_app(data_dir: Path, secret_key: str, secure_cookies: bool = True) -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secret_key, MAX_CONTENT_LENGTH=MAX_UPLOAD + 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=secure_cookies)
    app.jinja_env.filters["money"] = money
    # Amounts as read are shown with two decimals; everything else exactly as the model copied it.
    app.jinja_env.filters["amount"] = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    failures: dict[str, tuple[int, float]] = {}

    def store() -> Store:
        if "store" not in g:
            g.store = Store(data_dir)
        return g.store

    @app.teardown_appcontext
    def close_store(_exc):
        s = g.pop("store", None)
        if s is not None:
            s.db.close()

    def user() -> str | None:
        return session.get("user")

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not user():
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    @app.before_request
    def csrf():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), session["csrf"]):
            abort(400, "the form expired; go back and try again")

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; img-src 'self'; style-src 'self'; form-action 'self'; "
                                                   "frame-ancestors 'none'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def common():
        return {"user": user(), "csrf_token": session.get("csrf", "")}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            count, until = failures.get(name, (0, 0.0))
            if until > time.time():
                flash("Too many tries. Wait a minute and try again.")
            elif store().check_password(name, request.form.get("password", "")):
                failures.pop(name, None)
                session.clear()
                session["user"] = name
                nxt = request.args.get("next", "")
                return redirect(nxt if re.fullmatch(r"/[A-Za-z0-9/_-]*", nxt) else url_for("queue"))
            else:
                count += 1
                failures[name] = (0, time.time() + LOCKOUT_SECONDS) if count >= LOCKOUT_FAILURES else (count, 0.0)
                flash("That name and password do not match.")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def queue():
        return render_template("queue.html", invoices=store().invoices())

    @app.post("/upload")
    @login_required
    def upload():
        s, added = store(), []
        for f in request.files.getlist("files"):
            data = f.read(MAX_UPLOAD + 1)
            if not data:
                continue
            if len(data) > MAX_UPLOAD:
                flash(f"{f.filename} is larger than {MAX_UPLOAD // (1024 * 1024)} MB and was not added.")
                continue
            added.append(s.add_invoice(user(), f.filename or "invoice.pdf", data))
        flash(f"{len(added)} invoice(s) queued." if added else "No file was chosen.")
        return redirect(url_for("queue"))

    def _invoice_or_404(invoice_id: int) -> dict:
        inv = store().invoice(invoice_id)
        if inv is None:
            abort(404)
        return inv

    @app.get("/invoices/<int:invoice_id>")
    @login_required
    def invoice(invoice_id: int):
        s = store()
        inv = _invoice_or_404(invoice_id)
        vendor = s.vendor(inv["vendor_id"]) if inv["vendor_id"] else None
        return render_template("invoice.html", inv=inv, vendor=vendor, entries=ledger.entries(s.db, invoice_id))

    @app.get("/invoices/<int:invoice_id>/<name>")
    @login_required
    def page_image(invoice_id: int, name: str):
        inv = _invoice_or_404(invoice_id)
        if name not in ((inv["airlock"] or {}).get("images") or []):
            abort(404)
        return send_file(store().pages_dir(invoice_id) / name, mimetype="image/png")

    @app.post("/invoices/<int:invoice_id>/decide")
    @login_required
    def decide(invoice_id: int):
        s = store()
        approve = request.form.get("decision") == "approve"
        try:
            inv = s.decide(user(), invoice_id, approve, request.form.get("note", ""), recheck=lambda i: recheck(s, i))
        except (ValueError, KeyError) as e:
            flash(str(e).strip("'"))
        else:
            flash({"approved": "Approved for payment to the account on file.", "rejected": "Rejected.",
                   "held": "Held: something changed since it was checked. See below."}[inv["status"]])
        return redirect(url_for("invoice", invoice_id=invoice_id))

    @app.route("/vendors", methods=["GET", "POST"])
    @login_required
    def vendors():
        s = store()
        if request.method == "POST":
            f = request.form
            aliases = [a.strip() for a in f.get("aliases", "").split(";") if a.strip()]
            pay_to = {k: f.get(k, "") for k in ("iban", "account_number", "sort_code", "routing_number")}
            if not f.get("name", "").strip():
                flash("The vendor needs a name.")
                return redirect(url_for("vendors"))
            try:
                s.add_vendor(user(), f.get("name", ""), aliases=aliases, pay_to=pay_to, phone=f.get("phone", ""))
                flash("Vendor added.")
            except sqlite3.IntegrityError:
                flash("A vendor with that name is already on file.")
            return redirect(url_for("vendors"))
        return render_template("vendors.html", vendors=s.vendors())

    @app.post("/vendors/<int:vendor_id>/bank")
    @login_required
    def vendor_bank(vendor_id: int):
        f = request.form
        try:
            store().change_vendor_bank(user(), vendor_id, {k: f.get(k, "") for k in ("iban", "account_number", "sort_code", "routing_number")},
                                       f.get("how_confirmed", ""))
            flash("Bank details on file changed. The old ones are kept in the ledger.")
        except (ValueError, KeyError) as e:
            flash(str(e).strip("'"))
        return redirect(url_for("vendors"))

    @app.route("/pos", methods=["GET", "POST"])
    @login_required
    def pos():
        s = store()
        if request.method == "POST":
            f = request.form
            amount = to_cents(f.get("amount"))
            if not key(f.get("number")) or not key(f.get("currency")) or not amount or amount <= 0:
                flash("A purchase order needs a number, a currency and an amount above zero.")
                return redirect(url_for("pos"))
            try:
                s.add_po(user(), f.get("number", ""), int(f.get("vendor_id") or 0), key(f.get("currency")), amount)
                flash("Purchase order added.")
            except sqlite3.IntegrityError:
                flash("A purchase order with that number is already on file.")
            except (KeyError, ValueError):
                flash("Choose a vendor on file for the purchase order.")
            return redirect(url_for("pos"))
        return render_template("pos.html", pos=s.pos(), vendors=s.vendors())

    @app.post("/pos/<int:po_id>/close")
    @login_required
    def close_po(po_id: int):
        store().close_po(user(), po_id)
        flash("Purchase order closed.")
        return redirect(url_for("pos"))

    @app.get("/ledger")
    @login_required
    def ledger_page():
        s = store()
        ok, what = ledger.verify(s.db)
        return render_template("ledger.html", ok=ok, what=what, entries=list(reversed(ledger.entries(s.db)))[:300])

    return app


def from_env() -> Flask:
    return create_app(Path(os.environ["TALLY_DATA"]), os.environ["TALLY_SECRET_KEY"],
                      secure_cookies=os.environ.get("TALLY_INSECURE_COOKIES") != "1")
