"""SQLite persistence (stdlib only). One connection per operation; WAL so the dialer,
simulator and API can read and write together.

Full phone numbers live only in `recipients.phone` (needed to dial) and never leave this
module unmasked: use `public_recipient` for anything returned by the API.
Phase 6 adds encryption at rest and retention on top of this schema.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .exotel import mask

DB_PATH = os.getenv("REACHOUT_DB", "reachout.db")

LANGUAGES = {"en": "English", "hi": "Hindi", "mr": "Marathi", "ta": "Tamil", "kn": "Kannada",
             "ml": "Malayalam"}
ANSWERED = ("confirmed", "declined", "rescheduled")
NON_RESPONDER = ("voicemail", "no_answer")
OUTCOMES = ("confirmed", "rescheduled", "declined", "voicemail", "no_answer", "pending")

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  languages TEXT NOT NULL, segments TEXT NOT NULL, started_at TEXT NOT NULL,
  handling TEXT NOT NULL, event TEXT NOT NULL DEFAULT '{}', scripts TEXT NOT NULL DEFAULT '{}',
  retry TEXT NOT NULL DEFAULT '{}', record INTEGER NOT NULL DEFAULT 0, escalation INTEGER NOT NULL DEFAULT 0,
  simulated INTEGER NOT NULL DEFAULT 0, sim TEXT NOT NULL DEFAULT '{}',
  voice TEXT NOT NULL DEFAULT '', audio_ready INTEGER NOT NULL DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS recipients (
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  name TEXT NOT NULL, phone TEXT NOT NULL, language TEXT NOT NULL, segment TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT 'pending', channel TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  retrying INTEGER NOT NULL DEFAULT 0, in_flight INTEGER NOT NULL DEFAULT 0,
  call_sid TEXT, last_attempt_at TEXT, recording_url TEXT, pickup REAL
);
CREATE INDEX IF NOT EXISTS recipients_campaign ON recipients(campaign_id);
CREATE INDEX IF NOT EXISTS recipients_call ON recipients(call_sid);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
  call_sid TEXT, at TEXT NOT NULL, answered INTEGER NOT NULL, outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_at ON calls(at);
CREATE TABLE IF NOT EXISTS access_log (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, target TEXT, client TEXT
);
"""
_JSON = ("languages", "segments", "handling", "event", "scripts", "retry", "sim")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# Columns added after the first release; init() adds them to older databases.
_ADDED = {"campaigns": [("voice", "TEXT NOT NULL DEFAULT ''"), ("audio_ready", "INTEGER NOT NULL DEFAULT 0"),
                        ("note", "TEXT"), ("agent_provider", "TEXT NOT NULL DEFAULT 'elevenlabs'")]}


def init() -> None:
    with tx() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        for table, cols in _ADDED.items():
            have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _campaign(row: sqlite3.Row) -> dict:
    c = dict(row)
    for k in _JSON:
        c[k] = json.loads(c[k])
    for k in ("record", "escalation", "simulated", "audio_ready"):
        c[k] = bool(c[k])
    return c


def has_campaigns() -> bool:
    with tx() as db:
        return db.execute("SELECT 1 FROM campaigns LIMIT 1").fetchone() is not None


def campaigns() -> list[dict]:
    with tx() as db:
        return [_campaign(r) for r in db.execute("SELECT * FROM campaigns ORDER BY started_at DESC")]


def campaign(cid: str) -> dict | None:
    with tx() as db:
        row = db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
    return _campaign(row) if row else None


def recipients(cid: str | None = None) -> list[dict]:
    with tx() as db:
        q = "SELECT * FROM recipients" + (" WHERE campaign_id = ?" if cid else "") + " ORDER BY rowid"
        return [dict(r) for r in db.execute(q, (cid,) if cid else ())]


def recipient(rid: str) -> dict | None:
    with tx() as db:
        row = db.execute("SELECT * FROM recipients WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def recipient_by_call(call_sid: str) -> dict | None:
    with tx() as db:
        row = db.execute("SELECT * FROM recipients WHERE call_sid = ? AND in_flight = 1", (call_sid,)).fetchone()
    return dict(row) if row else None


def insert_campaign(c: dict, recs: list[dict], calls: list[dict] = ()) -> None:
    c = {k: json.dumps(v) if k in _JSON else v for k, v in c.items()}
    with tx() as db:
        db.execute(f"INSERT INTO campaigns ({', '.join(c)}) VALUES ({', '.join('?' * len(c))})", list(c.values()))
        if recs:
            cols = list(recs[0])
            db.executemany(f"INSERT INTO recipients ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           [[r[k] for k in cols] for r in recs])
        db.executemany("INSERT INTO calls (campaign_id, recipient_id, call_sid, at, answered, outcome) "
                       "VALUES (:campaign_id, :recipient_id, :call_sid, :at, :answered, :outcome)", calls)


def set_status(cid: str, status: str) -> None:
    with tx() as db:
        db.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, cid))


def log_call(db: sqlite3.Connection, r: dict | sqlite3.Row, outcome: str, at: str | None = None) -> None:
    db.execute("INSERT INTO calls (campaign_id, recipient_id, call_sid, at, answered, outcome) VALUES (?, ?, ?, ?, ?, ?)",
               (r["campaign_id"], r["id"], r["call_sid"], at or now_iso(), int(outcome in ANSWERED), outcome))


def calls_since(iso: str) -> list[dict]:
    with tx() as db:
        return [dict(r) for r in db.execute("SELECT campaign_id, at, answered FROM calls WHERE at >= ?", (iso,))]


def log_access(action: str, target: str | None, client: str | None) -> None:
    with tx() as db:
        db.execute("INSERT INTO access_log (at, action, target, client) VALUES (?, ?, ?, ?)",
                   (now_iso(), action, target, client))


def mask_phone(phone: str) -> str:
    if phone.startswith("+91") and len(phone) == 13:
        return f"+91 {phone[3]}•••• ••{phone[-2:]}"
    return mask(phone)


def public_recipient(r: dict) -> dict:
    return {
        "id": r["id"], "name": r["name"], "phone": mask_phone(r["phone"]),
        "language": LANGUAGES.get(r["language"], r["language"]), "segment": r["segment"],
        "outcome": r["outcome"], "channel": r["channel"], "attempts": r["attempts"],
        "retrying": bool(r["retrying"]), "has_recording": bool(r["recording_url"]),
    }
