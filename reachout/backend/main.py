"""Reachout: dashboard API (Phase 0, mock data).

Everything in here is seeded demo data so the dashboard can be built and
judged before Exotel access lands. The response shapes are the contract the
real call engine will fill in later, so the frontend should not need to change.

Run from the project root:  uvicorn backend.main:app --reload
Set DEMO_LIVE=0 to stop the background simulator that resolves pending calls.
"""
from __future__ import annotations

import asyncio
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import exotel
from .voicebot import router as voicebot_router

DEMO_LIVE = os.getenv("DEMO_LIVE", "1") == "1"

LANGUAGES = {"en": "English", "hi": "Hindi", "mr": "Marathi", "ta": "Tamil", "kn": "Kannada"}
ANSWERED = ("confirmed", "declined", "rescheduled")
NON_RESPONDER = ("voicemail", "no_answer")
OUTCOMES = ("confirmed", "rescheduled", "declined", "voicemail", "no_answer", "pending")

# Rough per-call cost model in INR (see the cost comparison): telephony only for
# keypad calls, plus an extra when the live voice agent had to step in.
COST = {"answered": 0.7, "agent_extra": 3.0, "unanswered": 0.3}
RETRY_ESTIMATE_PER_CALL = 0.45

FIRST = ["Aarav", "Vihaan", "Ananya", "Diya", "Rohan", "Isha", "Kabir", "Meera", "Arjun", "Saanvi",
         "Neha", "Rahul", "Priya", "Karan", "Sneha", "Aditya", "Pooja", "Vikram", "Lakshmi", "Imran",
         "Farah", "Gurpreet", "Divya", "Suresh", "Anjali", "Manoj", "Kavya", "Harsh", "Nisha", "Tarun"]
LAST = "ABCDEGHJKMNPRSTV"

SPECS = [
    dict(id="pune-ai-summit", name="Pune AI Summit: invitations", kind="seminar", status="running",
         langs={"en": .35, "hi": .40, "mr": .25}, segments={"Students": .5, "Faculty": .2, "Alumni": .3},
         size=420, done=.78, pickup=.58, mix={"confirmed": .52, "declined": .30, "rescheduled": .18}, days_ago=2,
         handling=dict(audio=dict(provider="Sarvam", note="Processed in India"),
                       text=dict(provider="Ollama (local)", note="Stays on this machine"),
                       recordings=dict(provider="Exotel", note="Stored in India, deleted after 30 days"))),
    dict(id="sunrise-clinic", name="Sunrise Clinic: appointment reminders", kind="clinic", status="completed",
         langs={"hi": .45, "en": .30, "ta": .25}, segments={"Follow-ups": .6, "New patients": .4},
         size=260, done=1.0, pickup=.71, mix={"confirmed": .74, "declined": .06, "rescheduled": .20}, days_ago=5,
         handling=dict(audio=dict(provider="Sarvam", note="Processed in India"),
                       text=dict(provider="Ollama (local)", note="Stays on this machine"),
                       recordings=dict(provider="Exotel", note="Stored in India, deleted after 7 days"))),
    dict(id="greenfield-ptm", name="Greenfield School: PTM on 18 Oct", kind="school", status="completed",
         langs={"en": .30, "hi": .45, "kn": .25}, segments={"Classes 1–5": .4, "Classes 6–8": .35, "Classes 9–10": .25},
         size=380, done=1.0, pickup=.64, mix={"confirmed": .61, "declined": .09, "rescheduled": .30}, days_ago=6,
         handling=dict(audio=dict(provider="Piper (local)", note="Stays on this machine"),
                       text=dict(provider="Ollama (local)", note="Stays on this machine"),
                       recordings=dict(provider="Exotel", note="Stored in India, deleted after 30 days"))),
    dict(id="fee-reminder-t2", name="Term 2 fee reminders", kind="payment", status="paused",
         langs={"en": .40, "hi": .60}, segments={"Due this week": .55, "Overdue": .45},
         size=300, done=.55, pickup=.49, mix={"confirmed": .46, "declined": .14, "rescheduled": .40}, days_ago=1,
         handling=dict(audio=dict(provider="Piper (local)", note="Stays on this machine"),
                       text=dict(provider="Keypad only", note="No speech is processed"),
                       recordings=dict(provider="Exotel", note="Stored in India, deleted after 30 days"))),
]

CAMPAIGNS: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def _weighted(rng: random.Random, weights: dict) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _resolve(rng: random.Random, c: dict, r: dict, day: int = 0) -> None:
    """Simulate one call attempt finishing for recipient r."""
    r["attempts"] += 1
    r["retrying"] = False
    if rng.random() < r["_p"]:
        r["outcome"] = _weighted(rng, c["_mix"])
        r["channel"] = _weighted(rng, {"keypad": .78, "speech": .14, "agent": .08})
    else:
        r["outcome"] = rng.choices(["voicemail", "no_answer"], [.55, .45])[0]
        r["channel"] = None
    c["_events"].append((day, r["outcome"] in ANSWERED))


def _build() -> None:
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    for spec in SPECS:
        seg_adj = {s: rng.uniform(-.08, .08) for s in spec["segments"]}
        lang_adj = {l: rng.uniform(-.07, .07) for l in spec["langs"]}
        c = {
            "id": spec["id"], "name": spec["name"], "kind": spec["kind"], "status": spec["status"],
            "languages": [LANGUAGES[l] for l in spec["langs"]],
            "segments": list(spec["segments"]),
            "started_at": (now - timedelta(days=spec["days_ago"], hours=3)).isoformat(),
            "handling": spec["handling"], "_mix": spec["mix"], "_events": [], "recipients": [],
        }
        for i in range(spec["size"]):
            lang = _weighted(rng, spec["langs"])
            seg = _weighted(rng, spec["segments"])
            r = {
                "id": f"{spec['id']}-{i}",
                "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}.",
                # Contact numbers are masked at the source: the dashboard never needs the full number.
                "phone": f"+91 {rng.choice('6789')}•••• ••{rng.randint(10, 99)}",
                "language": LANGUAGES[lang], "segment": seg,
                "outcome": "pending", "channel": None, "attempts": 0, "retrying": False,
                "_p": _clamp(spec["pickup"] + seg_adj[seg] + lang_adj[lang], .2, .9),
            }
            if rng.random() < spec["done"]:
                _resolve(rng, c, r, day=rng.randint(0, spec["days_ago"]))
                if r["outcome"] in NON_RESPONDER and spec["done"] == 1.0 and rng.random() < .35:
                    _resolve(rng, c, r, day=rng.randint(0, spec["days_ago"]))
            c["recipients"].append(r)
        CAMPAIGNS[c["id"]] = c


def _cost(r: dict) -> float:
    if r["outcome"] == "pending":
        return r["attempts"] * COST["unanswered"]
    cost = (r["attempts"] - 1) * COST["unanswered"]
    if r["outcome"] in ANSWERED:
        cost += COST["answered"] + (COST["agent_extra"] if r["channel"] == "agent" else 0)
    else:
        cost += COST["unanswered"]
    return cost


def _totals(recs: list[dict]) -> dict:
    counts = {k: 0 for k in OUTCOMES}
    calls = contacted = retrying = 0
    cost = 0.0
    for r in recs:
        counts[r["outcome"]] += 1
        calls += r["attempts"]
        contacted += r["attempts"] > 0
        retrying += r["retrying"]
        cost += _cost(r)
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
        groups.setdefault(r[key], []).append(r)
    out = []
    for k, rs in groups.items():
        t = _totals(rs)
        out.append({"key": k, "total": len(rs), "counts": t["counts"],
                    "answer_rate": t["answer_rate"], "confirm_rate": t["confirm_rate"]})
    return sorted(out, key=lambda g: -g["total"])


def _summary(c: dict) -> dict:
    return {k: c[k] for k in ("id", "name", "kind", "status", "languages", "segments", "started_at")} | {
        "totals": _totals(c["recipients"])}


def _daily() -> list[dict]:
    today = datetime.now().date()
    out = []
    for off in range(6, -1, -1):
        day = today - timedelta(days=off)
        calls = answered = 0
        for c in CAMPAIGNS.values():
            for o, a in c["_events"]:
                if o == off:
                    calls += 1
                    answered += a
        out.append({"label": "Today" if off == 0 else day.strftime("%a"), "calls": calls, "answered": answered})
    return out


def _refresh_status(c: dict) -> None:
    if c["status"] == "running" and not any(r["outcome"] == "pending" for r in c["recipients"]):
        c["status"] = "completed"


def _get(cid: str) -> dict:
    if cid not in CAMPAIGNS:
        raise HTTPException(404, "Campaign not found")
    return CAMPAIGNS[cid]


async def _live_loop() -> None:
    """Demo only: keep running campaigns moving so the dashboard feels live."""
    rng = random.Random()
    while True:
        await asyncio.sleep(1.5)
        for c in CAMPAIGNS.values():
            if c["status"] != "running":
                continue
            idle = [r for r in c["recipients"] if r["outcome"] == "pending" and not r["retrying"]]
            for r in rng.sample(idle, min(len(idle), rng.randint(1, 3))):
                _resolve(rng, c, r)
            _refresh_status(c)


async def _retry_worker(c: dict, targets: list[dict]) -> None:
    rng = random.Random()
    rng.shuffle(targets)
    for r in targets:
        await asyncio.sleep(rng.uniform(.05, .2))
        _resolve(rng, c, r)
        _refresh_status(c)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_live_loop()) if DEMO_LIVE else None
    yield
    if task:
        task.cancel()


_build()
app = FastAPI(title="Reachout", lifespan=lifespan)
app.include_router(voicebot_router)


@app.get("/api/overview")
def overview():
    recs = [r for c in CAMPAIGNS.values() for r in c["recipients"]]
    return {
        "totals": _totals(recs),
        "by_language": _group(recs, "language"),
        "daily": _daily(),
        "campaigns": [_summary(c) for c in CAMPAIGNS.values()],
    }


@app.get("/api/campaigns")
def list_campaigns():
    return {"campaigns": [_summary(c) for c in CAMPAIGNS.values()]}


@app.get("/api/campaigns/{cid}")
def campaign_detail(cid: str):
    c = _get(cid)
    recs = c["recipients"]
    public = ("id", "name", "phone", "language", "segment", "outcome", "channel", "attempts", "retrying")
    return _summary(c) | {
        "by_language": _group(recs, "language"),
        "by_segment": _group(recs, "segment"),
        "handling": c["handling"],
        "retry_estimate_inr": round(sum(r["outcome"] in NON_RESPONDER for r in recs) * RETRY_ESTIMATE_PER_CALL, 1),
        "recipients": [{k: r[k] for k in public} for r in recs],
    }


@app.post("/api/campaigns/{cid}/retry")
async def retry_non_responders(cid: str):
    c = _get(cid)
    targets = [r for r in c["recipients"] if r["outcome"] in NON_RESPONDER]
    if not targets:
        return {"queued": 0}
    for r in targets:
        r["outcome"], r["channel"], r["retrying"] = "pending", None, True
    task = asyncio.create_task(_retry_worker(c, targets))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"queued": len(targets)}


class TestCall(BaseModel):
    to: str


@app.post("/api/telephony/test-call")
async def test_call(body: TestCall):
    """Phase 1: place one call to a test number; the flow streams to /ws/exotel."""
    if not exotel.configured():
        raise HTTPException(503, "Exotel is not configured (see .env.example)")
    return await exotel.place_call(body.to)

