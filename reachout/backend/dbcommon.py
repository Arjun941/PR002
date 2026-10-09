"""Constants and helpers shared by both storage backends (SQLite and MongoDB) and re-exported by
`store`. Full phone numbers live only in the recipient records (needed to dial) and never leave
the store unmasked: use `public_recipient` for anything returned by the API.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone



def mask(number: str) -> str:
    """Never log or return a full phone number."""
    digits = number.strip()
    return digits[:3] + "•" * max(len(digits) - 5, 0) + digits[-2:] if len(digits) > 5 else "•••"

LANGUAGES = {"en": "English", "hi": "Hindi", "mr": "Marathi", "ta": "Tamil", "kn": "Kannada",
             "ml": "Malayalam"}
ANSWERED = ("confirmed", "declined", "rescheduled")
NON_RESPONDER = ("voicemail", "no_answer")
OUTCOMES = ("confirmed", "rescheduled", "declined", "voicemail", "no_answer", "pending")
BOOL_FIELDS = ("record", "escalation", "simulated", "audio_ready")  # campaign flags, bool once loaded


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def mask_phone(phone: str) -> str:
    if phone.startswith("+91") and len(phone) == 13:
        return f"+91 {phone[3]}•••• ••{phone[-2:]}"
    return mask(phone)


def answers(r: dict) -> dict[str, str]:
    """Follow-up answers as {question id: key pressed}."""
    try:
        return json.loads(r.get("answers") or "{}")
    except ValueError:
        return {}


def public_recipient(r: dict, questions: list[dict] = ()) -> dict:
    """questions (the campaign's) turn the keys pressed into option labels."""
    got = answers(r)
    labels = {q["id"]: q["options"] for q in questions}
    return {
        "id": r["id"], "name": r["name"], "phone": mask_phone(r["phone"]),
        "language": LANGUAGES.get(r["language"], r["language"]), "segment": r["segment"],
        "outcome": r["outcome"], "channel": r["channel"], "attempts": r["attempts"],
        "retrying": bool(r["retrying"]), "has_recording": bool(r["recording_url"]),
        "answers": {qid: labels[qid][int(k) - 1] for qid, k in got.items()
                    if qid in labels and k.isdigit() and 0 < int(k) <= len(labels[qid])},
    }
