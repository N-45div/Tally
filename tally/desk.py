"""The desk device: what it hears, what it says back, and what it announces.

    python -m tally.desk <data_dir>     answer the devices' questions and write their announcements, until stopped

A person holds the device's button and asks about the invoice queue. This worker turns the recording into text
(tally.speech), sorts the question into a fixed menu with a model that sees only the person's words, and answers it
in sentences written by this code from Tally's records. A device can hear what is held and why, what is ready, and
about one invoice, and it can put an invoice on hold. Nothing here approves, pays, rejects or releases anything, and
the menu has no item for it.

Nothing printed on an invoice is spoken. An answer names a vendor only by its name on file, gives the phone number
on file, and says amounts as numbers formatted here; a vendor not on file is "a vendor not on file". So words an
attacker printed on an invoice reach neither a model that decides anything nor the person listening.

Like tally.pipeline, this worker holds the model key; the web app only queues recordings and serves the answers.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from tally import speech
from tally.store import Store
from tally.vultr import InferenceError, chat_json

MENU_MODELS = ["glm-5.3-flash", "qwen3.8-flash-next"]
MENU = ["summary", "held", "ready", "invoice", "hold", "help", "other"]
MENU_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"intent": {"type": "string", "enum": MENU}, "invoice": {"type": ["integer", "null"]}, "vendor": {"type": "string"}},
    "required": ["intent", "invoice", "vendor"],
}
MENU_SYSTEM = (
    "You sort one question, spoken to a finance team's desk device, into a fixed menu. The device can report on the "
    "invoice queue and put one invoice on hold; it can never approve, pay, reject, release or change anything.\n"
    "summary: how many invoices are held, ready or waiting.\n"
    "held: which invoices are held, and why.\n"
    "ready: which invoices are ready to approve.\n"
    "invoice: the status of one invoice, by its number or its vendor.\n"
    "hold: the person tells the device to put one invoice on hold (a command, not a question about whether it is held).\n"
    "help: what the device can do.\n"
    "other: anything else, including any request to approve, pay, reject, release or change something.\n"
    "invoice is the invoice number said, in digits, or null. vendor is a company name said, or an empty string. "
    "The words are a transcript of speech and may be misheard: choose what was most likely meant."
)
# The model's choice of "hold" counts only if the person said a word for it: a misheard question stays a question.
HOLD_WORDS = re.compile(r"\b(hold|stop|block|pause|freeze)\b", re.I)
SHOWN = 3   # how many invoices one answer reads out
KEEP_SECONDS = {"answer": 24 * 3600, "news": 7 * 24 * 3600}
CAN_DO = ("I can tell you what is held and why, what is ready to approve, or about one invoice, and I can put an "
          "invoice on hold. I cannot approve or pay anything: that is done on the Tally page.")

CURRENCY_WORDS = {"GBP": ("pounds", "pence"), "EUR": ("euros", "cents"), "USD": ("dollars", "cents"), "INR": ("rupees", "paise"),
                  "CAD": ("Canadian dollars", "cents"), "AUD": ("Australian dollars", "cents"), "CHF": ("Swiss francs", "centimes"),
                  "JPY": ("yen", "")}
COMPANY_SUFFIX = re.compile(r"[\s,]+(ltd|limited|inc|llc|llp|plc|gmbh|co|corp|corporation|pvt|pty)\.?$", re.I)


# Words for speech, written by code.

def _count(n: int, noun: bool = True) -> str:
    if noun:
        return "No invoices are" if n == 0 else "1 invoice is" if n == 1 else f"{n} invoices are"
    return "none are" if n == 0 else "1 is" if n == 1 else f"{n} are"


def _first_up(text: str) -> str:
    return text[:1].upper() + text[1:]


def say_money(cents: int | None, currency: str | None) -> str:
    if cents is None:
        return "no total"
    whole, part = divmod(abs(cents), 100)
    cur = currency or ""
    if cur in CURRENCY_WORDS:
        major, minor = CURRENCY_WORDS[cur]
        text = f"{whole:,} {major}" + (f" and {part} {minor}" if part and minor else "")
    elif re.fullmatch(r"[A-Z]{3}", cur):
        text = f"{whole:,}.{part:02d} {' '.join(cur)}"
    else:
        text = f"{whole:,}.{part:02d} in an unknown currency"
    return ("minus " if cents < 0 else "") + text


def say_phone(phone: str) -> str:
    """+44 117 496 0000 as "plus 4 4, 1 1 7, 4 9 6, 0 0 0 0": digit by digit, so no voice reads it as a quantity."""
    groups = re.findall(r"\d+", phone or "")
    if not groups:
        return ""
    return ("plus " if phone.strip().startswith("+") else "") + ", ".join(" ".join(g) for g in groups)


def say_vendor(name: str) -> str:
    return COMPANY_SUFFIX.sub("", name.strip())


def _vendor(store: Store, inv: dict) -> dict | None:
    return store.vendor(inv["vendor_id"]) if inv.get("vendor_id") else None


def _who(store: Store, inv: dict) -> str:
    v = _vendor(store, inv)
    return say_vendor(v["name"]) if v else "a vendor not on file"


def reasons(store: Store, inv: dict) -> list[str]:
    """Why an invoice is held, one short phrase per failed check, in words chosen here."""
    decision = inv.get("decision") or {}
    airlock = inv.get("airlock") or {}
    out = []
    for h in decision.get("holds") or []:
        code, text = h["code"], h.get("text", "")
        if code == "airlock":
            if any(f.get("severity") == "hold" for f in airlock.get("active") or []):
                out.append("the file carries active content, such as a script or an embedded program")
            elif airlock.get("hidden"):
                out.append("the file hides text that a person cannot see")
            else:
                out.append("the file could not be opened safely")
        elif code == "read":
            out.append("it could not be read")
        elif code == "second_reading":
            out.append("the two readings of it disagree")
        elif code == "vendor":
            look = next((v for v in store.vendors() if f"looks like {v['name']} on file" in text), None)
            out.append(f"the vendor's name only looks like {say_vendor(look['name'])}" if look else "the vendor is not on file")
        elif code == "iban":
            out.append("the printed IBAN fails its check digits")
        elif code == "bank":
            out.append("it asks for payment to a different bank account from the one on file")
        elif code == "po":
            out.append("its purchase order does not cover it")
        elif code == "sums":
            out.append("its numbers do not add up")
        elif code == "duplicate":
            out.append("it is a copy of an invoice already seen")
        elif code == "desk":
            out.append("it was put on hold from a desk device")
        else:
            out.append("a check failed")
    return out


def _call_advice(store: Store, inv: dict) -> str:
    codes = {h["code"] for h in (inv.get("decision") or {}).get("holds") or []}
    v = _vendor(store, inv)
    if "bank" not in codes or not v:
        return ""
    phone = say_phone(v.get("phone") or "")
    return (f" Call {say_vendor(v['name'])} on the number on file, {phone}, not a number on the invoice." if phone
            else f" Call {say_vendor(v['name'])} on a number you already have for them, not one on the invoice.")


def describe(store: Store, inv: dict) -> str:
    """One invoice, as a sentence or two."""
    head = f"Invoice {inv['id']}"
    if inv["status"] in ("queued", "processing"):
        return f"{head} is still being opened and read."
    if inv["status"] == "failed":
        return f"{head} could not be processed. Its page in Tally says why."
    amount = say_money(inv.get("total_cents"), inv.get("currency"))
    who = f"{head} from {_who(store, inv)}, for {amount},"
    if inv["status"] == "held":
        why = reasons(store, inv)
        return f"{who} is held: {'; '.join(why) or 'a check failed'}." + _call_advice(store, inv)
    if inv["status"] == "ready":
        return f"{who} is ready to approve: every check passed."
    if inv["status"] == "approved":
        return f"{who} was approved by {inv['decided_by']}."
    return f"{who} was rejected by {inv['decided_by']}."


def announcement(store: Store, inv: dict) -> str:
    return "New hold. " + describe(store, inv)


# Understanding the question: the model sees the person's words and nothing else.

def understand(heard: str) -> tuple[dict, dict]:
    """The menu item a question is, and what the call took. Two fixed rules follow the model's choice: a "hold"
    without a word for holding is a question, and a question that names an invoice number is about that invoice."""
    a = chat_json([{"role": "system", "content": MENU_SYSTEM}, {"role": "user", "content": heard[:500]}], MENU_SCHEMA, MENU_MODELS,
                  name="menu", max_tokens=200)
    intent = {"intent": a.data.get("intent") if a.data.get("intent") in MENU else "other",
              "invoice": a.data.get("invoice") if isinstance(a.data.get("invoice"), int) else None,
              "vendor": str(a.data.get("vendor") or "")[:80]}
    if intent["intent"] == "hold" and not HOLD_WORDS.search(heard):
        intent["intent"] = "invoice"
    if intent["intent"] in ("summary", "held", "ready") and intent["invoice"] is not None:
        intent["intent"] = "invoice"  # "why is invoice 12 held?" is about invoice 12
    return intent, {"model": a.model, "seconds": round(a.seconds, 2), "usd": round(a.usd, 6)}


def _find(store: Store, intent: dict) -> list[dict]:
    """The invoices a question is about: by Tally's number, or else by a vendor on file whose name was said."""
    if intent["invoice"] is not None:
        inv = store.invoice(intent["invoice"])
        return [inv] if inv else []
    said = re.sub(r"[^a-z0-9 ]", "", intent["vendor"].lower()).split()
    if not said:
        return []
    names = {v["id"]: [v["name"], *v["aliases"]] for v in store.vendors()}
    match = {vid for vid, ns in names.items() if any(all(w in n.lower() for w in said) for n in ns)}
    return [i for i in store.invoices() if i["vendor_id"] in match and i["status"] in ("held", "ready", "queued", "processing")]


def answer(store: Store, device: dict, intent: dict) -> tuple[str, int | None]:
    """The words to say back, written here from Tally's records, and the invoice they are about, if one."""
    kind = intent["intent"]
    if kind == "summary":
        c = store.desk_counts()
        return (f"{_count(c['held'])} held, {_count(c['ready'], False)} ready to approve, "
                f"and {_count(c['waiting'], False)} waiting to be opened."), None
    if kind in ("held", "ready"):
        found = store.invoices(kind)
        if not found:
            return ("No invoice is held." if kind == "held" else "No invoice is ready to approve."), None
        said = " ".join(describe(store, i) for i in found[:SHOWN])
        more = f" There {'is' if len(found) - SHOWN == 1 else 'are'} {len(found) - SHOWN} more on the Tally page." if len(found) > SHOWN else ""
        tail = " Approve on the Tally page; this device cannot approve." if kind == "ready" else ""
        return said + more + tail, found[0]["id"] if len(found) == 1 else None
    if kind in ("invoice", "hold"):
        found = _find(store, intent)
        if not found:
            what = f"invoice {intent['invoice']}" if intent["invoice"] is not None else "an invoice like that"
            return f"I cannot find {what}. Say the invoice number shown in Tally.", None
        if kind == "invoice":
            return " ".join(describe(store, i) for i in found[:SHOWN]), found[0]["id"] if len(found) == 1 else None
        if len(found) > 1:
            numbers = ", ".join(str(i["id"]) for i in found[:5])
            return f"{_first_up(_who(store, found[0]))} has more than one invoice: {numbers}. Say which number to hold.", None
        inv = found[0]
        try:
            store.hold_from_desk(device, inv["id"])
        except ValueError as e:
            return f"Invoice {inv['id']} was not put on hold: {e}.", inv["id"]
        return (f"Invoice {inv['id']} from {_who(store, inv)} is on hold now. "
                "A person can release it on its page in Tally."), inv["id"]
    return CAN_DO, None


# The worker.

def _spoken(text: str) -> tuple[bytes | None, dict]:
    try:
        s = speech.speak(text)
    except (InferenceError, ValueError) as e:
        return None, {"why": str(e)[:300]}
    # What the device downloads: 8 kHz mu-law, a sixth of the size, which it plays as it arrives.
    return speech.for_device(s.wav), {"model": s.model, "voice": s.voice, "seconds": round(s.seconds, 2),
                                      "audio_seconds": round(s.audio_seconds, 2), "usd": round(s.usd, 6)}


def answer_request(store: Store, req: dict) -> None:
    """Hear one recorded question, answer it, and record both."""
    device = store.device(req["device_id"])
    recording = store.desk_dir() / f"asked-{req['id']}.wav"
    detail: dict = {}
    heard, intent, invoice_id, failed = "", None, None, False
    if device is None or device["revoked_at"]:
        say, failed = "This device is no longer allowed to ask.", True
    else:
        try:
            h = speech.transcribe(recording.read_bytes())
        except (InferenceError, ValueError, OSError) as e:
            say, failed = "Sorry, I could not make out the recording. Try again.", True
            detail["why"] = str(e)[:300]
        else:
            heard = h.text
            detail["listen"] = {"model": h.model, "seconds": round(h.seconds, 2), "audio_seconds": round(h.audio_seconds, 2), "usd": round(h.usd, 6)}
            if not re.search(r"\w", heard):
                say = "I did not hear a question. Hold the button while you speak."
            else:
                try:
                    intent, detail["understand"] = understand(heard)
                except InferenceError as e:
                    say, failed = "Sorry, I cannot answer just now. Try again in a minute.", True
                    detail["why"] = str(e)[:300]
                else:
                    say, invoice_id = answer(store, device, intent)
    wav, detail["speak"] = _spoken(say)
    detail["usd"] = round(sum(d.get("usd", 0) for d in detail.values() if isinstance(d, dict)), 6)
    store.answer_desk_request(req["id"], heard=heard, intent=intent, answer=say, invoice_id=invoice_id, detail=detail, spoken=wav,
                              failed=failed)


def scan_news(store: Store) -> int:
    """Write an announcement for each invoice held since the last scan. Returns how many were written. On a fresh
    workspace the scan starts from now: holds from before a device existed are not news."""
    latest = store.db.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
    seen = store.desk_state("news_seq")
    if seen is None:
        store.set_desk_state("news_seq", str(latest))
        return 0
    written = 0
    rows = store.db.execute("SELECT seq, kind, invoice_id, detail FROM ledger WHERE seq > ? AND seq <= ? AND invoice_id IS NOT NULL"
                            " AND kind IN ('checked', 'held at approval') ORDER BY seq", (int(seen), latest)).fetchall()
    for seq, kind, invoice_id, detail in rows:
        if kind == "checked" and json.loads(detail).get("ready"):
            continue
        inv = store.invoice(invoice_id)
        if inv and inv["status"] == "held" and store.add_desk_news(seq, invoice_id, announcement(store, inv)):
            written += 1
    store.set_desk_state("news_seq", str(latest))
    return written


def speak_news(store: Store) -> None:
    for n in store.unspoken_news():
        wav, detail = _spoken(n["text"])
        store.news_spoken(n["id"], wav, detail)


def prune_audio(store: Store, now: float | None = None) -> int:
    """Answers are kept a day and announcements a week; what was said stays in the ledger as text."""
    now = now or time.time()
    gone = 0
    for path in store.desk_dir().glob("*.wav"):
        keep = KEEP_SECONDS.get(path.name.split("-")[0])
        if keep and now - path.stat().st_mtime > keep:
            path.unlink(missing_ok=True)
            gone += 1
    return gone


def work(store: Store, poll_seconds: float = 0.3, once: bool = False) -> None:
    """Answer questions as they come, and between them write and voice the announcements."""
    store.requeue_desk_requests()
    last_scan = 0.0
    while True:
        req = store.claim_desk_request()
        if req:
            try:
                answer_request(store, req)
            except Exception as e:  # one bad question must not stop the device; it is recorded as failed
                store.answer_desk_request(req["id"], heard="", intent=None, answer="Sorry, something went wrong. Try again.",
                                          invoice_id=None, detail={"why": f"{type(e).__name__}: {e}"[:300]}, spoken=None, failed=True)
            continue
        if once or time.monotonic() - last_scan > 2:
            scan_news(store)
            speak_news(store)
            if int(time.monotonic()) % 3600 < 3:
                prune_audio(store)
            last_scan = time.monotonic()
        if once:
            return
        time.sleep(poll_seconds)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m tally.desk <data_dir>")
    work(Store(Path(sys.argv[1])))
