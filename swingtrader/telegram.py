"""Telegram alerts: push every desktop alert to your phone too.

Wired into ``alerts._toast`` so 0DTE entries/exits, pattern matches, swing
signals and reversals all arrive in Telegram -- the point being you get them
when you're away from the laptop (exactly the unattended-trading case).

Setup (once):
  1. In Telegram, message @BotFather -> /newbot -> copy the bot TOKEN.
  2. Send your new bot any message (e.g. "hi") so it has a chat to reply to.
  3. In the app (Laptop config -> Telegram alerts) paste the token, click
     "Detect chat ID", then "Send test".

Credentials live in telegram.json (gitignored, like alpaca.json). Sending is
best-effort and never raises, so a Telegram outage can't disrupt trading.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

PATH = Path(__file__).resolve().parent.parent / "telegram.json"
API = "https://api.telegram.org"


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")


def config() -> dict:
    """Non-secret status for the UI (never returns the token itself)."""
    d = _load()
    return {
        "token_set": bool(d.get("token")),
        "chat_id": d.get("chat_id", ""),
        "enabled": bool(d.get("enabled", True)),
        "configured": bool(d.get("token") and d.get("chat_id")),
    }


def configured() -> bool:
    d = _load()
    return bool(d.get("token") and d.get("chat_id") and d.get("enabled", True))


def save(token=None, chat_id=None, enabled=None) -> dict:
    d = _load()
    if token is not None:
        token = str(token).strip()
        if token:                      # blank submit keeps the existing token
            d["token"] = token
    if chat_id is not None:
        d["chat_id"] = str(chat_id).strip()
    if enabled is not None:
        d["enabled"] = bool(enabled)
    _save(d)
    return config()


def detect_chat_id(token=None) -> str | None:
    """Read the bot's recent updates and return the chat id of the latest
    message -- so the user just messages the bot, then clicks Detect."""
    tok = (str(token).strip() if token else "") or _load().get("token", "")
    if not tok:
        return None
    try:
        data = requests.get(f"{API}/bot{tok}/getUpdates", timeout=10).json()
    except Exception as exc:
        print(f"[telegram] getUpdates failed: {exc}")
        return None
    for upd in reversed(data.get("result", []) or []):
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
        cid = ((msg or {}).get("chat") or {}).get("id")
        if cid is not None:
            return str(cid)
    return None


def send(title: str, body: str = "", token=None, chat_id=None) -> bool:
    """Best-effort send; returns True on success, never raises."""
    d = _load()
    tok = (str(token).strip() if token else "") or d.get("token", "")
    cid = (str(chat_id).strip() if chat_id else "") or d.get("chat_id", "")
    if not (tok and cid):
        return False
    text = f"*{title}*\n{body}" if body else f"*{title}*"
    try:
        r = requests.post(
            f"{API}/bot{tok}/sendMessage", timeout=10,
            json={"chat_id": cid, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
        )
        if not r.ok:
            print(f"[telegram] send failed: HTTP {r.status_code} {r.text[:120]}")
        return r.ok
    except Exception as exc:
        print(f"[telegram] send error: {exc}")
        return False


import re


def strategy_catalog() -> list[dict]:
    """The trading strategies you can filter Telegram alerts by -- the same 0DTE
    and swing strategies used elsewhere, plus Pattern lab. (No analyzer/misc/TV.)"""
    from .zerodte import STRATEGY_LABELS
    from .strategies import STRATEGIES
    out = [{"id": f"0dte:{k}", "group": "0DTE", "label": v} for k, v in STRATEGY_LABELS.items()]
    out += [{"id": f"swing:{k}", "group": "Swing", "label": s.label} for k, s in STRATEGIES.items()]
    out.append({"id": "pattern", "group": "Pattern", "label": "Pattern lab"})
    return out


def strategies() -> list[dict]:
    """Catalog merged with the saved per-strategy filter (enabled + tickers)."""
    f = _load().get("filters", {})
    res = []
    for c in strategy_catalog():
        cf = f.get(c["id"], {})
        res.append({**c, "enabled": bool(cf.get("enabled", True)),
                    "tickers": cf.get("tickers", "")})
    return res


def set_filter(strat_id, enabled=None, tickers=None) -> list[dict]:
    d = _load()
    cf = d.setdefault("filters", {}).setdefault(str(strat_id), {})
    if enabled is not None:
        cf["enabled"] = bool(enabled)
    if tickers is not None:
        cf["tickers"] = str(tickers).strip()
    _save(d)
    return strategies()


def allowed(strat_id, symbol=None) -> bool:
    """Should an alert for this strategy/symbol be pushed to Telegram? Unknown
    strategies default to on; a set ticker list restricts to those symbols."""
    cf = _load().get("filters", {}).get(str(strat_id))
    if cf is None:
        return True
    if not cf.get("enabled", True):
        return False
    tickers = {t.upper() for t in re.split(r"[,\s]+", cf.get("tickers", "") or "") if t.strip()}
    if tickers and symbol and str(symbol).upper() not in tickers:
        return False
    return True


def test(token=None, chat_id=None) -> bool:
    return send(
        "Swing Trade Tracker",
        "✅ Telegram alerts connected. You'll get trade entries, exits and "
        "pattern matches here.",
        token, chat_id,
    )
