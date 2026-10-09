"""Reachout: dashboard API on real data (SQLite).

Campaigns come from the builder (backend/builder.py) and calls are placed by the dialer
(backend/dialer.py), which records outcomes from Exotel's status callback and the voicebot.
DEMO=1 seeds sample campaigns into an empty database and simulates calls for them; without
it nothing here is fake.

Run from the project root:  uvicorn backend.main:app --reload
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import assistant, audio, chatgpt, demo, dialer, exotel, store, stt
from .builder import DEMO, router as builder_router
from .costs import RETRY_ESTIMATE_PER_CALL, recipient_cost
from .providers import router as providers_router
from .recordings import router as recordings_router
from .store import ANSWERED, LANGUAGES, NON_RESPONDER, OUTCOMES
from .voicebot import router as voicebot_router


def _totals(recs: list[dict]) -> dict:
    counts = {k: 0 for k in OUTCOMES}
    calls = contacted = retrying = 0
    cost = 0.0
    for r in recs:
        counts[r["outcome"]] += 1
        calls += r["attempts"]
        contacted += r["attempts"] > 0
        retrying += bool(r["retrying"])
        cost += recipient_cost(r)
    answered = sum(counts[k] for k in ANSWERED)
    return {
        "recipients": len(recs), "contacted": contacted, "calls_placed": calls, "answered": answered,
        "answer_rate": answered / contacted if contacted else 0,
        "confirm_rate": counts["confirmed"] / answered if answered else 0,
        "cost_inr": round(cost, 2), "counts": counts,
        "retryable": counts["voicemail"] + counts["no_answer"], "retrying": retrying,
    }


def _group(recs: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in recs:
        k = LANGUAGES.get(r[key], r[key]) if key == "language" else r[key]
        groups.setdefault(k, []).append(r)
    out = []
    for k, rs in groups.items():
        t = _totals(rs)
        out.append({"key": k, "total": len(rs), "counts": t["counts"],
                    "answer_rate": t["answer_rate"], "confirm_rate": t["confirm_rate"]})
    return sorted(out, key=lambda g: -g["total"])


def _summary(c: dict, recs: list[dict]) -> dict:
    return {k: c[k] for k in ("id", "name", "kind", "status", "segments", "started_at", "simulated")} | {
        "languages": [LANGUAGES.get(l, l) for l in c["languages"]], "totals": _totals(recs)}


def _by_campaign() -> list[tuple[dict, list[dict]]]:
    recs: dict[str, list[dict]] = {}
    for r in store.recipients():
        recs.setdefault(r["campaign_id"], []).append(r)
    return [(c, recs.get(c["id"], [])) for c in store.campaigns()]


def _daily() -> list[dict]:
    today = datetime.now().astimezone().date()
    days = {today - timedelta(days=off): [0, 0] for off in range(6, -1, -1)}
    since = datetime.combine(today - timedelta(days=6), datetime.min.time()).astimezone().astimezone(timezone.utc)
    for call in store.calls_since(since.isoformat(timespec="seconds")):
        day = datetime.fromisoformat(call["at"]).astimezone().date()
        if day in days:
            days[day][0] += 1
            days[day][1] += call["answered"]
    return [{"label": "Today" if d == today else d.strftime("%a"), "calls": n, "answered": a}
            for d, (n, a) in days.items()]


def _question_results(questions: list[dict], recs: list[dict]) -> list[dict]:
    """How many people pressed each option of each follow-up question."""
    out = []
    for q in questions:
        counts = [0] * len(q["options"])
        for r in recs:
            k = store.answers(r).get(q["id"], "")
            if k.isdigit() and 0 < int(k) <= len(counts):
                counts[int(k) - 1] += 1
        out.append(q | {"answered": sum(counts),
                        "results": [{"key": str(i + 1), "label": o, "count": n} for i, (o, n) in enumerate(zip(q["options"], counts))]})
    return out


def _get(cid: str) -> dict:
    c = store.campaign(cid)
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    if DEMO and not store.has_campaigns():
        demo.seed()
    tasks = [asyncio.create_task(dialer.run()), asyncio.create_task(audio.run())]
    tasks += [asyncio.create_task(demo.simulate())] if DEMO else []
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Reachout", lifespan=lifespan)
app.include_router(voicebot_router)
app.include_router(builder_router)
app.include_router(chatgpt.router)
app.include_router(assistant.router)
app.include_router(stt.router)
app.include_router(dialer.router)
app.include_router(recordings_router)
app.include_router(providers_router)


@app.get("/api/overview")
def overview():
    pairs = _by_campaign()
    recs = [r for _, rs in pairs for r in rs]
    return {
        "totals": _totals(recs),
        "by_language": _group(recs, "language"),
        "daily": _daily(),
        "campaigns": [_summary(c, rs) for c, rs in pairs],
    }


@app.get("/api/campaigns")
def list_campaigns():
    return {"campaigns": [_summary(c, rs) for c, rs in _by_campaign()]}


@app.get("/api/campaigns/{cid}")
def campaign_detail(cid: str):
    c = _get(cid)
    recs = store.recipients(cid)
    return _summary(c, recs) | {
        "by_language": _group(recs, "language"),
        "by_segment": _group(recs, "segment"),
        "handling": c["handling"],
        "retry_estimate_inr": round(sum(r["outcome"] in NON_RESPONDER for r in recs) * RETRY_ESTIMATE_PER_CALL, 1),
        "retry_policy": c["retry"],
        "note": c["note"],
        "scripts": [{"language": LANGUAGES.get(l, l)} | s for l, s in c["scripts"].items()],
        "questions": _question_results(c["questions"], recs),
        "recipients": [store.public_recipient(r, c["questions"]) for r in recs],
    }


@app.post("/api/campaigns/{cid}/retry")
def retry_non_responders(cid: str):
    _get(cid)
    return {"queued": store.requeue(cid)}


class StatusChange(BaseModel):
    status: Literal["running", "paused"]


@app.post("/api/campaigns/{cid}/status")
def change_status(cid: str, body: StatusChange):
    """Pause stops new calls at once (calls already ringing finish). Resume picks up where it left off,
    first finishing the voice synthesis if a live campaign's audio is not ready."""
    c = _get(cid)
    if c["status"] == "completed":
        raise HTTPException(409, "Campaign has already finished")
    status = body.status
    if status == "running" and not c["simulated"] and not c["audio_ready"]:
        status = "preparing"
    store.set_status(cid, status)
    return {"status": status}


@app.delete("/api/campaigns/{cid}")
def delete_campaign(cid: str, request: Request):
    """Removes the campaign, its contacts and call log. A campaign that is calling must be paused first.
    Recordings stay with Exotel and cached voice audio stays until retention runs (Phase 6)."""
    c = _get(cid)
    if c["status"] in ("running", "preparing"):
        raise HTTPException(409, "Pause the campaign before deleting it")
    store.delete_campaign(cid)
    store.log_access("campaign.delete", cid, request.client.host if request.client else None)
    return {"deleted": cid}


class TestCall(BaseModel):
    to: str


@app.post("/api/telephony/test-call")
async def test_call(body: TestCall):
    """Phase 1: place one call to a test number; the flow streams to /ws/exotel."""
    if not exotel.configured():
        raise HTTPException(503, "Exotel is not configured (see .env.example)")
    return await exotel.place_call(body.to)
