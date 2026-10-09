"""Campaign builder (Phase 4): event details -> one drafting call (scripts + the agent's system prompt)
-> human review -> cost estimate -> launch. A campaign picks one provider (ElevenLabs or Gemini) and a mode:
  live    the agent takes over the whole call
  hybrid  pre-synthesised IVR with the keypad, then the agent for anyone with a question at the end
Calls ring the phone page (webphone.py); the IVR audio is synthesised when the campaign is created.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
from collections import Counter
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from . import audio, callctx, catalog, costs, llm, recstore, store, webphone
from .store import LANGUAGES, now_iso

router = APIRouter(prefix="/api")

DEMO = os.getenv("DEMO", "0") == "1"
KINDS = {"seminar": "Seminar invitation", "clinic": "Clinic reminder", "school": "School notice",
         "payment": "Payment reminder"}
MAX_CONTACTS = 5000
Provider = Literal["elevenlabs", "gemini"]
Mode = Literal["live", "hybrid"]
MAX_PROMPT = 6000


def normalise_kind(v: str) -> str:
    """The four presets keep their special handling (payment asks for an amount, and so on). Any other
    label is a custom type: stored and shown as typed, handled as a general notice."""
    v = re.sub(r"\s+", " ", v).strip()
    if not v or any(ord(ch) < 32 for ch in v):
        raise ValueError("Give the campaign a type")
    return v.lower() if v.lower() in KINDS else v


# A preset key ("seminar", ...) or a custom label of up to 40 characters.
Kind = Annotated[str, StringConstraints(max_length=40), AfterValidator(normalise_kind)]



def kind_options() -> list[dict]:
    """Presets first, then every custom type used by an earlier campaign, so they can be picked again."""
    used = sorted({c["kind"] for c in store.campaigns() if c["kind"] not in KINDS}, key=str.lower)
    return [{"value": k, "label": l} for k, l in KINDS.items()] + [{"value": k, "label": k} for k in used]


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
    provider: Provider
    mode: Mode = "hybrid"


class ContactsReq(BaseModel):
    csv: str = Field(max_length=1_000_000)
    languages: list[str] = Field(min_length=1)


class EstimateReq(BaseModel):
    kind: Kind
    by_language: dict[str, int]
    scripts: dict[str, Script]
    retry: Retry
    questions: list[Question] = Field(default_factory=list, max_length=llm.MAX_QUESTIONS)
    provider: Provider
    mode: Mode


class CreateReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    event: Event
    languages: list[str] = Field(min_length=1)
    provider: Provider
    mode: Mode
    system_prompt: str = Field("", max_length=MAX_PROMPT)
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


def escalation_ready(provider: str) -> bool:
    """The closing "any other questions?" needs the provider's live conversation."""
    return catalog.ready(provider, "live")


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
        "kinds": kind_options(),
        "providers": [{"key": k, "label": catalog.label(k), "region": p["region"], "sends": p["sends"],
                       "caps": {cap: {"supported": catalog.supports(k, cap), "ready": catalog.ready(k, cap),
                                      "missing": catalog.missing(k, cap)} for cap in p["caps"]}}
                      for k, p in catalog.PROVIDERS.items()],
        "default_provider": catalog.default(),
        "phones": webphone.connected(),
    }


class TranslateReq(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    options: list[str] = Field(min_length=llm.MIN_OPTIONS, max_length=llm.MAX_OPTIONS)
    languages: list[str] = Field(min_length=1)
    provider: Provider | None = None


@router.post("/builder/translate-question")
def translate_question(body: TranslateReq):
    """The spoken text of one keypad question in each of the campaign's languages (one model call). Languages the
    model does not return usable text for get the English wording, and are reported so the page can say so."""
    langs = _check_langs(body.languages)
    q = {"label": body.label.strip(), "options": [o.strip() for o in body.options if o.strip()]}
    english = llm.question_text(q)
    todo = [l for l in langs if l != "en"]
    texts, why = {"en": english} if "en" in langs else {}, []
    if todo:
        shape = {l: "..." for l in todo}
        msgs = [
            {"role": "system", "content": "You translate short phone-call prompts for an automated keypad menu used by Indian schools, "
                                          "clinics and event organisers. Reply with JSON only."},
            {"role": "user", "content": f"""English prompt: {english}

Translate it into each of these languages, written natively in its own script, as simple, polite spoken text:
{", ".join(f"{LANGUAGES[l]} ({l})" for l in todo)}.
Keep the structure: first the question, then which key to press for each option, in the same order. Write the key
numbers as digits. Translate the option names too, but keep names of people, places and brands as they are.

Reply with JSON only, in exactly this shape: {json.dumps(shape, ensure_ascii=False)}"""}]
        raw, used, why = llm.run_json(msgs, body.provider or catalog.default())
        for l in todo:
            t = raw.get(l)
            if isinstance(t, str) and t.strip():
                texts[l] = t.strip()[:400]
    missing = [l for l in langs if l not in texts]
    for l in missing:
        texts[l] = english
    return {"texts": texts, "english_for": missing, "warnings": why if missing else []}


@router.post("/builder/draft")
def draft(body: DraftReq):
    """The one drafting call for this campaign (sync: the provider client blocks)."""
    d, warnings = llm.draft(body.event.model_dump(), _check_langs(body.languages), body.provider,
                            body.mode == "hybrid" and escalation_ready(body.provider))
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
    return costs.estimate(kind=body.kind, by_language=body.by_language,
                          scripts={l: s.model_dump() for l, s in body.scripts.items()},
                          max_attempts=body.retry.max_attempts, questions=len(body.questions),
                          provider=body.provider, mode=body.mode) | {
        "handling": handling(body.provider, body.mode, audio.choose_voice({"provider": body.provider}) or body.provider)}


def handling(provider: str, mode: str, voice: str = "") -> dict:
    p = catalog.PROVIDERS[provider]
    if mode == "live":
        return {"audio": {"provider": p["label"], "note": "The whole call is a live conversation. " + p["sends"]},
                "text": {"provider": p["label"], "note": "Speech is processed live for the whole call"},
                "recordings": recstore.handling({})}
    v = catalog.PROVIDERS.get(voice or provider, p)
    return {"audio": {"provider": v["label"], "note": "IVR phrases and names are synthesised once. " + v["sends"]},
            "text": {"provider": p["label"],
                     "note": "Only callers who ask a question at the end; their voice goes to " + p["region"]},
            "recordings": recstore.handling({})}


class PreviewReq(BaseModel):
    provider: Provider
    language: str
    text: str = Field(min_length=1, max_length=800)


@router.post("/builder/preview-audio")
async def preview_audio(body: PreviewReq):
    """Synthesise one IVR phrase so it can be heard while writing the script. Cached, so launching the campaign
    reuses it (the greeting's {name} is filled with a sample name here; each real name is made at launch)."""
    from fastapi.responses import Response
    _check_langs([body.language])
    voice = audio.choose_voice({"provider": body.provider})
    if not voice:
        raise HTTPException(400, "IVR audio uses ElevenLabs: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID")
    try:
        pcm = await audio.synth_text(voice, body.language, body.text.replace("{name}", "Asha"))
    except Exception as exc:
        raise HTTPException(502, f"{catalog.label(voice)} could not synthesise this ({type(exc).__name__})")
    return Response(audio.wav(pcm), media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.post("/campaigns", status_code=201)
def create(body: CreateReq):
    if not body.reviewed:
        raise HTTPException(400, "Review the scripts before launch")
    hybrid = body.mode == "hybrid"
    if not catalog.ready(body.provider, "live"):
        raise HTTPException(400, f"{catalog.label(body.provider)} is not set up for live calls "
                                 f"(set {', '.join(catalog.missing(body.provider, 'live'))})")
    voice = audio.choose_voice({"provider": body.provider}) if hybrid else ""
    if hybrid and not voice:
        raise HTTPException(400, "Hybrid mode makes its IVR audio with ElevenLabs: set ELEVENLABS_API_KEY and "
                                 "ELEVENLABS_VOICE_ID")
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
    if hybrid:
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
    prompt = body.system_prompt.strip() or callctx.default_system_prompt(
        body.event.model_dump(), [q.model_dump() for q in body.questions])
    c = {
        # Live campaigns synthesise their audio first (audio.py moves them to running).
        "id": cid, "name": body.name.strip(), "kind": body.event.kind,
        "status": "preparing" if hybrid else "running", "voice": voice,
        "languages": langs, "segments": list(dict.fromkeys(r["segment"] for r in rows)), "started_at": now_iso(),
        "handling": handling(body.provider, body.mode, voice),
        "event": body.event.model_dump(),
        "scripts": {l: body.scripts[l].model_dump() | {"questions": {q.id: body.scripts[l].questions[q.id] for q in body.questions}}
                    for l in langs},
        "questions": [q.model_dump() for q in body.questions], "retry": body.retry.model_dump(),
        "record": 0, "escalation": int(hybrid), "agent_provider": body.provider,
        "provider": body.provider, "mode": body.mode, "telephony": "webphone", "system_prompt": prompt, "simulated": 0,
    }
    recs = [{
        "id": f"{cid}-{i}", "campaign_id": cid, "name": r["name"], "phone": r["phone"], "language": r["language"],
        "segment": r["segment"], "outcome": "pending", "channel": None, "attempts": 0, "retrying": 0, "in_flight": 0,
        "call_sid": None, "last_attempt_at": None, "recording_url": None,
        "pickup": None,
    } for i, r in enumerate(rows)]
    store.insert_campaign(c, recs)
    return {"id": cid, "mode": body.mode, "recipients": len(recs)}
