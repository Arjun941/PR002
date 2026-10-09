"""Reminders and updates: a voice message about an event that is already set up, sent to a chosen group of its recipients.

The organiser opens the campaign's "Remind / update" chat, an assistant writes the message (or they type their own), it is
translated into the campaign's languages, they pick who gets it by outcome (confirmed, declined, voicemail ...) and send it now or
at a set time. Delivery is a phone call through the same web phone as the campaign's own calls: the live agent says the message,
answers questions about it from the event facts, then ends the call. One call at a time; someone who does not answer is tried once
more after NOTICE_GAP_MINUTES. These calls never change a recipient's campaign outcome or answers, show up in History (tagged as a
reminder or update) and are recorded like any other call.

The audience is decided when the notice is due, not when it is written, so a scheduled reminder reaches whoever has "confirmed" by then.
The conversation reuses webphone._converse (the engine glue shared with the campaign's own live calls).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import catalog, llm, store, webphone
from .dbcommon import LANGUAGES, missing_questions, now_iso

log = logging.getLogger("reachout.notices")
router = APIRouter(prefix="/api")

MAX_ATTEMPTS = int(os.getenv("NOTICE_ATTEMPTS", "2"))
GAP_MINUTES = int(os.getenv("NOTICE_GAP_MINUTES", "30"))
CHOICES = ("confirmed", "declined", "rescheduled", "voicemail", "no_answer", "pending", "unfinished")
LABELS = {"confirmed": "Confirmed", "declined": "Declined", "rescheduled": "Wants to reschedule", "voicemail": "Voicemail",
          "no_answer": "No answer", "pending": "Not reached yet", "unfinished": "Answered but unfinished"}
WAITING = "Waiting for the phone: open /phone on a device and keep it open."
Kind = Literal["reminder", "update"]
_busy = False  # one notice call at a time


# ---------------- who gets it ----------------

def audience(c: dict, recs: list[dict], outcomes: list[str]) -> list[dict]:
    """The recipients that match any of the chosen outcomes right now. "unfinished" = gave a decision but has questions left."""
    want = set(outcomes)
    qs = c.get("questions") or []
    return [r for r in recs if r["outcome"] in want or ("unfinished" in want and missing_questions(qs, r))]


def counts(c: dict, recs: list[dict]) -> dict[str, int]:
    return {k: len(audience(c, recs, [k])) for k in CHOICES}


# ---------------- writing the message ----------------

class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1500)


class Manual(BaseModel):
    lang: str
    text: str = Field(min_length=1, max_length=600)


class DraftIn(BaseModel):
    kind: Kind
    messages: list[Turn] = Field(default_factory=list, max_length=12)
    manual: Manual | None = None  # the organiser's own wording in one language: translate it, do not rewrite it


def _facts(c: dict) -> str:
    e = c.get("event") or {}
    rows = (("Organisation", e.get("org")), ("About", e.get("title")), ("Date", e.get("date")), ("Time", e.get("time")),
            ("Venue", e.get("venue")), ("Details", e.get("details")))
    return "\n".join(f"- {k}: {v}" for k, v in rows if v) or "- (no details)"


def _draft_prompt(c: dict, body: DraftIn, langs: list[str]) -> list[dict]:
    e = c.get("event") or {}
    names = ", ".join(f"{LANGUAGES[l]} ({l})" for l in langs)
    shape = {"reply": "one short sentence to the organiser", "texts": {l: "..." for l in langs}}
    system = ("You write short spoken phone messages for an automated calling system used by schools, clinics and event organisers "
              f"in India. The messages are for {e.get('org') or 'the organisation'}. Reply with JSON only.")
    rules = f"""Event facts (use only these and what the organiser tells you):
{_facts(c)}

Write the message in each of these languages, natively in its own script, as simple, polite spoken text: {names}.
Rules:
- Under 60 words each. It is read out by a voice agent, so no lists and no symbols.
- Start with "Hello {{name}}, this is {e.get('org') or 'us'}." using the placeholder {{name}} exactly once and no other placeholders.
- Never tell the person to press a key. Never invent a date, time, venue, price or promise: use only the facts above and what the organiser says.
- Keep dates, times, venues and names exactly as given. Write numbers and dates the way a person would say them.
Reply with JSON only, in exactly this shape: {json_dumps(shape)}"""
    if body.manual:
        src = LANGUAGES.get(body.manual.lang, body.manual.lang)
        task = (f"The organiser wrote this {body.kind} message in {src}:\n\"{body.manual.text}\"\n"
                f"Translate it faithfully into every language listed (the original stays as it is for {src}). Translate EVERY word, "
                "including the greeting, into the language's own script: leave in English only the placeholder {name}, names of people and "
                "places, and numbers. Do not add or drop information. If the original has no greeting, add one from the rules, in the "
                "target language. \"reply\" says what you did.")
        return [{"role": "system", "content": system}, {"role": "user", "content": rules + "\n\n" + task}]
    what = ("Write a REMINDER: remind the person about the event with its date, time and venue, warmly and briefly." if body.kind == "reminder"
            else "Write an UPDATE: tell the person what has CHANGED about the event, using ONLY what the organiser says changed, and say what "
                 "stays the same only if the facts above show it. If the organiser has not yet said what changed, set \"texts\" to {} and ask "
                 "in \"reply\" what changed.")
    task = what + " After the message, the agent answers questions from the facts and the organiser's words only. If the organiser asks " \
                  "you to change the draft, rewrite it fully with the change applied."
    turns = [{"role": t.role, "content": t.content} for t in body.messages] or [{"role": "user", "content": "Write it."}]
    return [{"role": "system", "content": system}, {"role": "user", "content": rules + "\n\n" + task}, *turns]


def json_dumps(o) -> str:
    import json
    return json.dumps(o, ensure_ascii=False)


def _clean_text(t) -> str:
    t = re.sub(r"\{(?!name\})[^{}]*\}", "", str(t or "")).strip()[:600]
    if t.count("{name}") > 1:  # one greeting only
        first = t.index("{name}") + 6
        t = t[:first] + t[first:].replace("{name}", "")
    return t


@router.post("/campaigns/{cid}/notices/draft")
def draft(cid: str, body: DraftIn):
    c = _campaign(cid)
    langs = list(c["languages"])
    if body.manual and body.manual.lang not in langs:
        raise HTTPException(400, "That language is not one of this campaign's languages")
    raw, used, warnings = llm.run_json(_draft_prompt(c, body, langs), catalog.default())
    if used == "template":
        raise HTTPException(503, "No writing model is set up (connect ChatGPT in the assistant, or set GEMINI_API_KEY), so the message "
                                 "cannot be written for you. You can still type it in each language yourself. " + " ".join(warnings))
    texts = {l: _clean_text((raw.get("texts") or {}).get(l)) for l in langs} if isinstance(raw.get("texts"), dict) else {}
    texts = {l: t for l, t in texts.items() if t}
    if body.manual:
        texts[body.manual.lang] = body.manual.text.strip()  # their own words stay exactly as written
    return {"reply": str(raw.get("reply") or "").strip()[:300], "texts": texts, "missing": [l for l in langs if l not in texts],
            "provider": catalog.label(used), "warnings": warnings}


# ---------------- creating and listing ----------------

class NoticeIn(BaseModel):
    kind: Kind
    texts: dict[str, str]
    outcomes: list[str] = Field(min_length=1)
    send_at: str | None = None  # ISO time with a timezone; empty = send now


def _campaign(cid: str) -> dict:
    c = store.campaign(cid)
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


def _parse(iso: str) -> datetime:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def view(n: dict) -> dict:
    t = n.get("targets") or []
    return {k: n.get(k) for k in ("id", "campaign_id", "kind", "status", "send_at", "created_at", "finished_at", "outcomes", "texts", "note")} | {
        "progress": {"total": len(t), "delivered": sum(x["state"] == "delivered" for x in t),
                     "failed": sum(x["state"] == "failed" for x in t), "waiting": sum(x["state"] in ("pending", "calling") for x in t)},
        "targets": [{"name": x["name"], "state": x["state"], "attempts": x["attempts"], "call_id": x.get("call_id")} for x in t]}


@router.get("/campaigns/{cid}/notices/audience")
def audience_counts(cid: str, outcomes: str = ""):
    """How many people each group has right now, and (for `outcomes`, comma separated) how many different people the chosen groups
    add up to: someone can be in two groups, e.g. confirmed and unfinished."""
    c = _campaign(cid)
    recs = store.recipients(cid)
    chosen = [o for o in outcomes.split(",") if o in CHOICES]
    return {"counts": counts(c, recs), "labels": LABELS, "total": len(recs), "selected": len(audience(c, recs, chosen)) if chosen else 0}


@router.get("/campaigns/{cid}/notices")
def list_for(cid: str):
    _campaign(cid)
    return {"notices": [view(n) for n in store.list_notices(cid)]}


@router.post("/campaigns/{cid}/notices", status_code=201)
def create(cid: str, body: NoticeIn):
    c = _campaign(cid)
    bad = [o for o in body.outcomes if o not in CHOICES]
    if bad:
        raise HTTPException(400, f"Unknown audience: {', '.join(bad)}")
    texts = {l: _clean_text(t) for l, t in body.texts.items() if l in c["languages"]}
    missing = [LANGUAGES[l] for l in c["languages"] if not texts.get(l)]
    if missing:
        raise HTTPException(400, f"Write the message in {', '.join(missing)} too: each person hears it in their own language")
    now = datetime.now(timezone.utc)
    when = now
    if body.send_at:
        try:
            when = _parse(body.send_at)
        except ValueError:
            raise HTTPException(400, "The send time is not valid")
        if when < now - timedelta(minutes=2):
            raise HTTPException(400, "That time has already passed")
        if when > now + timedelta(days=90):
            raise HTTPException(400, "Schedule it within 90 days")
        when = max(when, now)
    n = {"id": f"n-{secrets.token_hex(5)}", "campaign_id": cid, "kind": body.kind, "texts": texts, "outcomes": list(dict.fromkeys(body.outcomes)),
         "send_at": when.isoformat(timespec="seconds"), "created_at": now_iso(), "status": "scheduled", "targets": [], "note": None}
    store.save_notice(n)
    return view(n)


@router.post("/notices/{nid}/cancel")
def cancel(nid: str):
    n = store.get_notice(nid)
    if not n:
        raise HTTPException(404, "Not found")
    if n["status"] in ("scheduled", "sending"):
        n["status"], n["finished_at"] = "cancelled", now_iso()
        store.save_notice(n)
    return view(n)


# ---------------- delivery ----------------

def _system_prompt(kind: str) -> str:
    return (f"You are a friendly, professional phone assistant calling on behalf of {{org}}. You are speaking with {{name}} in {{language}}. "
            f"You are calling only to pass on a {kind} about {{title}}: you are not collecting a decision or any answers. "
            "Say the message in your own natural words, keeping every fact exactly, then ask if they have any questions. Answer only from "
            "the facts you were given and what the message says; if you do not know, say someone from the organisation will follow up. "
            "Never invent dates, prices or promises. If they have no questions, thank them, say goodbye and end the call.")


async def call_notice(n: dict, c: dict, r: dict, phone: webphone.Phone) -> tuple[bool, str]:
    """Ring the phone, say the message, return (delivered, call id). Delivered = answered and the agent spoke to them."""
    sid = f"web-{secrets.token_hex(6)}"
    provider = c.get("provider") or c.get("agent_provider") or catalog.default()
    lang = r["language"]
    text = n["texts"].get(lang) or next(iter(n["texts"].values()))
    # The campaign as the shared conversation code reads it: this call's only script is the message, and there is nothing to record.
    nc = c | {"mode": "live", "questions": [], "escalation": 0, "_notice": n["kind"], "system_prompt": _system_prompt(n["kind"]),
              "scripts": {lang: {"greeting": "", "message": text}}}
    answered = heard = False
    phone.state = "ringing"
    while not phone.inbox.empty():
        phone.inbox.get_nowait()
    try:
        await phone.send({"type": "incoming", "call_id": sid, "campaign": c["name"], "recipient": r["name"], "provider": catalog.label(provider),
                          "language": LANGUAGES.get(lang, lang), "mode": "live",
                          "notice": {"id": n["id"], "kind": n["kind"], "campaign_id": c["id"], "recipient_id": r["id"]}})
        log.info("notice %s: ringing the phone for %s", n["id"], r["id"])
        picked_up = asyncio.Event()
        convo = asyncio.create_task(webphone._converse(nc, r, phone, sid, provider, picked_up))
        try:
            if await webphone._wait_answer(phone):
                answered = True
                phone.state = "in_call"
                picked_up.set()
                heard = await convo
        finally:
            if not convo.done():
                convo.cancel()
    except Exception:
        log.exception("notice call failed")
    finally:
        phone.state = "idle"
        if phone in webphone._phones:
            try:
                await phone.send({"type": "ended"})
            except Exception:
                pass
    return answered and heard, sid


async def _deliver(nid: str, rid: str, phone: webphone.Phone) -> None:
    global _busy
    try:
        n, r = store.get_notice(nid), store.recipient(rid)
        c = store.campaign(n["campaign_id"]) if n else None
        ok, sid = (await call_notice(n, c, r, phone)) if n and c and r else (False, "")
        n = store.get_notice(nid)  # re-read: it may have been cancelled during the call
        if not n:
            return
        t = next((x for x in n["targets"] if x["rid"] == rid), None)
        if t:
            t["call_id"] = sid or t.get("call_id")
            if ok:
                t["state"] = "delivered"
            elif t["attempts"] >= MAX_ATTEMPTS:
                t["state"] = "failed"
            else:
                t["state"], t["next_at"] = "pending", (datetime.now(timezone.utc) + timedelta(minutes=GAP_MINUTES)).isoformat(timespec="seconds")
            store.save_notice(n)
    except Exception:
        log.exception("notice delivery failed")
    finally:
        _busy = False


def _finish_if_done(n: dict) -> bool:
    if all(x["state"] in ("delivered", "failed") for x in n["targets"]):
        n["status"], n["finished_at"] = "done", now_iso()
        return True
    return False


def _advance(n: dict) -> None:
    """Start the next call of a sending notice, if the phone is free."""
    global _busy
    now = datetime.now(timezone.utc)
    if _finish_if_done(n):
        store.save_notice(n)
        return
    if _busy:
        return
    c = store.campaign(n["campaign_id"])
    if not c:
        n["status"], n["finished_at"] = "cancelled", now_iso()
        store.save_notice(n)
        return
    due = next((x for x in n["targets"] if x["state"] == "pending" and (not x.get("next_at") or _parse(x["next_at"]) <= now)), None)
    if not due:
        return
    r = store.recipient(due["rid"])
    if not r:
        due["state"] = "failed"
        store.save_notice(n)
        return
    if r["in_flight"]:  # in the middle of one of the campaign's own calls: try again shortly
        due["next_at"] = (now + timedelta(seconds=45)).isoformat(timespec="seconds")
        store.save_notice(n)
        return
    phone = webphone.idle_phone()
    if not phone:
        if n.get("note") != WAITING:
            n["note"] = WAITING
            store.save_notice(n)
        return
    if n.get("note"):
        n["note"] = None
    due["state"], due["attempts"] = "calling", due["attempts"] + 1
    store.save_notice(n)
    phone.state = "ringing"  # reserve it before the task starts
    _busy = True
    asyncio.create_task(_deliver(n["id"], due["rid"], phone))


async def _tick() -> None:
    now = datetime.now(timezone.utc)
    for n in store.list_notices(None, ["scheduled", "sending"]):
        if n["status"] == "scheduled":
            if _parse(n["send_at"]) > now:
                continue
            c = store.campaign(n["campaign_id"])
            if not c:
                n["status"], n["finished_at"] = "cancelled", now_iso()
                store.save_notice(n)
                continue
            who = audience(c, store.recipients(n["campaign_id"]), n["outcomes"])  # decided now, at send time
            n["targets"] = [{"rid": r["id"], "name": r["name"], "state": "pending", "attempts": 0, "next_at": None} for r in who]
            n["status"] = "sending"
            log.info("notice %s: sending to %d people", n["id"], len(who))
            store.save_notice(n)
        _advance(n)


async def run() -> None:
    for n in store.list_notices(None, ["sending"]):  # a restart interrupted these calls: they were not delivered
        for t in n["targets"]:
            if t["state"] == "calling":
                t["state"] = "pending"
        store.save_notice(n)
    while True:
        await asyncio.sleep(2)
        try:
            await _tick()
        except Exception:
            log.exception("notice tick failed")
