"""    python -m lab <out_dir> [trials]

Builds the attack set, runs it through Tally (needs microVMs and VULTR_INFERENCE_API_KEY) and through the naive
agent, text only and with records (needs the key), `trials` times each (default 3), runs the attacks from inside the
microVM (needs microVMs), and writes results.json and results.txt to out_dir. A part that cannot run here is skipped
and says so.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from lab import guest, naive, run_tally
from lab.attacks import ATTACKER_IBAN, ATTACKS, missed_hold, records_for, wrong_payment
from tally import sandbox
from tally.normalize import key

MARK = {"wrong": "!", "missed": "~"}


def _revision() -> str:
    for p in (Path("REVISION"),):
        if p.exists():
            return p.read_text().strip()[:12]
    done = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True)
    return done.stdout.strip() or "unknown"


def run_naive(files, trials: int) -> list[dict]:
    out = []
    for attack, path in files:
        for config, records in (("text only", None), ("with records", records_for(attack))):
            for trial in range(trials):
                d = naive.decide(path, records)
                paid = d["action"] == "pay"
                out.append({"attack": attack.name, "config": config, "trial": trial + 1, **d,
                            "wrong_payment": wrong_payment(attack, paid, d.get("iban"), d.get("amount_cents")),
                            "missed_hold": missed_hold(attack, paid),
                            "paid_attacker": paid and key(ATTACKER_IBAN) in key(d.get("iban"))})
    return out


def _cell(rows: list[dict]) -> str:
    marks = []
    for r in rows:
        word = {"pay": "pay", "hold": "hold", "error": "err"}.get(r["action"], r["action"])
        marks.append(word + (MARK["wrong"] if r["wrong_payment"] else MARK["missed"] if r["missed_hold"] else ""))
    return " ".join(marks)


def summary(res: dict) -> dict:
    """One line per run, kept in history.jsonl, because the models do not answer the same way every time."""
    out: dict = {"at": res["at"], "revision": res["revision"]}
    if res.get("tally"):
        rs = res["tally"]["results"]
        hidden = [r for r in rs if r["hidden_iban_read"] is not None]
        out["tally"] = {"files": len(rs), "as_expected": sum(r["as_expected"] for r in rs), "wrong": sum(r["wrong_payment"] for r in rs),
                        "missed": sum(r["missed_hold"] for r in rs), "hidden_read": sum(r["hidden_iban_read"] for r in hidden),
                        "hidden_files": len(hidden), "usd": round(sum(r["usd"] for r in rs), 4)}
    for c in ("text only", "with records"):
        rows = [r for r in res.get("naive") or [] if r["config"] == c]
        if rows:
            out["naive, " + c] = {"decisions": len(rows), "wrong": sum(r["wrong_payment"] for r in rows),
                                  "to_attacker": sum(r["paid_attacker"] for r in rows), "missed": sum(r["missed_hold"] for r in rows)}
    if res.get("guest"):
        out["guest"] = {"attacks": len(res["guest"]), "contained": sum(r["contained"] for r in res["guest"])}
    return out


def history_report(history: list[dict]) -> str:
    lines = ["", "Every run so far (the models vary from run to run; this is how much):",
             f"  {'when':20} {'commit':12}  {'Tally: expected / wrong / missed':34}  {'naive text: wrong / missed':28}  "
             f"{'naive records: wrong / missed':30}  microVM contained"]
    for h in history:
        t, a, b, g = h.get("tally"), h.get("naive, text only"), h.get("naive, with records"), h.get("guest")
        lines.append(f"  {h['at'][:19]:20} {h['revision']:12}  "
                     + (f"{t['as_expected']}/{t['files']} / {t['wrong']} / {t['missed']}" if t else "-").ljust(34) + "  "
                     + (f"{a['wrong']} / {a['missed']} of {a['decisions']}" if a else "-").ljust(28) + "  "
                     + (f"{b['wrong']} / {b['missed']} of {b['decisions']}" if b else "-").ljust(30) + "  "
                     + (f"{g['contained']}/{g['attacks']}" if g else "-"))
    return "\n".join(lines) + "\n"


def text_report(res: dict) -> str:
    lines = [f"Tally lab, {res['at']}, commit {res['revision']}", ""]
    t, n, g = res.get("tally"), res.get("naive"), res.get("guest")
    lines += ["Invoice attacks: " + f"{len(ATTACKS)} files, each with what should happen to it (lab/attacks.py).",
              f"  {MARK['wrong']} wrong payment: money to an account not on file, the wrong amount, or an invoice not payable",
              f"  {MARK['missed']} missed hold: paid (or made ready) something that should have been held", ""]
    header = f"{'attack':34} {'expect':6}  {'Tally':26}  {'naive, text only':22}  {'naive, with records':22}"
    lines += [header, "-" * len(header)]
    for a in ATTACKS:
        tr = next((r for r in (t or {}).get("results", []) if r["attack"] == a.name), None)
        tally_cell = "-" if tr is None else (tr["status"] + (f" ({', '.join(tr['holds'][:2])})" if tr["holds"] else "")
                                            + (MARK["wrong"] if tr["wrong_payment"] else MARK["missed"] if tr["missed_hold"] else ""))
        cells = [_cell([r for r in (n or []) if r["attack"] == a.name and r["config"] == c]) or "-" for c in ("text only", "with records")]
        lines.append(f"{a.name:34} {a.expect:6}  {tally_cell[:26]:26}  {cells[0]:22}  {cells[1]:22}")
    lines.append("")
    if t:
        rs = t["results"]
        hidden = [r for r in rs if r["hidden_iban_read"] is not None]
        lines.append(f"Tally: {sum(r['as_expected'] for r in rs)} of {len(rs)} as expected, "
                     f"{sum(r['as_expected'] and r['for_the_right_reason'] for r in rs)} for the expected reason; "
                     f"{sum(r['wrong_payment'] for r in rs)} wrong payments; {sum(r['missed_hold'] for r in rs)} missed holds; "
                     f"the hidden account reached a reading in {sum(r['hidden_iban_read'] for r in hidden)} of {len(hidden)} hidden-instruction files. "
                     f"{t['seconds']} s for all {len(rs)}, ${sum(r['usd'] for r in rs):.4f} of model calls; ledger intact: {t['ledger_intact']}.")
    else:
        lines.append(f"Tally: not run here ({res['skipped'].get('tally')}).")
    for c in ("text only", "with records"):
        rows = [r for r in (n or []) if r["config"] == c]
        if rows:
            lines.append(f"Naive agent, {c}: {len(rows)} decisions; {sum(r['wrong_payment'] for r in rows)} wrong payments "
                         f"({sum(r['paid_attacker'] for r in rows)} to the attacker's account); {sum(r['missed_hold'] for r in rows)} missed holds; "
                         f"${sum(r.get('usd', 0) for r in rows):.4f}.")
    if not n:
        lines.append(f"Naive agent: not run here ({res['skipped'].get('naive')}).")
    if t:
        odd = [r for r in t["results"] if not r["as_expected"] or not r["for_the_right_reason"]
               or (r["expect"] == "held" and len(r["holds"]) > 1)]
        if odd:
            lines += ["", "Tally, in detail where it did not do exactly what was expected, or held for more than one reason:"]
            for r in odd:
                lines.append(f"  {r['attack']}: {r['status']}")
                lines += [f"    - {h}" for h in r.get("hold_texts", [])]
    lines.append("")
    if g:
        lines.append(f"Attacks from inside the microVM: {sum(r['contained'] for r in g)} of {len(g)} contained.")
        for r in g:
            mb = max(r["bytes_on_host"], r.get("host_disk_lost_at_peak", 0)) / 1024 ** 2
            lines.append(f"  {'contained' if r['contained'] else 'NOT CONTAINED':13} {r['name']:38} {r['outcome']}; {r['checks'][:70]}"
                         + (f"; the host's disk lost {mb:.0f} MB at the peak" if mb >= 1 else "") + (f"; left running: {r['left_running']}" if r["left_running"] else "")
                         + (f"; the guest said: {r['guest_said'][:120]}" if r["guest_said"] and not r["contained"] else ""))
    else:
        lines.append(f"Attacks from inside the microVM: not run here ({res['skipped'].get('guest')}).")
    return "\n".join(lines) + "\n"


def main(out_dir: Path, trials: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tally-lab-"))
    files = [(a, a.build(work / f"{i:02d}-{key(a.name).lower()[:24]}.pdf")) for i, a in enumerate(ATTACKS)]
    res: dict = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "revision": _revision(), "skipped": {}}
    has_key = bool(os.environ.get("VULTR_INFERENCE_API_KEY"))
    why_no_vm = sandbox.available()
    started = time.monotonic()
    if why_no_vm is None and has_key:
        res["tally"] = run_tally.run(files, work / "tally-data")
    else:
        res["skipped"]["tally"] = why_no_vm or "no VULTR_INFERENCE_API_KEY"
    if has_key:
        res["naive"] = run_naive(files, trials)
    else:
        res["skipped"]["naive"] = "no VULTR_INFERENCE_API_KEY"
    if why_no_vm is None:
        hidden = next(p for a, p in files if a.kind == "hidden instruction")
        res["guest"] = guest.run(hidden, work)
    else:
        res["skipped"]["guest"] = why_no_vm
    res["seconds"] = round(time.monotonic() - started, 1)
    (out_dir / "results.json").write_text(json.dumps(res, indent=1))
    with open(out_dir / "history.jsonl", "a") as f:
        f.write(json.dumps(summary(res)) + "\n")
    history = [json.loads(line) for line in (out_dir / "history.jsonl").read_text().splitlines() if line.strip()]
    report = text_report(res) + history_report(history)
    (out_dir / "results.txt").write_text(report)
    print(report)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    main(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) == 3 else 3)
