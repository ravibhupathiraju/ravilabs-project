"""Optional LLM parsing of trade notes via Google Gemini.

The TV strategy tester's regex parser handles table-style notes well; this
module upgrades parsing to a real LLM so messier, free-form recommendations
("I'd wait for it to reclaim the 83s, risk a break of 80, looking for mid
90s") also extract cleanly.

Setup (one minute, free):
  1. Get an API key at https://aistudio.google.com/apikey -- this is free
     with any Google account and separate from a Gemini Pro subscription;
     the free tier's daily allowance vastly exceeds the few tiny requests
     per day this app sends.
  2. Put it in ``gemini.json`` next to webapp.py:  {"api_key": "AIza..."}
     (see gemini.example.json), or set the GEMINI_API_KEY env var.

No key -> ``enabled()`` is False and callers fall back to the regex parser;
nothing else in the app changes. The note text is sent to Google's API when
enabled -- don't paste anything you consider private.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).resolve().parent.parent / "gemini.json"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# First model that answers wins. The -latest aliases always track Google's
# current models, so the chain survives model retirements (pinned names like
# gemini-2.5-flash 404 for new keys once retired).
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.1-flash-lite"]

PROMPT = """You extract trade-plan levels from a written stock recommendation.
Return ONLY a JSON object with exactly these keys:
  ticker: string or null      (the stock symbol the plan is about)
  direction: "long" or "short" or null
  entry_low: number or null   (bottom of the entry/pullback zone; the single entry level if no zone)
  entry_high: number or null  (top of the entry zone; equal to entry_low if a single level)
  stop: number or null        (stop-loss level)
  target: number or null      (first profit target)
  target2: number or null     (second target, if given)
  summary: string             (one short sentence restating the plan)
  warnings: array of strings  (anything ambiguous, missing, or contradictory)

Rules: numbers must be plain numbers, no $ signs or commas. NEVER invent a
level that is not in the note -- use null and add a warning instead. If the
note mentions several tickers, pick the one the plan is actually about and
add a warning naming the others.

NOTE:
"""


def _config() -> dict:
    key = os.environ.get("GEMINI_API_KEY", "")
    model = ""
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            key = cfg.get("api_key") or key
            model = cfg.get("model") or ""
        except (OSError, ValueError) as exc:
            print(f"[llm] cannot read {CONFIG_PATH.name}: {exc}")
    return {"api_key": key, "model": model}


def enabled() -> bool:
    return bool(_config()["api_key"])


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_trade_note(text: str) -> dict | None:
    """Ask Gemini to structure the note. Returns None on any failure so the
    caller can fall back to the regex parser -- a parse must never error out."""
    cfg = _config()
    if not cfg["api_key"] or not text.strip():
        return None
    models = [cfg["model"]] if cfg["model"] else MODELS
    for model in models:
        try:
            resp = requests.post(
                URL.format(model=model),
                params={"key": cfg["api_key"]},
                json={
                    "contents": [{"parts": [{"text": PROMPT + text}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0,
                    },
                },
                timeout=30,
            )
            if resp.status_code == 404:
                continue  # model not on this key/API version; try the next
            resp.raise_for_status()
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(re.sub(r"^```(?:json)?|```$", "",
                                     raw.strip(), flags=re.M).strip())
        except Exception as exc:
            print(f"[llm] {model}: {exc}")
            continue

        out = {
            "ticker": (str(data.get("ticker")).upper().strip()
                       if data.get("ticker") else None),
            "direction": data.get("direction") if data.get("direction")
                         in ("long", "short") else None,
            "entry_low": _num(data.get("entry_low")),
            "entry_high": _num(data.get("entry_high")),
            "stop": _num(data.get("stop")),
            "target": _num(data.get("target")),
            "target2": _num(data.get("target2")),
            "summary": (str(data.get("summary") or "").strip() or None),
            "warnings": [str(w) for w in (data.get("warnings") or [])],
            "source": f"Gemini ({model})",
        }
        if out["entry_low"] and not out["entry_high"]:
            out["entry_high"] = out["entry_low"]
        return out
    return None
