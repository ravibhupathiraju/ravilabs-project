"""TV-tab action plan: date-stamped notes, each with an action plan and a status.

A tiny persistent journal so the TV strategy tester can hold day-by-day
observations ("what I saw"), the action to take ("what I'll do"), and where
each stands (open / in progress / done / cancelled). Backed by a JSON file --
the state is a handful of notes, so no database. Mirrors the shape of
tracker.py (seq counter + JSON list) for consistency.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "action_plan.json"
)

# status key -> display label; order is the workflow order shown in dropdowns
STATUSES = {
    "open": "Open",
    "in_progress": "In progress",
    "done": "Done",
    "cancelled": "Cancelled",
}


def _load() -> dict:
    if not os.path.exists(PATH):
        return {"seq": 0, "entries": []}
    try:
        with open(PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        d.setdefault("seq", 0)
        d.setdefault("entries", [])
        return d
    except Exception as exc:
        print(f"[actionplan] could not read {PATH}: {exc}")
        return {"seq": 0, "entries": []}


def _save(d: dict) -> None:
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)


def _today() -> str:
    return datetime.now(NY).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(NY).isoformat(timespec="seconds")


def _clean_status(status: str | None) -> str:
    s = (status or "").strip().lower()
    return s if s in STATUSES else "open"


def add(date: str | None, note: str, plan: str, status: str | None = None) -> dict:
    """Create a new dated entry. note or plan may be empty but not both."""
    note = (note or "").strip()
    plan = (plan or "").strip()
    if not note and not plan:
        raise ValueError("a note or an action plan is required")
    d = _load()
    d["seq"] += 1
    d["entries"].append({
        "id": d["seq"],
        "date": (date or "").strip() or _today(),
        "note": note,
        "plan": plan,
        "status": _clean_status(status),
        "created": _now(),
        "updated": _now(),
    })
    _save(d)
    return list_entries()


def update(entry_id: int, date: str | None = None, note: str | None = None,
           plan: str | None = None, status: str | None = None) -> dict:
    """Patch any subset of an entry's fields; only provided fields change."""
    d = _load()
    row = next((r for r in d["entries"] if r["id"] == entry_id), None)
    if row is None:
        return list_entries()
    if date is not None and date.strip():
        row["date"] = date.strip()
    if note is not None:
        row["note"] = note.strip()
    if plan is not None:
        row["plan"] = plan.strip()
    if status is not None:
        row["status"] = _clean_status(status)
    row["updated"] = _now()
    _save(d)
    return list_entries()


def delete(entry_id: int) -> dict:
    d = _load()
    d["entries"] = [r for r in d["entries"] if r["id"] != entry_id]
    _save(d)
    return list_entries()


def list_entries(date: str | None = None) -> dict:
    """All entries (or just one date's), newest date first, plus the set of
    dates that have entries so the UI can offer date-wise navigation."""
    d = _load()
    entries = d["entries"]
    dates = sorted({r["date"] for r in entries}, reverse=True)
    date = (date or "").strip()
    if date:
        entries = [r for r in entries if r["date"] == date]
    # newest date first; within a date, newest-created first
    entries = sorted(entries, key=lambda r: (r["date"], r.get("created", "")), reverse=True)
    counts = {s: sum(1 for r in d["entries"] if r["status"] == s) for s in STATUSES}
    return {
        "entries": entries,
        "dates": dates,
        "statuses": STATUSES,
        "counts": counts,
        "filter_date": date or None,
    }
