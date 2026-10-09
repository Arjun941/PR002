"""Campaign builder (Phase 4): event details -> one drafting call -> human review -> cost
estimate -> launch. Launch places real calls when the dialer is ready, simulates them in
demo mode, and is refused otherwise.
"""
from __future__ import annotations

import csv
import io
import os
import re
import secrets
from collections import Counter
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import audio, chatgpt, costs, dialer, elevenlabs, llm, store
from .store import LANGUAGES, now_iso

router = APIRouter(prefix="/api")

DEMO = os.getenv("DEMO", "0") == "1"
KINDS = {"seminar": "Seminar invitation", "clinic": "Clinic reminder", "school": "School notice",
         "payment": "Payment reminder"}
MAX_CONTACTS = 5000
Kind = Literal["seminar", "clinic", "school", "payment"]


class Event(BaseModel):
    org: str = Field("", max_length=80)
    kind: Kind
    title: str = Field(min_length=1, max_length=120)
    date: str = Field("", max_length=40)
    time: str = Field("", max_length=40)
    venue: str = Field("", max_length=120)
    details: str = Field("", max_length=600)


class Script(BaseModel):
    greeting: str = Field(min_length=1, max_length=400)
    message: str = Field(min_length=1, max_length=800)
    menu: str = Field(min_length=1, max_length=400)
    voicemail: str = Field(min_length=1, max_length=400)
    goodbye: str = Field(min_length=1, max_length=200)
    questions: dict[str, str] = Field(default_factory=dict)  # spoken text per follow-up question id
    doubts: str = Field("", max_length=300)  # closing "any other questions?" (campaigns with the assistant)


class Question(BaseModel):
    """A follow-up keypad question after the main 1/2/3 answer; option n is key n."""
    id: str = Field(pattern=r"^q[1-9]$")
    label: str = Field(min_length=1, max_length=60)
    options: list[str] = Field(min_length=llm.MIN_OPTIONS, max_length=llm.MAX_OPTIONS)
    only_if_confirmed: bool = True


class Retry(BaseModel):
    max_attempts: int = Field(ge=1, le=4)
    gap_hours: int = Field(ge=1, le=48)


class DraftReq(BaseModel):
    event: Event
    languages: list[str] = Field(min_length=1)
    text_provider: Literal["template", "chatgpt", "ollama", "sarvam"] = "template"
    escalation: bool = True


class ContactsReq(BaseModel):
    csv: str = Field(max_length=1_000_000)
    languages: list[str] = Field(min_length=1)


class EstimateReq(BaseModel):
    kind: Kind
    by_language: dict[str, int]
    scripts: dict[str, Script]
    retry: Retry
    questions: list[Question] = Field(default_factory=list, max_length=llm.MAX_QUESTIONS)
    escalation: bool
    record: bool
    voice_provider: Literal["piper", "sarvam", "elevenlabs"]
    text_provider: Literal["template", "chatgpt", "ollama", "sarvam"]


class CreateReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    event: Event
    languages: list[str] = Field(min_length=1)
    text_provider: Literal["template", "chatgpt", "ollama", "sarvam"]
    voice_provider: Literal["piper", "sarvam", "elevenlabs"]
    escalation: bool
    record: bool
    contacts_csv: str = Field(max_length=1_000_000)
    scripts: dict[str, Script]
    questions: list[Question] = Field(default_factory=list, max_length=llm.MAX_QUESTIONS)
    retry: Retry
    reviewed: bool


def _check_langs(langs: list[str]) -> list[str]:
    bad = [l for l in langs if l not in LANGUAGES]
    if bad:
        raise HTTPException(400, f"Unknown language: {', '.join(bad)}")
    return list(dict.fromkeys(langs))


def escalation_ready() -> bool:
    """Key 4 runs on the ElevenLabs agent; without it campaigns are keypad only."""
    return elevenlabs.agent_ready()


def launch_mode() -> str:
    return "live" if dialer.ready() else "simulated" if DEMO else "unavailable"


# ---------- contacts ----------

def normalise_phone(raw: str) -> str | None:
    p = re.sub(r"[\s\-().]", "", raw)
    if re.fullmatch(r"[6-9]\d{9}", p):
        return "+91" + p
    if re.fullmatch(r"0[6-9]\d{9}", p):
        return "+91" + p[1:]
    if re.fullmatch(r"91[6-9]\d{9}", p):
        return "+" + p
    if re.fullmatch(r"\+\d{8,15}", p):
        return p
    return None


def _lang_code(value: str) -> str | None:
    v = value.strip().lower()
    return next((code for code, name in LANGUAGES.items() if v in (code, name.lower())), None)


def parse_contacts(text: str, langs: list[str]) -> tuple[list[dict], list[dict]]:
    """CSV of name, phone[, language][, segment]. A header row is optional."""
    rows = [r for r in csv.reader(io.StringIO(text.strip())) if any(c.strip() for c in r)]
    cols = {"name": 0, "phone": 1, "language": 2, "segment": 3}
    start = 0
    if rows and any("phone" in c.lower() or "number" in c.lower() for c in rows[0]):
        header = [c.strip().lower() for c in rows[0]]
        cols = {k: next((i for i, h in enumerate(header) if k in h or (k == "phone" and "number" in h)), None)
                for k in cols}
        start = 1
        if cols["name"] is None or cols["phone"] is None:
            return [], [{"line": 1, "error": "Header needs a name column and a phone column"}]
    contacts, errors, seen = [], [], set()
    for i, row in enumerate(rows[start:], start=start + 1):
        get = lambda k: row[cols[k]].strip() if cols[k] is not None and cols[k] < len(row) else ""
        name, phone_raw, lang_raw = get("name"), get("phone"), get("language")
        if len(contacts) >= MAX_CONTACTS:
            errors.append({"line": i, "error": f"More than {MAX_CONTACTS:,} contacts; split the list"})
            break
        if not name:
            errors.append({"line": i, "error": "Missing name"})
            continue
        phone = normalise_phone(phone_raw)
        if not phone:
            errors.append({"line": i, "error": "Phone number is not valid"})
            continue
        if phone in seen:
            errors.append({"line": i, "error": "Duplicate phone number"})
            continue
        lang = _lang_code(lang_raw) if lang_raw else langs[0]
        if lang not in langs:
            errors.append({"line": i, "error": f"Language “{lang_raw}” is not one of this campaign's languages"})
            continue
        seen.add(phone)
        contacts.append({"name": name[:60], "phone": phone, "language": lang, "segment": get("segment")[:40] or "General"})
    return contacts, errors


# ---------- endpoints ----------

@router.get("/builder/options")
def options():
    return {
        "languages": [{"code": c, "name": n} for c, n in LANGUAGES.items()],
        "kinds": [{"value": k, "label": l} for k, l in KINDS.items()],
        "text_providers": [{"key": k, **v, "available": llm.available(k), "live": llm.live(k)}
                           for k, v in llm.TEXT_PROVIDERS.items()],
        # Only providers that can synthesise call audio can launch real calls; any can be simulated.
        "voice_providers": [{"key": k, **v, "available": audio.ready(k)} for k, v in llm.VOICE_PROVIDERS.items()],
        "escalation": {"available": escalation_ready(), "label": "ElevenLabs agent",
                       "sends": "Callers who ask a question at the end: their voice goes to ElevenLabs, USA"},
        "chatgpt": chatgpt.status(),
        "launch_mode": launch_mode(),
        "call_window": os.getenv("CALL_WINDOW", "09:00-20:00"),
    }


@router.post("/builder/draft")
def draft(body: DraftReq):
    """The one drafting call for this campaign (sync: the provider client blocks)."""
    d, warnings = llm.draft(body.event.model_dump(), _check_langs(body.languages), body.text_provider,
                            body.escalation and escalation_ready())
    return {"draft": d, "warnings": warnings}


@router.post("/builder/contacts")
def contacts(body: ContactsReq):
    langs = _check_langs(body.languages)
    rows, errors = parse_contacts(body.csv, langs)
    by_lang = Counter(r["language"] for r in rows)
    return {
        "count": len(rows),
        "by_language": {l: by_lang.get(l, 0) for l in langs},
        "segments": dict(Counter(r["segment"] for r in rows).most_common()),
        "errors": errors[:20], "error_count": len(errors),
        "preview": [{"name": r["name"], "phone": store.mask_phone(r["phone"]),
                     "language": LANGUAGES[r["language"]], "segment": r["segment"]} for r in rows[:5]],
    }


@router.post("/builder/estimate")
def estimate(body: EstimateReq):
    _check_langs(list(body.by_language))
    escalation = body.escalation and escalation_ready()
    return costs.estimate(kind=body.kind, by_language=body.by_language,
                          scripts={l: s.model_dump() for l, s in body.scripts.items()},
                          max_attempts=body.retry.max_attempts, escalation=escalation, questions=len(body.questions),
                          voice=body.voice_provider, text=body.text_provider) | {
        "handling": handling(body.voice_provider, body.text_provider, escalation, body.record)}


def handling(voice: str, text: str, escalation: bool, record: bool) -> dict:
    v = llm.VOICE_PROVIDERS[voice]
    return {
        "audio": {"provider": v["label"], "note": v["sends"]},
        "text": ({"provider": "ElevenLabs agent", "note": "Only callers who ask a question at the end; their voice goes to ElevenLabs, USA"}
                 if escalation else {"provider": "Keypad only", "note": "No speech is processed"}),
        "recordings": ({"provider": "Exotel", "note": "Stored in India, played back only after unlocking"}
                       if record else {"provider": "Not recorded", "note": "No call audio is kept"}),
    }


@router.post("/campaigns", status_code=201)
def create(body: CreateReq):
    if not body.reviewed:
        raise HTTPException(400, "Review the scripts before launch")
    mode = launch_mode()
    if mode == "unavailable":
        raise HTTPException(503, "Calling is not set up: configure Exotel, PUBLIC_URL and WEBHOOK_TOKEN, "
                                 "or start the server with DEMO=1 to simulate")
    if mode == "live" and not audio.ready(body.voice_provider):
        raise HTTPException(400, f"{llm.VOICE_PROVIDERS[body.voice_provider]['label']} cannot produce call audio yet; "
                                 "choose ElevenLabs (set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)")
    langs = _check_langs(body.languages)
    missing = [LANGUAGES[l] for l in langs if l not in body.scripts]
    if missing:
        raise HTTPException(400, f"Missing scripts for {', '.join(missing)}")
    ids = [q.id for q in body.questions]
    if len(set(ids)) != len(ids) or any(not o.strip() for q in body.questions for o in q.options):
        raise HTTPException(400, "Each question needs its own id and no empty options")
    unspoken = [f"{LANGUAGES[l]} ({q.label})" for l in langs for q in body.questions
                if not body.scripts[l].questions.get(q.id, "").strip()]
    if unspoken:
        raise HTTPException(400, f"Write the spoken question for {', '.join(unspoken[:3])}")
    if body.escalation and escalation_ready():
        silent = [LANGUAGES[l] for l in langs if not body.scripts[l].doubts.strip()]
        if silent:
            raise HTTPException(400, f"Write the closing question (any other questions?) for {', '.join(silent)}")
    rows, errors = parse_contacts(body.contacts_csv, langs)
    if errors:
        raise HTTPException(400, f"Fix {len(errors)} contact row(s) first (line {errors[0]['line']}: {errors[0]['error']})")
    if not rows:
        raise HTTPException(400, "Add at least one contact")

    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:40] or "campaign"
    cid = f"{slug}-{secrets.token_hex(3)}"
    # Escalation needs the agent at call time; keypad-only campaigns never run one.
    escalation = body.escalation and escalation_ready()
    c = {
        # Live campaigns synthesise their audio first (audio.py moves them to running).
        "id": cid, "name": body.name.strip(), "kind": body.event.kind,
        "status": "preparing" if mode == "live" else "running", "voice": body.voice_provider,
        "languages": langs, "segments": list(dict.fromkeys(r["segment"] for r in rows)), "started_at": now_iso(),
        "handling": handling(body.voice_provider, body.text_provider, escalation, body.record),
        "event": body.event.model_dump(),
        "scripts": {l: body.scripts[l].model_dump() | {"questions": {q.id: body.scripts[l].questions[q.id] for q in body.questions}}
                    for l in langs},
        "questions": [q.model_dump() for q in body.questions], "retry": body.retry.model_dump(),
        "record": int(body.record), "escalation": int(escalation), "simulated": int(mode == "simulated"),
    }
    recs = [{
        "id": f"{cid}-{i}", "campaign_id": cid, "name": r["name"], "phone": r["phone"], "language": r["language"],
        "segment": r["segment"], "outcome": "pending", "channel": None, "attempts": 0, "retrying": 0, "in_flight": 0,
        "call_sid": None, "last_attempt_at": None, "recording_url": None,
        "pickup": costs.PICKUP.get(body.event.kind, .6) if mode == "simulated" else None,
    } for i, r in enumerate(rows)]
    store.insert_campaign(c, recs)
    return {"id": cid, "mode": mode, "recipients": len(recs)}
