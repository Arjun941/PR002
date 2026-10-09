"""Call history: one record per call the phone rang (written by recwire.py when the call ends), with its
timings, outcome, keypad answers, everything asked and answered (callinsight.py) and its recording.

Recordings are personal data: the audio route needs the same PIN-unlocked session as the campaign
pages (recordings.py) and every play is written to access_log. Phone numbers are stored masked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response

from . import recordings, recstore, store

router = APIRouter(prefix="/api")
STARTED = datetime.now(timezone.utc).isoformat(timespec="seconds")  # calls before this were not ours to see
LIST_FIELDS = ("id", "campaign_id", "campaign_name", "org", "recipient_id", "recipient_name", "phone", "language", "provider",
               "mode", "rang_at", "answered_at", "ended_at", "ring_seconds", "talk_seconds", "outcome", "channel", "attempt",
               "answers", "recording", "analysis", "final_heard", "summary")


def _with_audio(calls: list[dict]) -> list[dict]:
    have = store.recordings_present([c["id"] for c in calls])
    return [c | {"has_recording": c["id"] in have} for c in calls]


def unseen_calls(hours: int = 24) -> list[dict]:
    """Calls placed while this backend was running that have no history record. Calls are only written to History
    by the backend whose phone page placed them, so these were placed by ANOTHER Reachout server using the same
    database (the phone page's Server setting points there), which does not record calls."""
    since = max(STARTED, (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds"))
    seen = {c["id"] for c in store.list_calls(None, 5000)}
    out = []
    for r in store.recipients():
        sid = r.get("call_sid") or ""
        if sid.startswith("web-") and (r.get("last_attempt_at") or "") >= since and sid not in seen:
            out.append({"call_id": sid, "campaign_id": r["campaign_id"], "recipient": r["name"], "at": r["last_attempt_at"]})
    return sorted(out, key=lambda x: x["at"], reverse=True)


@router.get("/history")
def history(campaign: str | None = None, limit: int = 200):
    calls = _with_audio(store.list_calls(campaign, max(1, min(limit, 1000))))
    return {"unseen": unseen_calls(), "calls": [{k: c.get(k) for k in LIST_FIELDS} | {"has_recording": c["has_recording"], "qa_count": len(c.get("qa") or [])}
                      for c in calls],
            "campaigns": [{"id": c["id"], "name": c["name"]} for c in store.campaigns()]}


@router.get("/history/{call_id}")
def call_detail(call_id: str):
    call = store.get_call(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    return _with_audio([call])[0]


@router.get("/history/{call_id}/audio")
def call_audio(call_id: str, request: Request):
    if not recordings.enabled() or not recordings._valid(request.cookies.get(recordings.COOKIE)):
        raise HTTPException(401, "Unlock recordings first")
    saved = recstore.load(call_id)
    if not saved:
        raise HTTPException(404, "No recording for this call (or RECORDINGS_KEY changed)")
    store.log_access("recordings.play", call_id, recordings._client(request))
    return Response(saved[0], media_type=saved[1], headers={"Cache-Control": "no-store"})


def latest_by_recipient(campaign_id: str) -> dict[str, dict]:
    """Each recipient's most recent call with its answers, for the campaign page."""
    out: dict[str, dict] = {}
    for c in store.list_calls(campaign_id, 5000):  # newest first
        if c.get("recipient_id") and c["recipient_id"] not in out:
            out[c["recipient_id"]] = {"call_id": c["id"], "qa": c.get("qa") or [], "summary": c.get("summary") or "",
                                      "talk_seconds": c.get("talk_seconds"), "ended_at": c.get("ended_at"),
                                      "analysis": (c.get("analysis") or {}).get("status")}
    return out
