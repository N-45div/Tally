"""Tally's web app: upload invoices, see why each is held or ready, approve, and keep vendors and purchase orders.

It never opens an uploaded file and never calls a model: it stores the bytes and queues them for the worker
(tally.pipeline), and it records people's decisions. Everything shown from an invoice is escaped by the templates,
including hidden text quoted from a file, which is a stranger's words.

    gunicorn 'tally.web:from_env()'       with TALLY_DATA and TALLY_SECRET_KEY set
"""
from __future__ import annotations

import json
import os
from collections import Counter
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for

from tally import ledger, payruns
from tally.normalize import key, money, to_cents
from tally.web import device as device_api, labdata
from tally.pipeline import recheck
from tally.store import Store

MAX_UPLOAD = 20 * 1024 * 1024
LOCKOUT_FAILURES = 5
LOCKOUT_SECONDS = 60
# The public demo: every upload is opened in a microVM and read by two models, so uploads there are capped.
DEMO_PER_VISITOR = (5, 600)     # at most 5 uploads in 10 minutes per visitor
DEMO_PER_HOUR = 40              # and 40 an hour across all visitors
DEMO_RUNS_PER_HOUR = 10         # payment runs planned in the demo, across all visitors


def create_app(data_dir: Path, secret_key: str, secure_cookies: bool = True, lab_dir: Path = Path("/var/www/tally/lab"),
               demo_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secret_key, MAX_CONTENT_LENGTH=MAX_UPLOAD + 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=secure_cookies)
    app.jinja_env.filters["money"] = money
    # Amounts as read are shown with two decimals; everything else exactly as the model copied it.
    app.jinja_env.filters["amount"] = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    failures: dict[str, tuple[int, float]] = {}

    def in_demo() -> bool:
        return demo_dir is not None and session.get("workspace") == "demo"

    def store() -> Store:
        """The demo workspace for visitors to the demo, the real one for everyone else. Never both."""
        if "store" not in g:
            g.store = Store(demo_dir if in_demo() else data_dir)
        return g.store

    def demo_samples() -> list[dict]:
        try:
            return json.loads((demo_dir / "samples.json").read_text()) if demo_dir else []
        except (OSError, ValueError):
            return []

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
        if request.path.startswith("/device/"):
            return None  # devices send their own token and no cookies; see tally/web/device.py
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
        return {"user": user(), "csrf_token": session.get("csrf", ""), "in_demo": in_demo(), "demo_on": demo_dir is not None}

    @app.get("/demo")
    def demo():
        if demo_dir is None:
            abort(404)
        return render_template("demo.html", samples=demo_samples())

    @app.post("/demo/start")
    def demo_start():
        if demo_dir is None:
            abort(404)
        session.clear()
        session["csrf"] = secrets.token_urlsafe(32)
        session["user"] = f"visitor-{secrets.token_hex(2)}"
        session["workspace"] = "demo"
        flash("You are in the demo workspace. Upload a sample from the demo page, or your own PDF.")
        return redirect(url_for("queue"))

    @app.get("/demo/samples/<name>")
    def demo_sample(name: str):
        if name not in {x["file"] for x in demo_samples()}:
            abort(404)
        return send_file(demo_dir / "samples" / name, mimetype="application/pdf", as_attachment=True)

    @app.get("/")
    def home():
        latest, history = labdata.load(lab_dir)
        return render_template("home.html", t=labdata.totals(history), latest=latest)

    @app.get("/lab")
    def lab():
        """The lab's results, in public: how Tally is attacked after every release and what happened (see lab/)."""
        latest, history = labdata.load(lab_dir)
        t = labdata.totals(history)
        bars = []
        if t:
            rows = [("Tally", t["tally_missed"], t["files"])] + [(label, n["missed"], n["decisions"]) for label, n in t["naive"].items()]
            for i, (label, missed, of) in enumerate(rows):
                share = missed / of if of else 0.0
                bars.append({"label": label, "missed": missed, "of": of, "pct": round(100 * share), "y": 8 + 40 * i, "w": round(360 * share, 1)})
        return render_template("lab.html", latest=latest, history=history, t=t, bars=bars,
                               grid=labdata.grid(latest) if latest else [],
                               fatura=labdata.extra(lab_dir, "fatura.json"), load=labdata.extra(lab_dir, "load.json"))

    @app.get("/lab/<name>.json")
    def lab_extra_json(name: str):
        """The benchmark's and the load test's full results, every invoice included."""
        if name not in ("fatura", "load"):
            abort(404)
        found = labdata.extra(lab_dir, f"{name}.json")
        return (json.dumps(found), 200, {"Content-Type": "application/json"}) if found else ("{}", 404, {"Content-Type": "application/json"})

    @app.get("/lab/log/<name>")
    def lab_log(name: str):
        """The last lines of a background run's log (the lab, the benchmark, the load test), so a run can be followed."""
        files = {"lab": "run.log", "fatura": "fatura.log", "load": "load.log"}
        if name not in files:
            abort(404)
        try:
            lines = (lab_dir / files[name]).read_text(errors="replace").splitlines()[-80:]
        except OSError:
            lines = ["No log yet."]
        return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/lab.txt")
    def lab_txt():
        results = lab_dir / "results.txt"
        text = results.read_text() if results.is_file() else "The lab has not run on this host yet.\n"
        return text, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/lab.json")
    def lab_json():
        results = lab_dir / "results.json"
        return (results.read_text(), 200, {"Content-Type": "application/json"}) if results.is_file() else ("{}", 404, {"Content-Type": "application/json"})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            count, until = failures.get(name, (0, 0.0))
            if until > time.time():
                flash("Too many tries. Wait a minute and try again.")
            elif Store(data_dir).check_password(name, request.form.get("password", "")):  # people are in the real workspace
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

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account():
        if in_demo():
            abort(404)
        if request.method == "POST":
            f = request.form
            if f.get("new", "") != f.get("again", ""):
                flash("The two new passwords are not the same.")
            else:
                try:
                    store().change_password(user(), f.get("current", ""), f.get("new", ""))
                    flash("Password changed.")
                except ValueError as e:
                    flash(str(e).capitalize() + ".")
            return redirect(url_for("account"))
        return render_template("account.html")

    @app.get("/invoices")
    @login_required
    def queue():
        everything = store().invoices()
        counts = Counter(i["status"] for i in everything)
        status = request.args.get("status") if request.args.get("status") in counts else None
        shown = [i for i in everything if i["status"] == status] if status else everything
        return render_template("queue.html", invoices=shown, counts=counts, status=status, total=len(everything),
                               risks=store().case_risks())

    def demo_upload_refused() -> str | None:
        """Why one more upload to the demo would be over its limits, or None."""
        now = time.time()
        mine = [t for t in session.get("uploads", []) if now - t < DEMO_PER_VISITOR[1]]
        session["uploads"] = mine
        if len(mine) >= DEMO_PER_VISITOR[0]:
            return f"The demo takes {DEMO_PER_VISITOR[0]} uploads per visitor every {DEMO_PER_VISITOR[1] // 60} minutes. Try again in a few minutes."
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        if store().db.execute("SELECT COUNT(*) FROM invoices WHERE uploaded_at > ?", (since,)).fetchone()[0] >= DEMO_PER_HOUR:
            return "The demo has had all the uploads it takes this hour. The samples already in the queue show every step."
        return None

    @app.post("/upload")
    @login_required
    def upload():
        s, added = store(), []
        for f in request.files.getlist("files"):
            if in_demo():
                refused = demo_upload_refused()
                if refused:
                    flash(refused)
                    break
            data = f.read(MAX_UPLOAD + 1)
            if not data:
                continue
            if len(data) > MAX_UPLOAD:
                flash(f"{f.filename} is larger than {MAX_UPLOAD // (1024 * 1024)} MB and was not added.")
                continue
            added.append(s.add_invoice(user(), f.filename or "invoice.pdf", data))
            if in_demo():
                session["uploads"] = session.get("uploads", []) + [time.time()]
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
        return render_template("invoice.html", inv=inv, vendor=vendor, entries=ledger.entries(s.db, invoice_id),
                               desk_holds=[h for h in s.desk_holds(invoice_id) if not h["released_at"]],
                               case=s.latest_case(invoice_id) if inv["status"] == "held" else None)

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

    @app.post("/invoices/<int:invoice_id>/check-again")
    @login_required
    def check_again(invoice_id: int):
        s = store()
        try:
            inv = s.check_again(user(), invoice_id, recheck=lambda i: recheck(s, i))
            flash("Checked again: " + ("ready to approve." if inv["status"] == "ready" else "still held."))
        except (ValueError, KeyError) as e:
            flash(str(e).strip("'"))
        return redirect(url_for("invoice", invoice_id=invoice_id))

    @app.post("/invoices/<int:invoice_id>/release")
    @login_required
    def release_desk_hold(invoice_id: int):
        """Only a signed-in person lifts a hold placed from a desk device; the checks then run again."""
        s = store()
        try:
            inv = s.release_desk_holds(user(), invoice_id, request.form.get("note", ""), recheck=lambda i: recheck(s, i))
            flash("Desk hold released and checked again: " + ("ready to approve." if inv["status"] == "ready" else f"{inv['status']}."))
        except (ValueError, KeyError) as e:
            flash(str(e).strip("'").capitalize() + ".")
        return redirect(url_for("invoice", invoice_id=invoice_id))

    @app.route("/devices", methods=["GET", "POST"])
    @login_required
    def devices():
        """Desk devices: add one (its token is shown once), revoke one, and see what they were asked."""
        if in_demo():
            abort(404)
        s, new = store(), None
        if request.method == "POST":
            try:
                did, token = s.add_device(user(), request.form.get("name", ""))
                new = {"id": did, "name": s.device(did)["name"], "token": token}
            except ValueError as e:
                flash(str(e).capitalize() + ".")
            except sqlite3.IntegrityError:
                flash("A device with that name is already on file.")
        asked = [e for e in reversed(ledger.entries(s.db)) if e["kind"] in ("desk question", "held from desk")][:30]
        return render_template("devices.html", devices=s.devices(), new=new, asked=asked)

    @app.post("/devices/<int:device_id>/revoke")
    @login_required
    def revoke_device(device_id: int):
        if in_demo():
            abort(404)
        try:
            store().revoke_device(user(), device_id)
            flash("Device revoked: its token no longer works.")
        except KeyError:
            flash("That device is not in use.")
        return redirect(url_for("devices"))

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

    # Payment runs. The web app only records what a person asks for and decides; the advisor worker plans.

    @app.get("/payruns")
    @login_required
    def payruns_page():
        s = store()
        payable = Counter()
        totals = Counter()
        for inv in s.invoices("approved"):
            if not s.db.execute("SELECT 1 FROM payments WHERE invoice_id = ?", (inv["id"],)).fetchone():
                payable[inv["currency"]] += 1
                totals[inv["currency"]] += inv["total_cents"] or 0
        return render_template("payruns.html", runs=s.payruns(), payable=payable, totals=totals,
                               today=datetime.now(timezone.utc).date().isoformat())

    @app.post("/payruns")
    @login_required
    def payruns_add():
        s = store()
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        if in_demo() and s.db.execute("SELECT COUNT(*) FROM payruns WHERE requested_at > ?", (since,)).fetchone()[0] >= DEMO_RUNS_PER_HOUR:
            flash(f"The demo plans {DEMO_RUNS_PER_HOUR} payment runs an hour. The runs already here show every step.")
            return redirect(url_for("payruns_page"))
        cash = to_cents(request.form.get("cash", "").replace(",", ""))
        pay_date = request.form.get("pay_date", "")
        try:
            datetime.strptime(pay_date, "%Y-%m-%d")
            rid = s.add_payrun(user(), request.form.get("policy", ""), request.form.get("currency", ""), cash or 0, pay_date)
        except ValueError as e:
            flash(str(e) if "does not match" not in str(e) else "Give the pay date as a date.")
            return redirect(url_for("payruns_page"))
        return redirect(url_for("payrun_page", payrun_id=rid))

    def _payrun_or_404(payrun_id: int) -> dict:
        run = store().payrun(payrun_id)
        if run is None:
            abort(404)
        return run

    @app.get("/payruns/<int:payrun_id>")
    @login_required
    def payrun_page(payrun_id: int):
        s = store()
        run = _payrun_or_404(payrun_id)
        return render_template("payrun.html", run=run, payments=s.payments(payrun_id) if run["status"] == "approved" else [],
                               entries=[e for e in ledger.entries(s.db) if e["detail"].get("payrun_id") == payrun_id])

    @app.post("/payruns/<int:payrun_id>/approve")
    @login_required
    def payrun_approve(payrun_id: int):
        s = store()
        try:
            s.approve_payrun(user(), payrun_id, request.form.get("plan_hash", ""), check=lambda r: payruns.rebuild(s, r))
        except (ValueError, KeyError) as e:
            flash(str(e).strip("'"))
        else:
            flash("Approved. The payments are recorded to the bank details on file; download the payment file for the bank.")
        return redirect(url_for("payrun_page", payrun_id=payrun_id))

    @app.post("/payruns/<int:payrun_id>/discard")
    @login_required
    def payrun_discard(payrun_id: int):
        try:
            store().discard_payrun(user(), payrun_id)
        except ValueError as e:
            flash(str(e))
        return redirect(url_for("payrun_page", payrun_id=payrun_id))

    @app.get("/payruns/<int:payrun_id>/payments.csv")
    @login_required
    def payrun_file(payrun_id: int):
        run = _payrun_or_404(payrun_id)
        if run["status"] != "approved":
            abort(404)
        return app.response_class(payruns.payment_file(store(), run), mimetype="text/csv",
                                  headers={"Content-Disposition": f"attachment; filename=tally-payment-run-{payrun_id}.csv"})

    device_api.register(app, data_dir)
    return app


def from_env() -> Flask:
    demo = os.environ.get("TALLY_DEMO")
    return create_app(Path(os.environ["TALLY_DATA"]), os.environ["TALLY_SECRET_KEY"],
                      secure_cookies=os.environ.get("TALLY_INSECURE_COOKIES") != "1", demo_dir=Path(demo) if demo else None)
