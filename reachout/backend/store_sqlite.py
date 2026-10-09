"""SQLite backend (stdlib only), the default. One connection per operation; WAL so the dialer,
simulator and API can read and write together. Used unless MONGODB_URI is set (see `store`).
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .dbcommon import ANSWERED, BOOL_FIELDS, ago, answers, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  languages TEXT NOT NULL, segments TEXT NOT NULL, started_at TEXT NOT NULL,
  handling TEXT NOT NULL, event TEXT NOT NULL DEFAULT '{}', scripts TEXT NOT NULL DEFAULT '{}',
  retry TEXT NOT NULL DEFAULT '{}', record INTEGER NOT NULL DEFAULT 0, escalation INTEGER NOT NULL DEFAULT 0,
  simulated INTEGER NOT NULL DEFAULT 0, sim TEXT NOT NULL DEFAULT '{}',
  voice TEXT NOT NULL DEFAULT '', audio_ready INTEGER NOT NULL DEFAULT 0, note TEXT,
  questions TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS recipients (
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  name TEXT NOT NULL, phone TEXT NOT NULL, language TEXT NOT NULL, segment TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT 'pending', channel TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  retrying INTEGER NOT NULL DEFAULT 0, in_flight INTEGER NOT NULL DEFAULT 0,
  call_sid TEXT, last_attempt_at TEXT, recording_url TEXT, pickup REAL,
  answers TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS recipients_campaign ON recipients(campaign_id);
CREATE INDEX IF NOT EXISTS recipients_call ON recipients(call_sid);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
  call_sid TEXT, at TEXT NOT NULL, answered INTEGER NOT NULL, outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_at ON calls(at);
CREATE TABLE IF NOT EXISTS recording_audio (
  call_id TEXT PRIMARY KEY, data BLOB NOT NULL, content_type TEXT NOT NULL, encrypted INTEGER NOT NULL DEFAULT 0,
  saved_at TEXT NOT NULL, campaign_id TEXT, recipient_id TEXT
);
CREATE TABLE IF NOT EXISTS call_history (
  id TEXT PRIMARY KEY, campaign_id TEXT, recipient_id TEXT, rang_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_history_rang ON call_history(rang_at);
CREATE INDEX IF NOT EXISTS call_history_campaign ON call_history(campaign_id);
CREATE TABLE IF NOT EXISTS access_log (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, target TEXT, client TEXT
);
"""
_JSON = ("languages", "segments", "handling", "event", "scripts", "retry", "sim", "questions", "chat")
# Columns added after the first release; init() adds them to older databases.
_ADDED = {"campaigns": [("voice", "TEXT NOT NULL DEFAULT ''"), ("audio_ready", "INTEGER NOT NULL DEFAULT 0"),
                        ("note", "TEXT"), ("questions", "TEXT NOT NULL DEFAULT '[]'"),
                        ("agent_provider", "TEXT NOT NULL DEFAULT 'elevenlabs'"),
                        ("provider", "TEXT NOT NULL DEFAULT ''"), ("telephony", "TEXT NOT NULL DEFAULT 'exotel'"),
                        ("system_prompt", "TEXT NOT NULL DEFAULT ''"),
                        ("mode", "TEXT NOT NULL DEFAULT 'live'"), ("chat", "TEXT NOT NULL DEFAULT '[]'"),
                        ("chat_summary", "TEXT NOT NULL DEFAULT ''")],
          "recipients": [("answers", "TEXT NOT NULL DEFAULT '{}'")]}
_DUE = ("campaign_id = ? AND in_flight = 0 AND (outcome = 'pending' "
        "OR (outcome IN ('voicemail', 'no_answer') AND attempts < ? AND last_attempt_at <= ?))")


def _campaign(row: sqlite3.Row) -> dict:
    c = dict(row)
    for k in _JSON:
        c[k] = json.loads(c[k])
    for k in BOOL_FIELDS:
        c[k] = bool(c[k])
    return c


def _log(db: sqlite3.Connection, r: dict | sqlite3.Row, outcome: str, at: str | None = None) -> None:
    db.execute("INSERT INTO calls (campaign_id, recipient_id, call_sid, at, answered, outcome) VALUES (?, ?, ?, ?, ?, ?)",
               (r["campaign_id"], r["id"], r["call_sid"], at or now_iso(), int(outcome in ANSWERED), outcome))


class SqliteStore:
    name = "SQLite"

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("REACHOUT_DB", "reachout.db")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self._tx() as db:
            db.execute("PRAGMA journal_mode=WAL")
            # Recordings were first kept one per recipient; they are now one per call (a retry no longer
            # overwrites the earlier call's audio). Older rows keep their recipient id as the key.
            old = {r["name"] for r in db.execute("PRAGMA table_info(recording_audio)")}
            if "recipient_id" in old and "call_id" not in old:
                db.execute("ALTER TABLE recording_audio RENAME COLUMN recipient_id TO call_id")
                db.execute("ALTER TABLE recording_audio ADD COLUMN campaign_id TEXT")
                db.execute("ALTER TABLE recording_audio ADD COLUMN recipient_id TEXT")
            db.executescript(SCHEMA)
            for table, cols in _ADDED.items():
                have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ---------- reads ----------

    def has_campaigns(self) -> bool:
        with self._tx() as db:
            return db.execute("SELECT 1 FROM campaigns LIMIT 1").fetchone() is not None

    def campaigns(self) -> list[dict]:
        with self._tx() as db:
            return [_campaign(r) for r in db.execute("SELECT * FROM campaigns ORDER BY started_at DESC")]

    def campaign(self, cid: str) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
        return _campaign(row) if row else None

    def recipients(self, cid: str | None = None) -> list[dict]:
        with self._tx() as db:
            q = "SELECT * FROM recipients" + (" WHERE campaign_id = ?" if cid else "") + " ORDER BY rowid"
            return [dict(r) for r in db.execute(q, (cid,) if cid else ())]

    def recipient(self, rid: str) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM recipients WHERE id = ?", (rid,)).fetchone()
        return dict(row) if row else None

    def recipient_by_call(self, call_sid: str) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM recipients WHERE call_sid = ? AND in_flight = 1", (call_sid,)).fetchone()
        return dict(row) if row else None

    def pending_recipients(self, cid: str) -> list[dict]:
        with self._tx() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM recipients WHERE campaign_id = ? AND outcome = 'pending' ORDER BY rowid", (cid,))]

    def calls_since(self, iso: str) -> list[dict]:
        with self._tx() as db:
            return [dict(r) for r in db.execute("SELECT campaign_id, at, answered FROM calls WHERE at >= ?", (iso,))]

    # ---------- campaigns ----------

    def insert_campaign(self, c: dict, recs: list[dict], calls: list[dict] = ()) -> None:
        c = {k: json.dumps(v) if k in _JSON else v for k, v in c.items()}
        with self._tx() as db:
            db.execute(f"INSERT INTO campaigns ({', '.join(c)}) VALUES ({', '.join('?' * len(c))})", list(c.values()))
            if recs:
                cols = list(recs[0])
                db.executemany(f"INSERT INTO recipients ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               [[r[k] for k in cols] for r in recs])
            db.executemany("INSERT INTO calls (campaign_id, recipient_id, call_sid, at, answered, outcome) "
                           "VALUES (:campaign_id, :recipient_id, :call_sid, :at, :answered, :outcome)", calls)

    def set_status(self, cid: str, status: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, cid))

    def delete_campaign(self, cid: str) -> bool:
        """The campaign, its recipients and its call log. False if there was no such campaign."""
        with self._tx() as db:
            db.execute("DELETE FROM calls WHERE campaign_id = ?", (cid,))
            db.execute("DELETE FROM recording_audio WHERE campaign_id = ? OR call_id IN "
                       "(SELECT id FROM recipients WHERE campaign_id = ?)", (cid, cid))  # the second: recordings from before calls had ids
            db.execute("DELETE FROM call_history WHERE campaign_id = ?", (cid,))
            db.execute("DELETE FROM recipients WHERE campaign_id = ?", (cid,))
            return db.execute("DELETE FROM campaigns WHERE id = ?", (cid,)).rowcount > 0

    def reset_campaign(self, cid: str) -> int:
        """Development: forget every call made. Recipients go back to queued with no attempts, answers or recordings; the
        call log, history and saved audio of the campaign are deleted; the campaign is left paused. Returns the recipients reset."""
        with self._tx() as db:
            n = db.execute("UPDATE recipients SET outcome = 'pending', channel = NULL, attempts = 0, retrying = 0, in_flight = 0, "
                           "call_sid = NULL, last_attempt_at = NULL, recording_url = NULL, answers = '{}' WHERE campaign_id = ?", (cid,)).rowcount
            db.execute("DELETE FROM calls WHERE campaign_id = ?", (cid,))
            db.execute("DELETE FROM recording_audio WHERE campaign_id = ? OR call_id IN "
                       "(SELECT id FROM recipients WHERE campaign_id = ?)", (cid, cid))
            db.execute("DELETE FROM call_history WHERE campaign_id = ?", (cid,))
            db.execute("UPDATE campaigns SET status = 'paused', note = NULL WHERE id = ?", (cid,))
        return n

    def pause_running(self, cid: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE campaigns SET status = 'paused' WHERE id = ? AND status = 'running'", (cid,))

    def refresh_status(self, c: dict, auto_retry: bool = True) -> None:
        """Running -> completed once nobody is queued, on a call, or due an automatic retry."""
        if c["status"] != "running":
            return
        max_attempts = c["retry"].get("max_attempts", 1) if auto_retry else 0
        with self._tx() as db:
            left = db.execute(
                "SELECT 1 FROM recipients WHERE campaign_id = ? AND (in_flight = 1 OR outcome = 'pending' "
                "OR (outcome IN ('voicemail', 'no_answer') AND attempts < ?)) LIMIT 1", (c["id"], max_attempts)).fetchone()
            if not left:
                db.execute("UPDATE campaigns SET status = 'completed' WHERE id = ?", (c["id"],))

    def requeue(self, cid: str) -> int:
        """Non-responders back to pending for another try; reopens a finished campaign."""
        with self._tx() as db:
            n = db.execute("UPDATE recipients SET outcome = 'pending', channel = NULL, retrying = 1 "
                           "WHERE campaign_id = ? AND in_flight = 0 AND outcome IN ('voicemail', 'no_answer')",
                           (cid,)).rowcount
            if n:
                db.execute("UPDATE campaigns SET status = 'running' WHERE id = ? AND status = 'completed'", (cid,))
        return n

    # ---------- contacts of an existing campaign ----------

    def add_recipients(self, recs: list[dict]) -> None:
        if not recs:
            return
        cols = list(recs[0])
        with self._tx() as db:
            db.executemany(f"INSERT INTO recipients ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           [[r[k] for k in cols] for r in recs])

    def update_recipient(self, rid: str, fields: dict) -> None:
        """name, language and segment only (the phone number is the identity: remove and add instead)."""
        fields = {k: v for k, v in fields.items() if k in ("name", "language", "segment")}
        if fields:
            with self._tx() as db:
                db.execute(f"UPDATE recipients SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), rid])

    def remove_recipients(self, cid: str, ids: list[str]) -> int:
        """Deletes these contacts of the campaign with their call log, history and recordings (a person asked to be
        removed must not leave audio behind). Contacts on a call right now are skipped. Returns how many went."""
        n = 0
        with self._tx() as db:
            for rid in ids:
                if not db.execute("SELECT 1 FROM recipients WHERE id = ? AND campaign_id = ? AND in_flight = 0", (rid, cid)).fetchone():
                    continue
                db.execute("DELETE FROM calls WHERE recipient_id = ?", (rid,))
                db.execute("DELETE FROM recording_audio WHERE recipient_id = ? OR call_id = ?", (rid, rid))
                db.execute("DELETE FROM call_history WHERE recipient_id = ?", (rid,))
                n += db.execute("DELETE FROM recipients WHERE id = ?", (rid,)).rowcount
        return n

    # ---------- voice preparation ----------

    def pause_preparing(self, cid: str, note: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE campaigns SET status = 'paused', note = ? WHERE id = ? AND status = 'preparing'", (note, cid))

    def finish_audio(self, cid: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE campaigns SET audio_ready = 1, note = NULL WHERE id = ?", (cid,))
            db.execute("UPDATE campaigns SET status = 'running' WHERE id = ? AND status = 'preparing'", (cid,))

    def update_campaign(self, cid: str, fields: dict) -> None:
        """Sets the given campaign columns (JSON ones are encoded). Callers pass only known fields."""
        vals = [json.dumps(v) if k in _JSON else int(v) if isinstance(v, bool) else v for k, v in fields.items()]
        with self._tx() as db:
            db.execute(f"UPDATE campaigns SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*vals, cid])

    def set_note(self, cid: str, note: str | None) -> None:
        with self._tx() as db:
            db.execute("UPDATE campaigns SET note = ? WHERE id = ?", (note, cid))

    # ---------- dialing ----------

    def expire_stale(self, before: str) -> None:
        """Attempts with no status callback since `before` are given up on."""
        with self._tx() as db:
            for r in db.execute("SELECT * FROM recipients WHERE in_flight = 1 AND last_attempt_at < ?", (before,)).fetchall():
                outcome = r["outcome"] if r["outcome"] != "pending" else "no_answer"
                db.execute("UPDATE recipients SET in_flight = 0, outcome = ? WHERE id = ?", (outcome, r["id"]))
                _log(db, r, outcome)

    def claim_due(self, running: list[dict], concurrency: int) -> list[tuple[dict, dict]]:
        """Marks up to `concurrency` (minus calls already in flight) due recipients as in flight and
        returns (campaign, recipient as it was before the claim) pairs."""
        batch: list[tuple[dict, dict]] = []
        with self._tx() as db:
            free = concurrency - db.execute("SELECT COUNT(*) FROM recipients WHERE in_flight = 1").fetchone()[0]
            for c in running:
                if free <= 0:
                    break
                rows = db.execute(f"SELECT * FROM recipients WHERE {_DUE} ORDER BY attempts, rowid LIMIT ?",
                                  (c["id"], c["retry"].get("max_attempts", 1),
                                   ago(hours=c["retry"].get("gap_hours", 4)), free)).fetchall()
                for r in rows:
                    db.execute("UPDATE recipients SET in_flight = 1, attempts = attempts + 1, retrying = 0, outcome = 'pending', "
                               "channel = NULL, call_sid = NULL, recording_url = NULL, last_attempt_at = ? WHERE id = ?",
                               (now_iso(), r["id"]))
                    batch.append((c, dict(r)))
                    free -= 1
        return batch

    def set_call_sid(self, rid: str, sid: str | None) -> None:
        with self._tx() as db:
            db.execute("UPDATE recipients SET call_sid = COALESCE(call_sid, ?) WHERE id = ?", (sid, rid))

    def mark_unreachable(self, r: dict) -> None:
        """This number is the problem: count the attempt as unreachable."""
        with self._tx() as db:
            db.execute("UPDATE recipients SET in_flight = 0, outcome = 'no_answer' WHERE id = ?", (r["id"],))
            _log(db, r, "no_answer")

    def restore_recipient(self, r: dict) -> None:
        """Put a recipient back exactly as it was before it was claimed."""
        with self._tx() as db:
            db.execute("UPDATE recipients SET in_flight = 0, attempts = ?, outcome = ?, retrying = ?, channel = ?, "
                       "call_sid = ?, recording_url = ?, last_attempt_at = ? WHERE id = ?",
                       (r["attempts"], r["outcome"], r["retrying"], r["channel"], r["call_sid"], r["recording_url"],
                        r["last_attempt_at"], r["id"]))

    def finish_call(self, rid: str | None, sid: str | None, completed: bool, recording: str | None) -> tuple[dict, str] | None:
        """Exotel's final status for a call in flight: (recipient before, outcome), None if unknown or already done.
        Answered with no digit pressed counts as voicemail."""
        with self._tx() as db:
            r = db.execute("SELECT * FROM recipients WHERE id = ? OR (call_sid IS NOT NULL AND call_sid = ?)",
                           (rid, sid)).fetchone()
            if not r or not r["in_flight"]:
                return None
            outcome = r["outcome"]
            if outcome == "pending":
                outcome = "voicemail" if completed else "no_answer"
            db.execute("UPDATE recipients SET in_flight = 0, outcome = ?, call_sid = COALESCE(call_sid, ?), "
                       "recording_url = COALESCE(?, recording_url) WHERE id = ?", (outcome, sid, (recording if completed else None) or None, r["id"]))
            _log(db, dict(r) | {"call_sid": r["call_sid"] or sid}, outcome)
        return dict(r), outcome

    def mark_outcome(self, call_sid: str, outcome: str | None, channel: str) -> bool:
        """outcome=None only marks the channel, e.g. when the caller is handed to the agent."""
        with self._tx() as db:
            cur = db.execute("UPDATE recipients SET outcome = COALESCE(?, outcome), channel = ? "
                             "WHERE call_sid = ? AND in_flight = 1", (outcome, channel, call_sid))
        return cur.rowcount > 0

    def add_answer(self, call_sid: str, question_id: str, digit: str) -> bool:
        with self._tx() as db:
            row = db.execute("SELECT id, answers FROM recipients WHERE call_sid = ? AND in_flight = 1", (call_sid,)).fetchone()
            if not row:
                return False
            db.execute("UPDATE recipients SET answers = ? WHERE id = ?",
                       (json.dumps(answers(dict(row)) | {question_id: digit}), row["id"]))
        return True

    def set_answer(self, rid: str, question_id: str, digit: str) -> None:
        """Sets one recipient's saved answer to a question (used to correct what the agent saved)."""
        with self._tx() as db:
            row = db.execute("SELECT answers FROM recipients WHERE id = ?", (rid,)).fetchone()
            if row:
                db.execute("UPDATE recipients SET answers = ? WHERE id = ?",
                           (json.dumps(answers(dict(row)) | {question_id: digit}), rid))

    def save_result(self, r: dict, outcome: str) -> None:
        """Simulated call result: store the recipient's new state and log the call."""
        with self._tx() as db:
            db.execute("UPDATE recipients SET outcome = ?, channel = ?, attempts = ?, retrying = 0, last_attempt_at = ?, "
                       "recording_url = ?, answers = ? WHERE id = ?",
                       (r["outcome"], r["channel"], r["attempts"], r["last_attempt_at"], r["recording_url"], r["answers"], r["id"]))
            _log(db, r, outcome)

    # ---------- saved recordings ----------

    def set_recording_url(self, rid: str, url: str) -> None:
        """Marks a recipient as having a recording, unless it already has one."""
        with self._tx() as db:
            db.execute("UPDATE recipients SET recording_url = ? WHERE id = ? AND recording_url IS NULL", (url, rid))

    def save_recording(self, key: str, data: bytes, content_type: str, encrypted: bool,
                       campaign_id: str | None = None, recipient_id: str | None = None) -> None:
        """One call's audio, keyed by its call id."""
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO recording_audio (call_id, data, content_type, encrypted, saved_at, campaign_id, "
                       "recipient_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (key, data, content_type, int(encrypted), now_iso(), campaign_id, recipient_id))

    def load_recording(self, key: str) -> tuple[bytes, str, bool] | None:
        """(audio as stored, content type, encrypted) for a call id, or None."""
        with self._tx() as db:
            row = db.execute("SELECT data, content_type, encrypted FROM recording_audio WHERE call_id = ?", (key,)).fetchone()
        return (bytes(row["data"]), row["content_type"], bool(row["encrypted"])) if row else None

    def recordings_present(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        with self._tx() as db:
            return {r[0] for r in db.execute(
                f"SELECT call_id FROM recording_audio WHERE call_id IN ({', '.join('?' * len(keys))})", list(keys))}

    # ---------- call history ----------

    def recipient_for_call(self, call_sid: str) -> dict | None:
        """The recipient a call belongs to, in flight or not."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM recipients WHERE call_sid = ?", (call_sid,)).fetchone()
        return dict(row) if row else None

    def save_call(self, call: dict) -> None:
        """Creates or replaces one call's history record (`id` is the call id)."""
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO call_history (id, campaign_id, recipient_id, rang_at, data) VALUES (?, ?, ?, ?, ?)",
                       (call["id"], call.get("campaign_id"), call.get("recipient_id"), call.get("rang_at") or now_iso(),
                        json.dumps(call)))

    def get_call(self, call_id: str) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT data FROM call_history WHERE id = ?", (call_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_calls(self, campaign_id: str | None = None, limit: int = 200) -> list[dict]:
        """Newest first."""
        with self._tx() as db:
            q = "SELECT data FROM call_history" + (" WHERE campaign_id = ?" if campaign_id else "") + " ORDER BY rang_at DESC LIMIT ?"
            return [json.loads(r["data"]) for r in db.execute(q, ((campaign_id,) if campaign_id else ()) + (limit,))]

    # ---------- access log ----------

    def log_access(self, action: str, target: str | None, client: str | None) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO access_log (at, action, target, client) VALUES (?, ?, ?, ?)",
                       (now_iso(), action, target, client))
