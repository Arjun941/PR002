"""Real campaign dialer and the Exotel status callback that turns calls into dashboard data.

Loop: every few seconds, place calls for running (non-simulated) campaigns, at most
DIAL_CONCURRENCY at once and only inside CALL_WINDOW (local time). Each call carries the
recipient id as CustomField; Exotel posts the final status to /api/telephony/status and the
voicebot records the DTMF digit by call sid. Answered with no digit counts as voicemail.
Non-responders are retried automatically per the campaign's retry policy.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import exotel, store
from .store import now_iso

log = logging.getLogger("reachout.dialer")
router = APIRouter()

CONCURRENCY = int(os.getenv("DIAL_CONCURRENCY", "2"))
STALE_MINUTES = 10  # no status callback by then: give up on the attempt
DIGITS = {"1": "confirmed", "2": "declined", "3": "rescheduled"}


def ready() -> bool:
    """Real calls need Exotel plus a public URL (tunnel) and a token for the status callback."""
    return exotel.configured() and bool(os.getenv("PUBLIC_URL")) and bool(os.getenv("WEBHOOK_TOKEN"))


def in_window(now: datetime | None = None) -> bool:
    try:
        start, end = os.getenv("CALL_WINDOW", "09:00-20:00").split("-")
    except ValueError:
        return True
    t = (now or datetime.now()).astimezone().strftime("%H:%M")
    return start.strip() <= t < end.strip()


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def refresh_status(db: sqlite3.Connection, c: dict, auto_retry: bool = True) -> None:
    """Running -> completed once nobody is queued, on a call, or due an automatic retry."""
    if c["status"] != "running":
        return
    max_attempts = c["retry"].get("max_attempts", 1) if auto_retry else 0
    left = db.execute(
        "SELECT 1 FROM recipients WHERE campaign_id = ? AND (in_flight = 1 OR outcome = 'pending' "
        "OR (outcome IN ('voicemail', 'no_answer') AND attempts < ?)) LIMIT 1", (c["id"], max_attempts)).fetchone()
    if not left:
        db.execute("UPDATE campaigns SET status = 'completed' WHERE id = ?", (c["id"],))


def record_dtmf(call_sid: str | None, digit: str) -> bool:
    """Called by the voicebot. Returns True if the digit was a menu choice for a known call."""
    return bool(DIGITS.get(digit)) and record_outcome(call_sid, DIGITS[digit], "keypad")


def record_outcome(call_sid: str | None, outcome: str | None, channel: str) -> bool:
    """Saves an answered call's result (keypad digit, or the voice agent's record_outcome tool).
    outcome=None only marks the channel, e.g. when the caller is handed to the agent."""
    if not call_sid or outcome not in (None, *DIGITS.values()):
        return False
    with store.tx() as db:
        cur = db.execute("UPDATE recipients SET outcome = COALESCE(?, outcome), channel = ? "
                         "WHERE call_sid = ? AND in_flight = 1", (outcome, channel, call_sid))
    return cur.rowcount > 0


def _expire_stale(db: sqlite3.Connection) -> None:
    for r in db.execute("SELECT * FROM recipients WHERE in_flight = 1 AND last_attempt_at < ?",
                        (_ago(minutes=STALE_MINUTES),)).fetchall():
        outcome = r["outcome"] if r["outcome"] != "pending" else "no_answer"
        db.execute("UPDATE recipients SET in_flight = 0, outcome = ? WHERE id = ?", (outcome, r["id"]))
        store.log_call(db, r, outcome)


def _due(db: sqlite3.Connection, c: dict, limit: int) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM recipients WHERE campaign_id = ? AND in_flight = 0 AND (outcome = 'pending' "
        "OR (outcome IN ('voicemail', 'no_answer') AND attempts < ? AND last_attempt_at <= ?)) "
        "ORDER BY attempts, rowid LIMIT ?",
        (c["id"], c["retry"].get("max_attempts", 1), _ago(hours=c["retry"].get("gap_hours", 4)), limit)).fetchall()


async def _dial(c: dict, r: dict) -> bool:
    """Place one call. False means the campaign was paused (config/network) and the attempt undone."""
    cb = f"{os.environ['PUBLIC_URL'].rstrip('/')}/api/telephony/status?token={os.environ['WEBHOOK_TOKEN']}"
    try:
        res = await exotel.place_call(r["phone"], custom_field=r["id"], status_callback=cb, record=c["record"])
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500 and exc.response.status_code not in (401, 403, 429):
            log.warning("call rejected for %s (HTTP %s)", r["id"], exc.response.status_code)
            with store.tx() as db:  # this number is the problem: count it as unreachable
                db.execute("UPDATE recipients SET in_flight = 0, outcome = 'no_answer' WHERE id = ?", (r["id"],))
                store.log_call(db, r, "no_answer")
            return True
        log.error("pausing campaign %s: could not place calls (HTTP %s)", c["id"], exc.response.status_code)
    except Exception as exc:  # network or config: stop before every recipient burns an attempt
        log.error("pausing campaign %s: could not place calls (%s)", c["id"], type(exc).__name__)
    else:
        with store.tx() as db:
            db.execute("UPDATE recipients SET call_sid = COALESCE(call_sid, ?) WHERE id = ?", (res.get("call_sid"), r["id"]))
        return True
    with store.tx() as db:
        db.execute("UPDATE campaigns SET status = 'paused' WHERE id = ? AND status = 'running'", (c["id"],))
    return False


def _undo(r: dict) -> None:
    """Put a recipient back exactly as it was before this tick claimed it."""
    with store.tx() as db:
        db.execute("UPDATE recipients SET in_flight = 0, attempts = ?, outcome = ?, retrying = ?, channel = ?, "
                   "call_sid = ?, recording_url = ?, last_attempt_at = ? WHERE id = ?",
                   (r["attempts"], r["outcome"], r["retrying"], r["channel"], r["call_sid"], r["recording_url"],
                    r["last_attempt_at"], r["id"]))


async def _tick() -> None:
    running = [c for c in store.campaigns() if c["status"] == "running" and not c["simulated"]]
    with store.tx() as db:
        _expire_stale(db)
        for c in running:
            refresh_status(db, c)
    if not running or not ready() or not in_window():
        return
    batch: list[tuple[dict, dict]] = []
    with store.tx() as db:
        free = CONCURRENCY - db.execute("SELECT COUNT(*) FROM recipients WHERE in_flight = 1").fetchone()[0]
        for c in running:
            if free <= 0:
                break
            for r in _due(db, c, free):
                db.execute("UPDATE recipients SET in_flight = 1, attempts = attempts + 1, retrying = 0, outcome = 'pending', "
                           "channel = NULL, call_sid = NULL, recording_url = NULL, last_attempt_at = ? WHERE id = ?",
                           (now_iso(), r["id"]))
                batch.append((c, dict(r)))
                free -= 1
    paused: set[str] = set()
    for c, r in batch:
        if c["id"] in paused or not await _dial(c, r):
            paused.add(c["id"])
            _undo(r)


async def run() -> None:
    while True:
        await asyncio.sleep(2)
        try:
            await _tick()
        except Exception:
            log.exception("dialer tick failed")


@router.post("/api/telephony/status")
async def status_callback(request: Request, token: str = ""):
    """Exotel StatusCallback (terminal event). JSON or form-encoded; field names UNVERIFIED."""
    expected = os.getenv("WEBHOOK_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(403, "Bad token")
    body = await request.body()
    try:
        data = json.loads(body)
    except ValueError:
        data = dict(parse_qsl(body.decode(errors="replace")))
    rid, sid = data.get("CustomField"), data.get("CallSid")
    status = str(data.get("Status", "")).lower()
    with store.tx() as db:
        r = db.execute("SELECT * FROM recipients WHERE id = ? OR (call_sid IS NOT NULL AND call_sid = ?)",
                       (rid, sid)).fetchone()
        if not r or not r["in_flight"]:
            return {"ok": True}
        outcome = r["outcome"]
        if outcome == "pending":
            outcome = "voicemail" if status == "completed" else "no_answer"
        recording = data.get("RecordingUrl") if status == "completed" else None
        db.execute("UPDATE recipients SET in_flight = 0, outcome = ?, call_sid = COALESCE(call_sid, ?), "
                   "recording_url = ? WHERE id = ?", (outcome, sid, recording or None, r["id"]))
        store.log_call(db, dict(r) | {"call_sid": r["call_sid"] or sid}, outcome)
    log.info("call finished recipient=%s status=%s outcome=%s", r["id"], status, outcome)
    return {"ok": True}
