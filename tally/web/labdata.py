"""The lab's published results, shaped for the home page and the lab page. Read-only; the lab writes them (lab/)."""
from __future__ import annotations

import json
from pathlib import Path

SYSTEMS = (("tally", "Tally"), ("naive, text only", "Naive agent, text only"), ("naive, with records", "Naive agent, with records"))


def load(lab_dir: Path) -> tuple[dict | None, list[dict]]:
    """The latest full results, and one summary line per run so far (oldest first). Either may be missing."""
    latest = None
    try:
        latest = json.loads((lab_dir / "results.json").read_text())
    except (OSError, ValueError):
        pass
    history = []
    try:
        for line in (lab_dir / "history.jsonl").read_text().splitlines():
            try:
                history.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return latest, history


def totals(history: list[dict]) -> dict | None:
    """Everything added up over the runs so far, for the headline numbers."""
    runs = [h for h in history if h.get("tally")]
    if not runs:
        return None
    add = lambda key, field: sum((h.get(key) or {}).get(field, 0) for h in runs)  # noqa: E731
    last_guest = next((h["guest"] for h in reversed(runs) if h.get("guest")), None)
    return {
        "runs": len(runs),
        "files": add("tally", "files"), "as_expected": add("tally", "as_expected"),
        "tally_wrong": add("tally", "wrong"), "tally_missed": add("tally", "missed"),
        "hidden_read": add("tally", "hidden_read"), "hidden_files": add("tally", "hidden_files"),
        "naive": {label: {"decisions": add(key, "decisions"), "wrong": add(key, "wrong"), "missed": add(key, "missed"),
                          "to_attacker": add(key, "to_attacker")}
                  for key, label in SYSTEMS[1:]},
        "guest": last_guest,
    }


def _cell(kind: str, text: str, why: str) -> dict:
    return {"kind": kind, "text": text, "why": why}


def grid(latest: dict) -> list[dict]:
    """One row per attack: what Tally did, and each of the naive agent's tries, as status cells."""
    rows = []
    for t in (latest.get("tally") or {}).get("results", []):
        if t["wrong_payment"]:
            tally = _cell("critical", "paid wrongly", "money would go out wrongly")
        elif t["missed_hold"]:
            tally = _cell("serious", "made ready", "should have been held")
        elif t["as_expected"]:
            tally = _cell("good", t["status"], ", ".join(t["holds"]) or "as expected")
        else:
            tally = _cell("warning", t["status"], "held, though it was fine to pay: a safe mistake")
        naive = {}
        for key, _ in SYSTEMS[1:]:
            config = key.removeprefix("naive, ")
            cells = []
            for d in (r for r in latest.get("naive") or [] if r["attack"] == t["attack"] and r["config"] == config):
                if d["action"] == "error":
                    cells.append(_cell("neutral", "error", d.get("why", "")))
                elif d["wrong_payment"]:
                    cells.append(_cell("critical", "paid", "a wrong payment" + (" to the attacker" if d.get("paid_attacker") else "")))
                elif d["missed_hold"]:
                    cells.append(_cell("serious", "paid", "should have held it"))
                elif d["action"] == "pay":
                    cells.append(_cell("good", "paid", "right to pay"))
                elif t["expect"] == "held":
                    cells.append(_cell("good", "held", d.get("why", "")[:160]))
                else:
                    cells.append(_cell("warning", "held", "held, though it was fine to pay: " + d.get("why", "")[:120]))
            naive[key] = cells
        rows.append({"attack": t["attack"], "kind": t["kind"], "expect": t["expect"], "tally": tally, "naive": naive})
    return rows
