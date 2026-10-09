"""Campaign drafting: ONE language-model call per campaign writes every script in every
language plus a retry policy. A human reviews the result before launch.

The writing model is Gemini (catalog.drafter picks it from the campaign's provider). With no
model configured the built-in templates are used (English only; other languages are marked as
placeholders).
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

from . import callctx, catalog, chatgpt
from .store import LANGUAGES

log = logging.getLogger("reachout.llm")

FIELDS = ("greeting", "message", "menu", "voicemail", "goodbye")
# Follow-up keypad questions after the main 1/2/3 answer, chosen per event by the model.
# MAX_QUESTIONS is only a safety bound: a campaign can have as many follow-ups as it needs. When drafting, the model is
# asked for MIN_QUESTIONS to DRAFT_QUESTIONS of them.
MAX_QUESTIONS, MIN_QUESTIONS, MIN_OPTIONS, MAX_OPTIONS = 500, 3, 2, 6
DRAFT_QUESTIONS = 4
KIND_LABEL = {"seminar": "seminar or event invitation", "clinic": "clinic appointment reminder",
              "school": "school notice to parents", "payment": "payment reminder"}
DTMF = [  # fixed by design; the model only words the prompt for it
    {"digit": "1", "action": "confirm"}, {"digit": "2", "action": "decline"}, {"digit": "3", "action": "reschedule"},
]
# With the assistant on, the call ends by asking for questions; a caller who starts speaking reaches the agent.
DOUBTS = "Do you have any other questions? Please ask now and our assistant will help you. Otherwise, you can hang up."

def _when(e: dict) -> str:
    return " at ".join(x for x in (e.get("date", "").strip(), e.get("time", "").strip()) if x)


def template(e: dict, escalation: bool) -> dict[str, str]:
    org, title, when, venue = e.get("org") or "us", e["title"], _when(e), e.get("venue", "").strip()
    where = f", at {venue}" if venue else ""
    message = {
        "seminar": f"You are invited to {title}, on {when}{where}.",
        "clinic": f"This is a reminder of your appointment, {title}, on {when}{where}.",
        "school": f"This is to inform you about {title}, on {when}{where}.",
        "payment": f"This is a reminder that {title} is due on {when}.",
    }.get(e["kind"], f"This is about {title}, on {when}{where}.")
    if e.get("details", "").strip():
        message += " " + e["details"].strip()
    menu = ("Press 1 if you will pay by then, 2 if you have already paid, 3 if you need more time"
            if e["kind"] == "payment" else "Press 1 to confirm, 2 if you cannot make it, 3 to reschedule")
    return {
        "greeting": f"Hello {{name}}, this is a message from {org}.",
        "message": message,
        "menu": menu + ".",
        "voicemail": f"Sorry we missed you. This is {org}, about {title} on {when}. We will call again.",
        "goodbye": "Thank you. Goodbye.",
    } | ({"doubts": DOUBTS} if escalation else {})


def question_text(q: dict) -> str:
    """English fallback for a question's spoken prompt: the label, then every option with its key."""
    opts = ", ".join(f"{i + 1} for {o}" for i, o in enumerate(q["options"]))
    return f"{q['label']}. Press {opts}."


def _prompt(e: dict, langs: list[str], escalation: bool) -> list[dict]:
    menu = "1 = confirm / will attend, 2 = decline / cannot attend, 3 = reschedule / need more time"
    doubts = ("\n- doubts: asked at the very end, after every answer: in one or two short sentences, ask whether they have "
              "any other questions or doubts about this event, invite them to ask now (an assistant will answer), and say "
              "they can hang up otherwise. Do not mention any key.") if escalation else ""
    shape = {"name": "short campaign name",
             "questions": [{"id": "q1", "label": "short English label for the dashboard", "only_if_confirmed": True,
                            "options": ["English option label", "another option", "Other"]}],
             "scripts": {l: {f: "..." for f in FIELDS} | {"questions": {"q1": "spoken question with every option and its key"}}
                         | ({"doubts": "..."} if escalation else {}) for l in langs},
             "system_prompt": "instructions for the voice agent that talks to each person",
             "retry": {"max_attempts": 3, "gap_hours": 4}, "notes": "anything the reviewer should check"}
    user = f"""Organisation: {e.get('org') or 'not given'}
Campaign type: {KIND_LABEL.get(e['kind'], e['kind'])}
Title: {e['title']}
Date and time: {_when(e) or 'not given'}
Venue: {e.get('venue') or 'not given'}
Details: {e.get('details') or 'none'}
Languages: {', '.join(f'{LANGUAGES[l]} ({l})' for l in langs)}

Rules:
- Write each language natively in its own script (e.g. Devanagari for Hindi and Marathi), as simple, polite spoken text.
- greeting: use the placeholder {{name}} exactly once for the recipient's name. Use no other placeholders anywhere.
- message: under 45 words; include the date, time and venue if given.
- menu: the keypad options in this order: {menu}.
- voicemail: under 30 words; say we will call again.
- goodbye: one short sentence.{doubts}
- questions: after the menu answer the call collects the details the organiser needs from each person, as follow-up
  multiple-choice questions. Return {MIN_QUESTIONS} to {DRAFT_QUESTIONS} for EVERY event, whatever its type (not fewer than {MIN_QUESTIONS}): think
  about everything an organiser of exactly this event needs to know from each person to plan it, and ask for all of it.
  Aim for {DRAFT_QUESTIONS} whenever you can think of that many useful ones. Return [] only if the call is a bare notice
  where nothing could be planned from the answers. Order them most important first. Typical ones by kind of event (pick and adapt, never copy blindly):
    hackathon, contest or fest: team status (I have a team / I need a team / Solo), food preference, T-shirt size, laptop or equipment
    workshop or training: skill level, laptop or materials, preferred batch or time slot, dietary needs
    school notice or parent-teacher meeting: who will attend (Mother / Father / Both / Someone else), preferred time slot, language or translator needed, transport
    clinic or health camp: preferred time slot, transport or wheelchair help needed, documents or reports ready, who is coming with the patient
    payment or fee reminder: when they will pay (Today / This week / After the due date), payment mode (Online / Cash at the office / Cheque / Bank transfer), instalment or help needed
    religious, community or society event: number attending (1 / 2 / 3 / 4 or more), pickup or transport needed, willing to volunteer or bring something, agenda item to raise
    volunteer or staff confirmation: shift, T-shirt size, own transport, previous experience
    seminar, conference or talk: number attending, session or topic of interest, dietary needs, accessibility needs
  Each question has {MIN_OPTIONS} to {MAX_OPTIONS} short options (keys 1, 2, 3... in order). Use yes/no (2 options) ONLY for
  a truly yes/no question. Otherwise give every realistic choice, 3 to {MAX_OPTIONS} options, and include "Other" or "None"
  when some people will not fit the listed ones (e.g. food preference: Vegetarian, Non-vegetarian, Other dietary needs;
  T-shirt size: S, M, L, XL, XXL). Options must be short, mutually exclusive, and cover the likely answers.
  only_if_confirmed: set true only when the question makes sense solely for people who said yes (food, T-shirt size,
  seating). Set it false when it matters for everyone who answers: a payment reminder's "when will you pay" and "how"
  questions, "what time suits you" for people who want to reschedule, "why can you not come" for people who decline.
  Mix them where the event allows; do not mark every question true. Never ask for anything private beyond what
  the event needs (no ID numbers, bank details, health history). Do not invent facts in a question.
  label and options are in English (they are shown on the dashboard). In every language's script, "questions" holds
  the spoken question for each id, reading out every option with its key in order, e.g. "What would you like for
  lunch? Press 1 for vegetarian, 2 for non-vegetarian, 3 for other dietary needs."
- retry: suggest max_attempts (1 to 4) and gap_hours (1 to 48) suited to this campaign type.
- system_prompt: the instructions for the live voice agent that will phone each person and hold a natural conversation
  (in English, 120 to 250 words, second person: "You are..."). Cover: who it calls for and why, the tone suited to this
  kind of call, how the call flows (greet by name, give the key facts briefly, ask for their decision, then any follow-up
  questions, then invite questions and say goodbye), what it may answer (only the facts above; say someone will follow up
  when it does not know; never invent dates, prices or promises), and how to behave (short spoken sentences, no lists,
  stop when interrupted, end politely if asked to stop calling). Use the placeholders {{name}} and {{language}} for the
  person's name and language, and no others.

Reply with JSON only, in exactly this shape:
{json.dumps(shape, ensure_ascii=False)}"""
    system = ("You write short phone-call scripts for an automated, keypad-first outbound calling system "
              "used by Indian schools, clinics and event organisers. Reply with JSON only.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def _gemini(messages: list[dict]) -> dict:
    from google import genai
    from google.genai import types
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = [types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["content"])])
                for m in messages if m["role"] != "system"]
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL") or "gemini-3.1-flash-lite", contents=contents,
        config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json", temperature=0.3,
                                           thinking_config=types.ThinkingConfig(thinking_budget=0)))
    return _parse(resp.text or "")


def _chatgpt(messages: list[dict]) -> dict:
    return _parse(chatgpt.complete(messages))


def run_json(messages: list[dict], requested: str) -> tuple[dict, str, list[str]]:
    """One model call returning JSON: (reply, provider that wrote it, warnings). A connected ChatGPT plan writes it
    first (it costs us nothing); if it is not connected, out of allowance or fails, the requested provider writes it
    if it can; otherwise the first configured one that can; with none usable the reply is {} and the provider is
    "template". Each step leaves a warning."""
    warnings: list[str] = []
    if chatgpt.connected():
        try:
            return _chatgpt(messages), "chatgpt", warnings
        except Exception as exc:  # plan limit, ineligible account, network: fall back to our own models
            reason = exc.reason if isinstance(exc, chatgpt.ChatGPTError) else type(exc).__name__
            log.warning("call via chatgpt failed: %s", type(exc).__name__)
            warnings.append(f"ChatGPT failed ({reason}).")
    run = {"gemini": _gemini}
    first = catalog.drafter(requested)
    if first is None:
        warnings.append("No writing model is set up (set GEMINI_API_KEY), so the built-in templates were used.")
        return {}, "template", warnings
    if first != requested:
        warnings.append(f"{catalog.label(requested)} cannot write text, so {catalog.label(first)} wrote the drafts.")
    for p in dict.fromkeys([first, *(k for k in catalog.ORDER if catalog.ready(k, "draft"))]):
        try:
            return run[p](messages), p, warnings
        except Exception as exc:  # network, HTTP or JSON: never block the person on it
            log.warning("call via %s failed: %s", p, type(exc).__name__)
            warnings.append(f"{catalog.label(p)} failed ({type(exc).__name__}).")
    warnings.append("Used the built-in templates instead.")
    return {}, "template", warnings


def draft(e: dict, langs: list[str], provider: str, escalation: bool) -> tuple[dict, list[str]]:
    """Returns (draft, warnings). The chosen provider writes it if it can, else another configured one, else the
    templates. Each step leaves a warning."""
    base = template(e, escalation)
    raw, provider, warnings = run_json(_prompt(e, langs, escalation), provider)

    questions, renamed = _questions(raw.get("questions"))
    scripts, placeholders = {}, []
    got = raw.get("scripts") if isinstance(raw.get("scripts"), dict) else {}
    for l in langs:
        s = got.get(l) if isinstance(got.get(l), dict) else {}
        ok = all(isinstance(s.get(f), str) and s[f].strip() for f in FIELDS)
        if ok:
            scripts[l] = {f: s[f].strip() for f in FIELDS} | {"placeholder": False}
        else:
            if provider != "template":
                warnings.append(f"The model returned no usable {LANGUAGES[l]} script; used the template.")
            scripts[l] = base | {"placeholder": l != "en"}
            if l != "en":
                placeholders.append(LANGUAGES[l])
        if escalation:
            got_doubts = s.get("doubts") if ok and isinstance(s.get("doubts"), str) and s["doubts"].strip() else None
            scripts[l]["doubts"] = (got_doubts or DOUBTS).strip()[:300]
            if not got_doubts and l != "en" and not scripts[l]["placeholder"]:
                warnings.append(f"{LANGUAGES[l]}: the closing question is in English. Translate it before launch.")
        spoken = s.get("questions") if ok and isinstance(s.get("questions"), dict) else {}
        scripts[l]["questions"] = {}
        for old, q in renamed.items():
            text = spoken.get(old)
            if isinstance(text, str) and text.strip():
                scripts[l]["questions"][q["id"]] = text.strip()[:400]
            else:
                scripts[l]["questions"][q["id"]] = question_text(q)
                if l != "en":
                    warnings.append(f"{LANGUAGES[l]}: the question “{q['label']}” is in English. Translate it before launch.")
    if placeholders:
        warnings.append(f"{', '.join(placeholders)}: English placeholder text. Translate it before launch, "
                        "or set GEMINI_API_KEY so a model drafts it.")
    for l, s in scripts.items():
        if "{name}" not in s["greeting"]:
            warnings.append(f"{LANGUAGES[l]} greeting does not use {{name}}.")
        if any(m != "{name}" for t in [*(s[f] for f in FIELDS), *s["questions"].values()]
               for m in re.findall(r"\{[^}]*\}", t)):
            warnings.append(f"{LANGUAGES[l]} script has a placeholder other than {{name}}; it will be read out literally.")

    r = raw.get("retry") if isinstance(raw.get("retry"), dict) else {}
    retry = {"max_attempts": _clamp(r.get("max_attempts"), 1, 4, 2 if e["kind"] == "payment" else 3),
             "gap_hours": _clamp(r.get("gap_hours"), 1, 48, 24 if e["kind"] == "payment" else 4)}
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw["name"].strip() else e["title"]
    sp = raw.get("system_prompt") if isinstance(raw.get("system_prompt"), str) and len(raw["system_prompt"].strip()) >= 80 else ""
    if not sp:
        if provider != "template":
            warnings.append("The model wrote no usable agent prompt; used the standard one.")
        sp = callctx.default_system_prompt(e, questions)
    return {"name": name.strip()[:80], "scripts": scripts, "questions": questions, "retry": retry, "dtmf": DTMF,
            "system_prompt": sp.strip()[:4000], "notes": str(raw.get("notes") or "")[:500], "provider": provider}, warnings


def _questions(raw) -> tuple[list[dict], dict[str, dict]]:
    """Untrusted model output -> at most DRAFT_QUESTIONS clean questions with ids q1..qN.
    Also returns {the model's id: question} so each language's spoken text can be matched up."""
    out, renamed = [], {}
    for q in raw if isinstance(raw, list) else []:
        if len(out) == DRAFT_QUESTIONS or not isinstance(q, dict):
            break
        label = str(q.get("label") or "").strip()[:60]
        opts = [str(o).strip()[:40] for o in q.get("options") or [] if str(o).strip()][:MAX_OPTIONS]
        if not label or len(opts) < MIN_OPTIONS:
            continue
        clean = {"id": f"q{len(out) + 1}", "label": label, "options": opts,
                 "only_if_confirmed": q.get("only_if_confirmed") is not False}
        out.append(clean)
        renamed[str(q.get("id") or clean["id"])] = clean
    return out, renamed


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def translate_scripts(source: dict, event: dict, langs: list[str], questions: list[dict], provider: str) -> tuple[dict, list[str]]:
    """The campaign's approved script in new languages (one model call), written natively in each language's script.
    `source` is an existing language's script (fields, closing question and the spoken text of each question). A language
    the model does not return usable text for gets the source wording unchanged (still English or whatever it was), and is
    named in the warnings so the person can fix it."""
    keep = [*FIELDS, "doubts"]
    src = {k: source.get(k, "") for k in keep if source.get(k)} | {"questions": source.get("questions") or {}}
    shape = {l: {k: "..." for k in src if k != "questions"} | {"questions": {q["id"]: "..." for q in questions}} for l in langs}
    msgs = [
        {"role": "system", "content": "You translate phone-call scripts for an automated calling system used by Indian schools, "
                                      "clinics and event organisers. Reply with JSON only."},
        {"role": "user", "content": f"""The approved script for this call, in the language it is already written in:
{json.dumps(src, ensure_ascii=False)}

Facts about the call (for context only): {json.dumps({k: v for k, v in event.items() if v}, ensure_ascii=False)}

Translate it into each of these languages, written natively in its own script, as simple, polite spoken text:
{", ".join(f"{LANGUAGES[l]} ({l})" for l in langs)}.
Keep the meaning and every fact (dates, times, venues, amounts, names) exactly. Keep the placeholder {{name}} in the greeting
exactly once, and add no other placeholders. For the keypad menu and the questions keep the structure: the question, then which
key to press for each option, in the same order, with the key numbers as digits.

Reply with JSON only, in exactly this shape: {json.dumps(shape, ensure_ascii=False)}"""}]
    raw, used, warnings = run_json(msgs, provider)
    out, bad = {}, []
    for l in langs:
        got = raw.get(l) if isinstance(raw.get(l), dict) else {}
        ok = all(isinstance(got.get(k), str) and got[k].strip() for k in src if k != "questions") and "{name}" in str(got.get("greeting", ""))
        qs = got.get("questions") if isinstance(got.get("questions"), dict) else {}
        if ok:
            out[l] = {k: got[k].strip() for k in src if k != "questions"} | {
                "questions": {q["id"]: (qs.get(q["id"]) or (source.get("questions") or {}).get(q["id"]) or question_text(q)).strip() for q in questions}}
        else:
            bad.append(LANGUAGES[l])
            out[l] = {k: v for k, v in src.items() if k != "questions"} | {"questions": dict(source.get("questions") or {})}
    if bad:
        warnings.append(f"No usable translation for {', '.join(bad)}: the original wording was copied there. Edit it before launch.")
    return out, warnings
