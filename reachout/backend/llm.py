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
KIND_LABEL = {"seminar": "seminar or event invitation", "clinic": "clinic appointment reminder",
              "school": "school notice to parents", "payment": "payment reminder"}
DTMF = [  # fixed by design; the model only words the prompt for it
    {"digit": "1", "action": "confirm"}, {"digit": "2", "action": "decline"},
    {"digit": "3", "action": "reschedule"}, {"digit": "4", "action": "assistant"},
]

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
    menu += ", or 4 to speak to our assistant." if escalation else "."
    return {
        "greeting": f"Hello {{name}}, this is a message from {org}.",
        "message": message,
        "menu": menu,
        "voicemail": f"Sorry we missed you. This is {org}, about {title} on {when}. We will call again.",
        "goodbye": "Thank you. Goodbye.",
    }


def _prompt(e: dict, langs: list[str], escalation: bool) -> list[dict]:
    menu = "1 = confirm / will attend, 2 = decline / cannot attend, 3 = reschedule / need more time"
    if escalation:
        menu += ", 4 = speak to an assistant"
    shape = {"name": "short campaign name",
             "scripts": {l: {f: "..." for f in FIELDS} for l in langs},
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
- goodbye: one short sentence.
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
    if placeholders:
        warnings.append(f"{', '.join(placeholders)}: English placeholder text. Translate it before launch, "
                        "or configure Sarvam or Ollama to draft it.")
    for l, s in scripts.items():
        if "{name}" not in s["greeting"]:
            warnings.append(f"{LANGUAGES[l]} greeting does not use {{name}}.")
        if any(m != "{name}" for f in FIELDS for m in re.findall(r"\{[^}]*\}", s[f])):
            warnings.append(f"{LANGUAGES[l]} script has a placeholder other than {{name}}; it will be read out literally.")

    r = raw.get("retry") if isinstance(raw.get("retry"), dict) else {}
    retry = {"max_attempts": _clamp(r.get("max_attempts"), 1, 4, 2 if e["kind"] == "payment" else 3),
             "gap_hours": _clamp(r.get("gap_hours"), 1, 48, 24 if e["kind"] == "payment" else 4)}
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw["name"].strip() else e["title"]
    return {"name": name.strip()[:80], "scripts": scripts, "retry": retry, "dtmf": DTMF,
            "notes": str(raw.get("notes") or "")[:500], "provider": provider}, warnings


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default
