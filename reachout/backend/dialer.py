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
from datetime import datetime
from urllib.parse import parse_qsl

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import exotel, store

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


def record_dtmf(call_sid: str | None, digit: str) -> bool:
    """Called by the voicebot. Returns True if the digit was a menu choice for a known call."""
    return bool(DIGITS.get(digit)) and record_outcome(call_sid, DIGITS[digit], "keypad")


def record_answer(call_sid: str | None, question: dict, digit: str) -> bool:
    """A follow-up question's answer (a key that is one of its options) for the call's recipient."""
    if not call_sid or not digit.isdigit() or not 0 < int(digit) <= len(question["options"]):
        return False
    return store.add_answer(call_sid, question["id"], digit)


def record_outcome(call_sid: str | None, outcome: str | None, channel: str) -> bool:
    """Saves an answered call's result (keypad digit, or the voice agent's record_outcome tool).
    outcome=None only marks the channel, e.g. when the caller is handed to the agent."""
    if not call_sid or outcome not in (None, *DIGITS.values()):
        return False
    return store.mark_outcome(call_sid, outcome, channel)


async def _dial(c: dict, r: dict) -> bool:
    """Place one call. False means the campaign was paused (config/network) and the attempt undone."""
    cb = f"{os.environ['PUBLIC_URL'].rstrip('/')}/api/telephony/status?token={os.environ['WEBHOOK_TOKEN']}"
    try:
        res = await exotel.place_call(r["phone"], custom_field=r["id"], status_callback=cb, record=c["record"])
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500 and exc.response.status_code not in (401, 403, 429):
            log.warning("call rejected for %s (HTTP %s)", r["id"], exc.response.status_code)
            store.mark_unreachable(r)  # this number is the problem: count it as unreachable
            return True
        log.error("pausing campaign %s: could not place calls (HTTP %s)", c["id"], exc.response.status_code)
    except Exception as exc:  # network or config: stop before every recipient burns an attempt
        log.error("pausing campaign %s: could not place calls (%s)", c["id"], type(exc).__name__)
    else:
        store.set_call_sid(r["id"], res.get("call_sid"))
        return True
    store.pause_running(c["id"])
    return False


async def _tick() -> None:
    running = [c for c in store.campaigns() if c["status"] == "running" and not c["simulated"]]
    store.expire_stale(store.ago(minutes=STALE_MINUTES))
    for c in running:
        store.refresh_status(c)
    if not running or not ready() or not in_window():
        return
    paused: set[str] = set()
    for c, r in store.claim_due(running, CONCURRENCY):
        if c["id"] in paused or not await _dial(c, r):
            paused.add(c["id"])
            store.restore_recipient(r)


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
    done = store.finish_call(rid, sid, status == "completed", data.get("RecordingUrl"))
    if done:
        log.info("call finished recipient=%s status=%s outcome=%s", done[0]["id"], status, done[1])
    return {"ok": True}
