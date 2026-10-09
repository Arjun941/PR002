"""Reachout: dashboard API on real data (SQLite).

Campaigns come from the builder (backend/builder.py) and calls are placed by the dialer
(backend/dialer.py), which rings the phone page (backend/webphone.py).
DEMO=1 seeds sample campaigns into an empty database and simulates calls for them; without
it nothing here is fake.

Run from the project root:  uvicorn backend.main:app --reload
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import assistant, audio, demo, dialer, store, stt
from . import callctx, campaignagent, catalog, chatgpt, llm, recstore
from .builder import DEMO, MAX_PROMPT, ChatTurnIn, Event, Question, Retry, Script, handling, router as builder_router
from .costs import RETRY_ESTIMATE_PER_CALL, recipient_cost
from .providers import router as providers_router
from .history import latest_by_recipient, router as history_router
from .recordings import router as recordings_router
from .dbcommon import missing_questions
from .recwire import CallRecordingMiddleware
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
app.add_middleware(CallRecordingMiddleware)  # records web-phone calls (recwire.py); touches nothing else
app.include_router(builder_router)
app.include_router(webphone_router)
app.include_router(assistant.router)
app.include_router(chatgpt.router)
app.include_router(stt.router)
app.include_router(recordings_router)
app.include_router(history_router)
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
    calls = latest_by_recipient(cid)  # each recipient's latest call: what was asked and answered
    missing = {r["id"]: [q["label"] for q in missing_questions(c["questions"], r)] for r in recs}  # decided, but questions unanswered
    unfinished = sum(1 for r in recs if missing[r["id"]] and not r["in_flight"])
    s = _summary(c, recs)
    return s | {
        "totals": s["totals"] | {"retryable": s["totals"]["retryable"] + unfinished}, "unfinished": unfinished,
        "by_language": _group(recs, "language"),
        "by_segment": _group(recs, "segment"),
        "handling": c["handling"] | {"recordings": recstore.handling(c)},  # what is done with audio now, for older campaigns too
        "retry_estimate_inr": round((sum(r["outcome"] in NON_RESPONDER for r in recs) + unfinished) * RETRY_ESTIMATE_PER_CALL, 1),
        "retry_policy": c["retry"],
        "note": c["note"],
        "provider": c["provider"] or c["agent_provider"], "mode": c["mode"], "voice": c["voice"],
        "system_prompt": c["system_prompt"], "ivr": audio.audio_status(c), "synthesising": cid in audio._active,
        "scripts": [{"language": LANGUAGES.get(l, l), "code": l} | s for l, s in c["scripts"].items()],
        "questions": _question_results(c["questions"], recs),
        "recipients": [store.public_recipient(r, c["questions"]) | {"last_call": calls.get(r["id"]), "missing": missing[r["id"]]} for r in recs],
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
    mode: Literal["live", "hybrid"] | None = None
    system_prompt: str | None = Field(None, min_length=1, max_length=MAX_PROMPT)
    retry: Retry | None = None
    scripts: dict[str, Script] | None = None
    questions: list[Question] | None = None  # every question the campaign should have: the existing ones unchanged, plus new ones
    event: dict | None = None  # the event facts to change (merged into the current ones)


@app.patch("/api/campaigns/{cid}")
def edit_campaign(cid: str, body: CampaignEdit):
    """Edit a campaign that is not calling: name, provider, mode, agent system prompt, retry policy and the
    scripts, and new follow-up questions (existing ones are fixed: answers are saved against their options). Contacts
    and languages are fixed once created. A hybrid campaign whose scripts changed (or
    that just became hybrid) goes back to 'preparing' when resumed, to synthesise what is missing."""
    c = _get(cid)
    if c["status"] in ("running", "preparing"):
        raise HTTPException(409, "Pause the campaign before editing it")
    changes: dict = {}
    provider = c["provider"] or c["agent_provider"]
    mode = body.mode or c["mode"]
    if body.name is not None:
        changes["name"] = body.name.strip()
    if body.system_prompt is not None:
        changes["system_prompt"] = body.system_prompt.strip()
    if body.retry is not None:
        changes["retry"] = body.retry.model_dump()
    if body.event:
        try:
            ev = Event(**((c["event"] or {}) | {k: v for k, v in body.event.items() if k in Event.model_fields})).model_dump()
        except Exception as exc:
            raise HTTPException(400, f"Event details are not valid: {str(exc)[:120]}")
        if ev != c["event"]:
            changes["event"] = ev
    if body.provider is not None and body.provider != provider:
        provider = body.provider
        changes |= {"provider": provider, "agent_provider": provider}
    if provider != (c["provider"] or c["agent_provider"]) or mode != c["mode"]:
        if not catalog.ready(provider, "live"):
            raise HTTPException(400, f"{catalog.label(provider)} is not set up for live calls "
                                     f"(set {', '.join(catalog.missing(provider, 'live'))})")
    voice = c["voice"]
    if mode == "hybrid" and mode != c["mode"]:
        voice = audio.choose_voice(c)
        if not voice:
            raise HTTPException(400, "Hybrid mode makes its IVR audio with ElevenLabs: set ELEVENLABS_API_KEY and "
                                     "ELEVENLABS_VOICE_ID")
    questions = c["questions"]
    if body.questions is not None:
        have = {q["id"]: q for q in c["questions"]}
        sent = {q.id: q.model_dump() for q in body.questions}
        if len(sent) != len(body.questions):
            raise HTTPException(400, "Two questions have the same id")
        for qid, old in have.items():  # existing questions already have answers saved against their options: keep them as they are
            if sent.get(qid) != {k: old.get(k) for k in ("id", "label", "options", "only_if_confirmed")}:
                raise HTTPException(400, f"Question “{old['label']}” cannot be changed or removed; add new questions instead")
        questions = [q.model_dump() for q in body.questions]
        if len(questions) > llm.MAX_QUESTIONS:
            raise HTTPException(400, f"At most {llm.MAX_QUESTIONS} follow-up questions")
        if questions != c["questions"]:
            changes["questions"] = questions
    ids = {q["id"] for q in questions}
    scripts_in = body.scripts if body.scripts is not None else None
    if scripts_in is not None and set(scripts_in) != set(c["scripts"]):
        raise HTTPException(400, "Scripts must cover exactly this campaign's languages")
    new = {}
    for lang, old in c["scripts"].items():
        sc = scripts_in[lang] if scripts_in is not None else None
        merged = old | sc.model_dump(exclude={"questions"}) if sc else dict(old)
        texts = {k: v for k, v in (old.get("questions", {}) | (sc.questions if sc else {})).items() if k in ids}
        if any(not texts.get(q["id"], "").strip() for q in questions):
            raise HTTPException(400, f"Every follow-up question needs its spoken text ({LANGUAGES.get(lang, lang)})")
        if mode == "hybrid" and not (merged.get("doubts") or "").strip():
            raise HTTPException(400, f"Write the closing question (any other questions?) for {LANGUAGES.get(lang, lang)}")
        new[lang] = merged | {"questions": texts}
    if new != c["scripts"]:
        changes["scripts"] = new
    if mode != c["mode"]:
        changes |= {"mode": mode, "escalation": int(mode == "hybrid"), "voice": voice}
    if "handling" not in changes and ({"provider", "mode"} & set(changes)):
        changes["handling"] = handling(provider, mode, voice or provider)
    if mode == "hybrid" and not c["simulated"] and ("scripts" in changes or mode != c["mode"]):
        changes["audio_ready"] = False  # resuming synthesises only what is missing
    if changes:
        store.update_campaign(cid, changes)
    return {"updated": sorted(changes)}


# ---------- campaign agent ----------

def _agent_state(cid: str) -> dict:
    """Everything the campaign agent is told about an existing campaign, on every turn."""
    d = campaign_detail(cid)
    c = _get(cid)
    return {
        "name": d["name"], "status": d["status"], "mode": c["mode"], "provider": d["provider"], "languages": c["languages"],
        "event": c["event"], "retry": d["retry_policy"], "system_prompt": c["system_prompt"],
        "scripts": {l: {k: v for k, v in s.items() if k in (*campaignagent.SCRIPT_FIELDS, "questions")} for l, s in c["scripts"].items()},
        "questions": [{"id": q["id"], "label": q["label"], "options": q["options"], "only_if_confirmed": q["only_if_confirmed"],
                       "answered": q["answered"], "results": q["results"]} for q in d["questions"]],
        "results": {"recipients": d["totals"]["recipients"], "calls_placed": d["totals"]["calls_placed"],
                    "answered": d["totals"]["answered"], "confirm_rate": d["totals"]["confirm_rate"], "counts": d["totals"]["counts"],
                    "cost_inr": d["totals"]["cost_inr"]},
        "note": d["note"], "ivr_audio_ready": bool(c["audio_ready"]),
    }


class AgentReq(BaseModel):
    scope: Literal["builder", "campaign"]
    campaign_id: str | None = None
    message: str | None = Field(None, max_length=2000)            # campaign: the new user message
    messages: list[ChatTurnIn] = Field(default_factory=list, max_length=200)  # builder: the chat so far, new message last
    state: dict | None = None                                      # builder: the form as it is now


@app.get("/api/campaigns/{cid}/chat")
def campaign_chat(cid: str):
    c = _get(cid)
    return {"messages": c["chat"], "summary": c["chat_summary"]}


@app.post("/api/assistant/agent")
def campaign_agent(body: AgentReq):
    """One turn with the campaign agent. In the builder it only proposes edits (the form applies them); on a campaign
    it applies them through the Edit page's validation and saves the chat on the campaign."""
    if body.scope == "builder":
        if not body.messages or body.messages[-1].role != "user":
            raise HTTPException(400, "The last message must be from the user")
        history = [t.model_dump() for t in body.messages]
        try:
            reply, edits, actions, warnings = campaignagent.turn("builder", body.state or {}, "", history)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        return {"reply": reply, "edits": edits, "actions": actions, "errors": [], "warnings": warnings}

    if not body.campaign_id or not body.message or not body.message.strip():
        raise HTTPException(400, "A campaign and a message are needed")
    cid = body.campaign_id
    c = _get(cid)
    history = [*c["chat"], {"role": "user", "content": body.message.strip()}]
    try:
        reply, edits, actions, warnings = campaignagent.turn("campaign", _agent_state(cid), c["chat_summary"], history)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    applied, errors = [], []
    if "pause" in actions and c["status"] in ("running", "preparing"):
        store.set_status(cid, "paused")
        applied.append("paused")
    if edits:
        cur = c["scripts"]
        scripts = None
        if "scripts" in edits:
            scripts = {}
            for l, sc in cur.items():
                part = edits["scripts"].get(l, {})
                texts = (sc.get("questions") or {}) | part.get("questions", {})
                scripts[l] = {k: v for k, v in sc.items() if k != "questions"} | {k: v for k, v in part.items() if k != "questions"} | {"questions": texts}
        try:
            payload = CampaignEdit(
                name=edits.get("name"), provider=edits.get("provider"), mode=edits.get("mode"),
                system_prompt=edits.get("system_prompt"), retry=edits.get("retry"), event=edits.get("event"),
                scripts={l: Script(**sc) for l, sc in scripts.items()} if scripts else None)
            applied += edit_campaign(cid, payload)["updated"]
        except HTTPException as exc:
            errors.append(str(exc.detail))
        except Exception as exc:
            errors.append(f"The change was not valid ({type(exc).__name__}).")
    if "resynthesize" in actions:
        fresh = _get(cid)
        if fresh["mode"] == "hybrid" and fresh["status"] != "preparing" and audio.choose_voice(fresh):
            if audio.start(cid, True):
                applied.append("ivr audio")
    if errors:
        reply += "\n\nI could not apply that: " + " ".join(errors)
    turns = [*c["chat"], {"role": "user", "content": body.message.strip(), "applied": []},
             {"role": "assistant", "content": reply[:2000], "applied": applied}]
    summary = c["chat_summary"]
    if len(turns) > campaignagent.SUMMARISE_AT:
        summary = campaignagent.fold(summary, turns[:campaignagent.SUMMARISE_N])
        turns = turns[campaignagent.SUMMARISE_N:]
    store.update_campaign(cid, {"chat": turns, "chat_summary": summary})
    return {"reply": reply, "edits": edits, "actions": actions, "applied": applied, "errors": errors, "warnings": warnings}


@app.post("/api/campaigns/{cid}/reset")
def reset_campaign(cid: str, request: Request):
    """Development tool: undo every call of a campaign that is not calling, so it can be run again from scratch. Deletes the
    campaign's call history, answers and recordings. ALLOW_RESET=0 turns it off."""
    if os.getenv("ALLOW_RESET", "1") == "0":
        raise HTTPException(403, "Resetting campaigns is turned off (ALLOW_RESET=0)")
    c = _get(cid)
    if c["status"] in ("running", "preparing"):
        raise HTTPException(409, "Pause the campaign before resetting it")
    n = store.reset_campaign(cid)
    store.log_access("campaign.reset", cid, request.client.host if request.client else None)
    return {"reset": n}


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
