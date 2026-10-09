"""Reachout: dashboard API on real data (SQLite).

Campaigns come from the builder (backend/builder.py) and calls are placed by the dialer
(backend/dialer.py), which rings the phone page (backend/webphone.py).
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
from pydantic import BaseModel, Field

from . import assistant, audio, demo, dialer, store, stt
from . import catalog
from .builder import DEMO, MAX_PROMPT, Retry, Script, handling, router as builder_router
from .costs import RETRY_ESTIMATE_PER_CALL, recipient_cost
from .providers import router as providers_router
from .recordings import router as recordings_router
from .store import ANSWERED, LANGUAGES, NON_RESPONDER, OUTCOMES
from .webphone import router as webphone_router


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
app.include_router(builder_router)
app.include_router(webphone_router)
app.include_router(assistant.router)
app.include_router(stt.router)
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
        "provider": c["provider"] or c["agent_provider"], "mode": c["mode"], "voice": c["voice"],
        "system_prompt": c["system_prompt"], "ivr": audio.audio_status(c), "synthesising": cid in audio._active,
        "scripts": [{"language": LANGUAGES.get(l, l), "code": l} | s for l, s in c["scripts"].items()],
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
    if status == "running" and c["note"] and cid not in audio._active:
        store.set_note(cid, None)  # a stale error note from before
    if status == "running" and c["mode"] == "hybrid" and not c["simulated"] and not c["audio_ready"]:
        status = "preparing"  # hybrid calls play pre-synthesised audio: make it first
    store.set_status(cid, status)
    return {"status": status}


class CampaignEdit(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    provider: Literal["elevenlabs", "gemini"] | None = None
    system_prompt: str | None = Field(None, min_length=1, max_length=MAX_PROMPT)
    retry: Retry | None = None
    scripts: dict[str, Script] | None = None


@app.patch("/api/campaigns/{cid}")
def edit_campaign(cid: str, body: CampaignEdit):
    """Edit a campaign that is not calling: name, provider, agent system prompt, retry policy and the
    scripts. Contacts, languages, questions and the mode are fixed once created. New scripts on a hybrid
    campaign need their audio synthesised again, so it goes back to 'preparing' when resumed."""
    c = _get(cid)
    if c["status"] in ("running", "preparing"):
        raise HTTPException(409, "Pause the campaign before editing it")
    changes: dict = {}
    if body.name is not None:
        changes["name"] = body.name.strip()
    if body.system_prompt is not None:
        changes["system_prompt"] = body.system_prompt.strip()
    if body.retry is not None:
        changes["retry"] = body.retry.model_dump()
    if body.provider is not None and body.provider != (c["provider"] or c["agent_provider"]):
        if not catalog.ready(body.provider, "live"):
            raise HTTPException(400, f"{catalog.label(body.provider)} is not set up for live calls "
                                     f"(set {', '.join(catalog.missing(body.provider, 'live'))})")
        changes |= {"provider": body.provider, "agent_provider": body.provider,
                    "handling": handling(body.provider, c["mode"], c["voice"] or body.provider)}
    if body.scripts is not None:
        if set(body.scripts) != set(c["scripts"]):
            raise HTTPException(400, "Scripts must cover exactly this campaign's languages")
        new = {}
        for lang, sc in body.scripts.items():
            old = c["scripts"][lang]
            merged = old | sc.model_dump(exclude={"questions"})
            texts = old.get("questions", {}) | {k: v for k, v in sc.questions.items() if k in old.get("questions", {})}
            if any(not texts.get(q["id"], "").strip() for q in c["questions"]):
                raise HTTPException(400, f"Every follow-up question needs its spoken text ({LANGUAGES.get(lang, lang)})")
            if c["escalation"] and not merged.get("doubts", "").strip():
                raise HTTPException(400, f"The closing question is empty ({LANGUAGES.get(lang, lang)})")
            new[lang] = merged | {"questions": texts}
        if new != c["scripts"]:
            changes["scripts"] = new
            if c["mode"] == "hybrid" and not c["simulated"]:
                changes["audio_ready"] = False  # resuming re-synthesises only the phrases that changed
    if changes:
        store.update_campaign(cid, changes)
    return {"updated": sorted(changes)}


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


@app.get("/api/campaigns/{cid}/ivr/{lang}/{key}")
def ivr_audio(cid: str, lang: str, key: str):
    """One synthesised IVR phrase as a WAV (the Listen buttons). The greeting plays with the first recipient's name
    when that is cached too."""
    from fastapi.responses import Response
    c = _get(cid)
    recs = store.recipients(cid)
    name = next((r["name"] for r in recs if r["language"] == lang), "")
    pcm = audio.phrase_audio(c, lang, key, name)
    if not pcm:
        raise HTTPException(404, "Not synthesised yet")
    return Response(audio.wav(pcm), media_type="audio/wav", headers={"Cache-Control": "no-store"})


class Synth(BaseModel):
    force: bool = False


@app.post("/api/campaigns/{cid}/ivr/synthesize")
async def ivr_synthesize(cid: str, body: Synth):
    """Synthesise the campaign's IVR audio in the background: only what is missing, or everything again with
    force (Resynthesize). Works for any campaign, including live ones that never had audio."""
    c = _get(cid)
    if c["status"] == "preparing":
        raise HTTPException(409, "The campaign is already preparing its audio")
    if not audio.choose_voice(c):
        raise HTTPException(400, "IVR audio uses ElevenLabs: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID")
    if not audio.start(cid, body.force):
        raise HTTPException(409, "Already synthesising")
    return {"started": True}
