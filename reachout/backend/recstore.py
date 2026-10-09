"""Keeping call recordings in the database (SQLite table or MongoDB collection `recording_audio`,
whichever `store` uses), one per call, keyed by the call id. The audio comes from `recwire.py`,
which records the web phone's calls. A recipient's `recording_url` is "app:<call id>" for its
latest recorded call ("app" alone marks an older recording kept under the recipient's own id).

If RECORDINGS_KEY is set (a Fernet key: python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())") the audio is encrypted before it is written; without it
the audio is stored as recorded, so rely on database encryption at rest. RECORDINGS_SAVE=0 turns
recording off. Recordings are personal data: they are deleted with their campaign, and only played
back behind the PIN (backend/recordings.py).
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from . import store

log = logging.getLogger("reachout.recstore")
MAX_BYTES = 15 * 1024 * 1024  # also under MongoDB's 16 MB document limit
APP_URL = "app"  # recording_url prefix for audio recorded here (there is no link to fetch)


def saving() -> bool:
    return os.getenv("RECORDINGS_SAVE", "1") != "0"


def wanted(campaign: dict) -> bool:
    """Whether this campaign's calls are recorded. RECORD_CALLS=all (default) records every web-phone call, because
    the builder no longer has a "Record calls" option; RECORD_CALLS=campaign follows each campaign's own flag."""
    if not saving():
        return False
    return os.getenv("RECORD_CALLS", "all").lower() != "campaign" or bool(campaign.get("record"))


def handling(campaign: dict) -> dict:
    """The "Recordings" line shown on the campaign pages: what is actually done with call audio."""
    if not wanted(campaign):
        return {"provider": "Not recorded", "note": "No call audio is kept"}
    return {"provider": "This app's database",
            "note": "Both sides of each call are saved" + (", encrypted" if os.getenv("RECORDINGS_KEY") else "")
                    + ", played back only after unlocking with the PIN, and deleted with the campaign"
                    + ("; each recording goes to Google (Gemini) once to note what was asked and answered"
                       if os.getenv("CALL_ANALYSIS", "1") != "0" and os.getenv("GEMINI_API_KEY") else "")}


def url_for(call_id: str) -> str:
    return f"{APP_URL}:{call_id}"


def key_for(recipient: dict) -> str | None:
    """The stored recording behind a recipient's recording_url, if it is one of ours."""
    url = recipient.get("recording_url") or ""
    if url == APP_URL:
        return recipient["id"]
    return url[len(APP_URL) + 1:] if url.startswith(APP_URL + ":") else None


def _fernet() -> Fernet | None:
    key = os.getenv("RECORDINGS_KEY")
    return Fernet(key.encode()) if key else None


def save(call_id: str, data: bytes, content_type: str, campaign_id: str | None = None,
         recipient_id: str | None = None) -> str | None:
    """Encrypts (if a key is set) and stores one call's recording. None if saved, else why not."""
    if len(data) > MAX_BYTES:
        log.warning("recording for %s is %d bytes: too large to store", call_id, len(data))
        return "too long to store"
    try:
        f = _fernet()
    except ValueError:
        log.error("RECORDINGS_KEY is not a valid Fernet key: recordings are NOT being saved")
        return "RECORDINGS_KEY is not a valid key"
    store.save_recording(call_id, f.encrypt(data) if f else data, content_type, bool(f), campaign_id, recipient_id)
    return None


def load(call_id: str) -> tuple[bytes, str] | None:
    """The saved audio and its content type, or None if nothing usable is stored."""
    got = store.load_recording(call_id)
    if not got:
        return None
    data, content_type, encrypted = got
    if not encrypted:
        return data, content_type
    try:
        f = _fernet()
        return (f.decrypt(data), content_type) if f else None
    except (ValueError, InvalidToken):
        log.error("saved recording for %s cannot be decrypted with the current RECORDINGS_KEY", call_id)
        return None
