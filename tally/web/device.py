"""What a desk device can call. Each call carries the device's own token (Authorization: Bearer ...), never a
session, so a signed-in browser cannot use these, and these cannot use a person's rights.

    GET  /device/hello                   the device's name, the queue's counts, the latest announcement
    GET  /device/news?after=<id>         announcements after that one (at most five), with their audio
    POST /device/ask                     a recorded question (WAV); returns its id at once
    GET  /device/ask/<id>?wait=<s>       its answer, waiting up to that many seconds (at most 20) for it
    GET  /device/audio/<kind>-<id>.wav   the audio of an answer to this device, or of an announcement

That is all: reading, and a question, which can put an invoice on hold. There is no call that approves, rejects,
releases or changes anything. Like the rest of the web app, these never call a model and never open an invoice:
a question is stored and queued, and the desk worker (tally.desk) answers it.
"""
from __future__ import annotations

import re
import time
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_file

from tally.speech import wav_info
from tally.store import Store

MAX_QUESTION_BYTES = 1536 * 1024    # 30 s at 24 kHz, 16-bit mono, and then some
QUESTION_SECONDS = (0.3, 30.0)
MAX_WAIT = 20.0


def register(app: Flask, data_dir: Path) -> None:
    def store() -> Store:
        if "device_store" not in g:
            g.device_store = Store(data_dir)  # devices belong to the real workspace, never the demo
        return g.device_store

    @app.teardown_appcontext
    def close_device_store(_exc):
        s = g.pop("device_store", None)
        if s is not None:
            s.db.close()

    def device_only(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            device = store().device_for_token(token)
            if device is None:
                return jsonify(error="this device is not known, or its token was revoked"), 401
            store().device_seen(device["id"], request.headers.get("X-Tally-Firmware", ""))
            g.device = device
            return view(*args, **kwargs)
        return wrapped

    def audio_url(kind: str, item_id: int) -> str:
        return f"/device/audio/{kind}-{item_id}.wav"

    @app.get("/device/hello")
    @device_only
    def device_hello():
        return jsonify(name=g.device["name"], counts=store().desk_counts(), latest=store().latest_news_id())

    @app.get("/device/news")
    @device_only
    def device_news():
        s = store()
        after = request.args.get("after", type=int)
        news = [] if after is None else s.news_after(after)
        return jsonify(counts=s.desk_counts(), latest=s.latest_news_id(),
                       news=[{"id": n["id"], "invoice_id": n["invoice_id"], "text": n["text"],
                              "audio": audio_url("news", n["id"]) if n["spoken"] == 1 else None} for n in news])

    @app.post("/device/ask")
    @device_only
    def device_ask():
        data = request.get_data(cache=False)
        if len(data) > MAX_QUESTION_BYTES:
            return jsonify(error=f"a question is at most {MAX_QUESTION_BYTES // 1024} KB"), 413
        try:
            _, channels, bits, seconds = wav_info(data)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if channels != 1 or bits != 16 or not QUESTION_SECONDS[0] <= seconds <= QUESTION_SECONDS[1]:
            return jsonify(error=f"send 16-bit mono WAV of {QUESTION_SECONDS[0]} to {QUESTION_SECONDS[1]:.0f} seconds"), 400
        try:
            rid = store().add_desk_request(g.device, data)
        except ValueError as e:
            return jsonify(error=str(e)), 429
        return jsonify(id=rid, status="queued"), 202

    @app.get("/device/ask/<int:request_id>")
    @device_only
    def device_asked(request_id: int):
        s = store()
        wait = min(max(request.args.get("wait", 0.0, type=float), 0.0), MAX_WAIT)
        until = time.monotonic() + wait
        while True:
            req = s.desk_request(request_id)
            if req is None or req["device_id"] != g.device["id"]:
                abort(404)
            if req["status"] in ("done", "failed") or time.monotonic() >= until:
                break
            time.sleep(0.25)
        done = req["status"] in ("done", "failed")
        spoken = done and (s.desk_dir() / f"answer-{request_id}.wav").is_file()
        return jsonify(id=request_id, status=req["status"], heard=req["heard"], answer=req["answer"], invoice_id=req["invoice_id"],
                       audio=audio_url("answer", request_id) if spoken else None, counts=s.desk_counts())

    @app.get("/device/audio/<name>")
    @device_only
    def device_audio(name: str):
        m = re.fullmatch(r"(answer|news)-([0-9]+)\.wav", name)
        if not m:
            abort(404)
        kind, item_id = m[1], int(m[2])
        if kind == "answer":
            req = store().desk_request(item_id)
            if req is None or req["device_id"] != g.device["id"]:
                abort(404)
        path = store().desk_dir() / name
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="audio/wav")
