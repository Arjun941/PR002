"""Campaign drafting: ONE language-model call per campaign writes every script in every
language plus a retry policy. A human reviews the result before launch.

Providers are pluggable and each says where text goes. With no model configured the
built-in templates are used (English only; other languages are marked as placeholders).
Ollama and Sarvam request/response details were written from memory and are UNVERIFIED;
check them on the first real run.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

from . import chatgpt
from .store import LANGUAGES

log = logging.getLogger("reachout.llm")

FIELDS = ("greeting", "message", "menu", "voicemail", "goodbye")
# Follow-up keypad questions after the main 1/2/3 answer, chosen per event by the model.
MAX_QUESTIONS, MIN_OPTIONS, MAX_OPTIONS = 4, 2, 6
KIND_LABEL = {"seminar": "seminar or event invitation", "clinic": "clinic appointment reminder",
              "school": "school notice to parents", "payment": "payment reminder"}
DTMF = [  # fixed by design; the model only words the prompt for it
    {"digit": "1", "action": "confirm"}, {"digit": "2", "action": "decline"}, {"digit": "3", "action": "reschedule"},
]
# With the assistant on, the call ends by asking for questions; a caller who starts speaking reaches the agent.
DOUBTS = "Do you have any other questions? Please ask now and our assistant will help you. Otherwise, you can hang up."

TEXT_PROVIDERS = {
    "template": dict(label="Built-in templates", region="This machine", sends="Nothing leaves this machine"),
    # Drafting only (live=False): key-4 assistant calls never run on someone's ChatGPT plan.
    "chatgpt": dict(label="ChatGPT (connected plan)", region="United States", live=False,
                    sends="Event details (text) go to OpenAI, USA, and use the connected ChatGPT plan"),
    "ollama": dict(label="Ollama (local)", region="This machine", sends="Nothing leaves this machine"),
    "sarvam": dict(label="Sarvam", region="India", sends="Event details (text) go to Sarvam, India"),
}
FALLBACK = ["ollama", "sarvam"]  # tried in order, then the templates, when ChatGPT is unavailable
VOICE_PROVIDERS = {
    "piper": dict(label="Piper (local)", region="This machine", sends="Stays on this machine"),
    "sarvam": dict(label="Sarvam", region="India", sends="Processed in India"),
    "elevenlabs": dict(label="ElevenLabs", region="United States",
                       sends="Script text and recipient names go to ElevenLabs, USA"),
}


def available(provider: str) -> bool:
    return {"template": True, "chatgpt": chatgpt.connected(), "ollama": bool(os.getenv("OLLAMA_MODEL")),
            "sarvam": bool(os.getenv("SARVAM_API_KEY"))}.get(provider, False)


def live(provider: str) -> bool:
    """Whether this provider can answer during a call (escalation), not only draft scripts."""
    return provider != "template" and TEXT_PROVIDERS[provider].get("live", True)


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
                            "options": ["English option label", "..."]}],
             "scripts": {l: {f: "..." for f in FIELDS} | {"questions": {"q1": "spoken question with every option and its key"}}
                         | ({"doubts": "..."} if escalation else {}) for l in langs},
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
- questions: after the menu answer, the call can ask up to {MAX_QUESTIONS} follow-up keypad questions. Add only the ones
  this particular event genuinely needs from each person, and none that do not fit: e.g. a hackathon may need team
  status, food preference and T-shirt size; a workshop may need laptop or skill level; a plain seminar invitation, a
  clinic reminder or a payment reminder usually needs none ([]). Each question has {MIN_OPTIONS} to {MAX_OPTIONS} short
  options (keys 1, 2, 3... in order) and only_if_confirmed (true when it only matters for people who will attend).
  label and options are in English (they are shown on the dashboard). In every language's script, "questions" holds
  the spoken question for each id, reading out every option with its key in order, e.g. "What would you like for
  lunch? Press 1 for vegetarian, 2 for non-vegetarian."
- retry: suggest max_attempts (1 to 4) and gap_hours (1 to 48) suited to this campaign type.

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


def _ollama(messages: list[dict]) -> dict:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    resp = httpx.post(f"{url}/api/chat", timeout=300, json={
        "model": os.environ["OLLAMA_MODEL"], "messages": messages, "stream": False,
        "format": "json", "options": {"temperature": 0.3}})
    resp.raise_for_status()
    return _parse(resp.json()["message"]["content"])


def _sarvam(messages: list[dict]) -> dict:
    resp = httpx.post("https://api.sarvam.ai/v1/chat/completions", timeout=120,
                      headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                      json={"model": os.getenv("SARVAM_MODEL", "sarvam-m"), "messages": messages, "temperature": 0.3})
    resp.raise_for_status()
    return _parse(resp.json()["choices"][0]["message"]["content"])


def _chatgpt(messages: list[dict]) -> dict:
    return _parse(chatgpt.complete(messages))


def _why(exc: Exception) -> str:
    return exc.reason if isinstance(exc, chatgpt.ChatGPTError) else type(exc).__name__


def run_json(messages: list[dict], requested: str) -> tuple[dict, str, list[str]]:
    """One model call returning JSON: (reply, provider used, warnings). ChatGPT falls back to the
    other configured models; with none usable the reply is {} and the provider is "template"."""
    warnings: list[str] = []
    raw: dict = {}
    used = "template"
    run = {"chatgpt": _chatgpt, "ollama": _ollama, "sarvam": _sarvam}
    for p in ([requested] + (FALLBACK if requested == "chatgpt" else [])) if requested != "template" else []:
        label = TEXT_PROVIDERS[p]["label"]
        if not available(p):
            warnings.append(f"{label} is not {'connected' if p == 'chatgpt' else 'configured'}.")
            continue
        try:
            raw, used = run[p](messages), p
            break
        except Exception as exc:  # network, HTTP or JSON: never block the caller on it
            log.warning("call via %s failed: %s", p, type(exc).__name__)
            warnings.append(f"{label} failed ({_why(exc)}).")
    if requested != "template" and used != requested:
        warnings.append(f"Used {TEXT_PROVIDERS[used]['label'] if used != 'template' else 'the built-in templates'} instead.")
    return raw, used, warnings


def draft(e: dict, langs: list[str], provider: str, escalation: bool) -> tuple[dict, list[str]]:
    """Returns (draft, warnings). ChatGPT falls back to the other configured models, then the
    templates; any other provider falls back to the templates. Each step leaves a warning."""
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
                        "or configure Sarvam or Ollama to draft it.")
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
    return {"name": name.strip()[:80], "scripts": scripts, "questions": questions, "retry": retry, "dtmf": DTMF,
            "notes": str(raw.get("notes") or "")[:500], "provider": provider}, warnings


def _questions(raw) -> tuple[list[dict], dict[str, dict]]:
    """Untrusted model output -> at most MAX_QUESTIONS clean questions with ids q1..qN.
    Also returns {the model's id: question} so each language's spoken text can be matched up."""
    out, renamed = [], {}
    for q in raw if isinstance(raw, list) else []:
        if len(out) == MAX_QUESTIONS or not isinstance(q, dict):
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
