"""The campaign agent: a chat that can edit a campaign, in the builder (before it exists) and on the campaign
page (after). Every turn it is handed the campaign's full current state plus a summary of the older chat and
the recent turns, so it always knows everything about the campaign, including what it changed earlier.

It never edits directly. It returns {reply, edits, actions}; this module checks and clamps them (model output is
untrusted), and the caller applies them: the browser applies builder edits to its form, the server applies
campaign edits through the same validation as the Edit page. The chat of a campaign built with the agent is saved
on the campaign, so the conversation continues on the campaign page.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from . import catalog, llm
from .store import LANGUAGES

log = logging.getLogger("reachout.campaignagent")

WINDOW = 24         # most recent turns given to the model verbatim
SUMMARISE_AT = 60   # saved turns before the oldest are folded into the summary
SUMMARISE_N = 30
SCRIPT_FIELDS = ("greeting", "message", "menu", "voicemail", "goodbye", "doubts")
EVENT_FIELDS = {"org": 80, "title": 120, "date": 40, "time": 40, "venue": 120, "details": 600, "kind": 40}
ACTIONS = {"builder": ("redraft",), "campaign": ("pause", "resynthesize")}

SHAPE = """{
  "reply": "what you say to the user: short, plain, in the language they are writing in",
  "edits": {   // include ONLY what you are changing; omit "edits" or leave it {} when changing nothing
    "name": "campaign name",
    "provider": "elevenlabs" | "gemini",
    "mode": "live" | "hybrid",
    "system_prompt": "the full new system prompt (it replaces the old one)",
    "retry": {"max_attempts": 1-4, "gap_hours": 1-48},
    "event": {"org": "", "kind": "", "title": "", "date": "", "time": "", "venue": "", "details": ""},
    "scripts": {"<language code>": {"greeting": "", "message": "", "menu": "", "voicemail": "", "goodbye": "", "doubts": "",
                                    "questions": {"q1": "spoken question"}}}
    BUILDER_ONLY
  },
  "actions": []   // ACTIONS
}"""


def _rules(scope: str) -> str:
    today = f"{date.today():%A, %d %B %Y}"
    if scope == "builder":
        where = ("You are inside the campaign BUILDER: the campaign does not exist yet. The user is filling in the form "
                 "(event, languages, contacts, provider and mode, scripts and the agent system prompt) and you can change "
                 "any of it for them. Nothing is called until they review and launch it themselves.")
        builder_only = (',\n    "languages": ["en", "hi"]   // replaces the language list; scripts for new languages need action "redraft"'
                        ',\n    "questions": [{"id": "q1", "label": "short English label", "options": ["A", "B"], "only_if_confirmed": true}]')
        where += (" If the state's scripts and system_prompt are null there is no draft yet: you cannot edit them, so use the "
                  "action \"redraft\" (or ask the user to open the Scripts step) instead.")
        acts = '"redraft" writes every script, the questions and the system prompt again from the event details ' \
               '(use it after changing the event, languages or mode when the existing scripts no longer fit, and say that you did)'
    else:
        where = ("You are on the page of an existing CAMPAIGN. Its contacts and languages are fixed, and so are its existing follow-up "
                 "questions (new ones are added on the Edit page); say so if asked to change them. You can edit its name, provider, mode, retry policy, event details, "
                 "system prompt and scripts, but only while it is paused or finished.")
        builder_only = ""
        acts = ('"pause" pauses a campaign that is running so it can be edited (only when the user asked for a change '
                'that needs it); "resynthesize" makes the IVR audio again for a hybrid campaign')
    return f"""You are the campaign agent of Reachout, a tool for automated multilingual phone-call campaigns run by Indian
schools, clinics and event organisers. Today is {today}. {where}

How a campaign works: calls ring a phone page one person at a time. Provider is who talks live (elevenlabs or gemini).
Mode "live": the agent takes over the whole call. Mode "hybrid": a pre-recorded keypad IVR (greeting with the name, message,
menu 1=confirm 2=decline 3=reschedule, follow-up questions), then "any other questions?" and the agent takes over only
if the person speaks. Scripts are the approved text per language (greeting must contain {{name}} exactly once; no other
placeholders). The system prompt is the live agent's instructions; it may use {{name}} and {{language}} only.

Your job: understand what the user wants, make the change yourself, and say briefly what you changed. Rules:
- You always have the campaign's current state below: use it, never ask for what it already says, and keep every part
  consistent. If the user changes a fact (date, time, venue, amount), update the event AND every script line and the
  system prompt that mention it, in every language, written natively in that language's script.
- Never invent facts (dates, venues, prices, names, phone numbers). If you need one, ask.
- When a request is ambiguous or risky, ask one short question instead of guessing. Answer questions about the
  campaign (results, scripts, cost, how it works) from the state without changing anything.
- Do not repeat unchanged text in "edits"; send only the fields you change. For scripts send only the fields you change.
- You cannot add or edit contacts, launch, delete or resume campaigns, or change phone settings: say so and point to the
  right place in the UI.
- Every change you mention in "reply" MUST be included in "edits" (or "actions"): never say you changed something you did not send.
- Do not claim a result you cannot know. Costs and totals come only from the state.
- You can see what you changed earlier in this chat ("[applied: ...]" notes); the state reflects it.

Reply with JSON only, exactly this shape:
{SHAPE.replace("BUILDER_ONLY", builder_only).replace("ACTIONS", acts)}"""


def build_messages(scope: str, state: dict, summary: str, history: list[dict]) -> list[dict]:
    system = _rules(scope) + "\n\nCURRENT CAMPAIGN STATE (always up to date):\n" + json.dumps(state, ensure_ascii=False)
    if summary:
        system += "\n\nSUMMARY OF THE EARLIER CONVERSATION:\n" + summary
    turns = []
    for t in history[-WINDOW:]:
        text = t["content"]
        if t["role"] == "assistant" and t.get("applied"):
            text += f"\n[applied: {', '.join(t['applied'])}]"
        turns.append({"role": t["role"], "content": text})
    return [{"role": "system", "content": system}] + turns


def _s(v, n: int) -> str:
    return str(v).strip()[:n] if isinstance(v, (str, int, float)) else ""


def clean(raw: dict, scope: str, languages: list[str]) -> tuple[str, dict, list[str]]:
    """(reply, edits, actions) with everything checked: unknown keys dropped, strings clamped, enums enforced."""
    reply = _s(raw.get("reply"), 1500)
    src = raw.get("edits") if isinstance(raw.get("edits"), dict) else {}
    edits: dict = {}
    if _s(src.get("name"), 80):
        edits["name"] = _s(src["name"], 80)
    if src.get("provider") in catalog.PROVIDERS:
        edits["provider"] = src["provider"]
    if src.get("mode") in ("live", "hybrid"):
        edits["mode"] = src["mode"]
    if _s(src.get("system_prompt"), 6000):
        edits["system_prompt"] = _s(src["system_prompt"], 6000)
    r = src.get("retry")
    if isinstance(r, dict):
        try:
            edits["retry"] = {"max_attempts": max(1, min(4, int(r["max_attempts"]))), "gap_hours": max(1, min(48, int(r["gap_hours"])))}
        except (KeyError, TypeError, ValueError):
            pass
    ev = src.get("event")
    if isinstance(ev, dict):
        got = {k: _s(ev[k], n) for k, n in EVENT_FIELDS.items() if k in ev and (k not in ("title", "kind") or _s(ev[k], n))}
        if got:
            edits["event"] = got
    sc = src.get("scripts")
    if isinstance(sc, dict):
        out = {}
        for lang, fields in sc.items():
            if lang not in LANGUAGES or not isinstance(fields, dict) or (languages and lang not in languages):
                continue
            part = {f: _s(fields[f], 800) for f in SCRIPT_FIELDS if _s(fields.get(f), 800)}
            qs = fields.get("questions")
            if isinstance(qs, dict):
                part["questions"] = {str(k)[:4]: _s(v, 400) for k, v in qs.items() if _s(v, 400)}
            if part:
                out[lang] = part
        if out:
            edits["scripts"] = out
    if scope == "builder":
        langs = src.get("languages")
        if isinstance(langs, list):
            ok = [l for l in dict.fromkeys(langs) if l in LANGUAGES]
            if ok:
                edits["languages"] = ok
        qs = src.get("questions")
        if isinstance(qs, list):
            clean_q = []
            for i, q in enumerate(qs[:4], 1):
                if isinstance(q, dict) and _s(q.get("label"), 60) and isinstance(q.get("options"), list):
                    opts = [_s(o, 40) for o in q["options"] if _s(o, 40)][:6]
                    if len(opts) >= 2:
                        clean_q.append({"id": f"q{i}", "label": _s(q["label"], 60), "options": opts,
                                        "only_if_confirmed": bool(q.get("only_if_confirmed", True))})
            if clean_q or qs == []:
                edits["questions"] = clean_q
    acts = [a for a in (raw.get("actions") or []) if a in ACTIONS[scope]] if isinstance(raw.get("actions"), list) else []
    return reply, edits, list(dict.fromkeys(acts))


def turn(scope: str, state: dict, summary: str, history: list[dict]) -> tuple[str, dict, list[str], list[str]]:
    """One model call. Returns (reply, edits, actions, warnings); raises RuntimeError if no model is available."""
    raw, used, warnings = llm.run_json(build_messages(scope, state, summary, history), catalog.default())
    if used == "template":
        raise RuntimeError("No model is available for the campaign agent: set GEMINI_API_KEY in .env. " + " ".join(warnings))
    reply, edits, actions = clean(raw, scope, state.get("languages") or [])
    if not reply:
        reply = "Done." if edits or actions else "I did not catch that: could you say it another way?"
    return reply, edits, actions, warnings


def fold(summary: str, turns: list[dict]) -> str:
    """The oldest turns folded into the running summary (one model call). On failure the old summary stays."""
    text = "\n".join(f"{t['role']}: {t['content']}" + (f" [applied: {', '.join(t['applied'])}]" if t.get("applied") else "")
                     for t in turns)
    msgs = [{"role": "system", "content": "You keep the memory of a chat between a user and the Reachout campaign agent. "
             "Merge the existing summary and the new turns into one summary under 250 words: the user's goals, decisions, "
             "preferences, every change made to the campaign and why, and anything still open. Reply with JSON "
             '{"summary": "..."} only.'},
            {"role": "user", "content": f"Existing summary:\n{summary or '(none)'}\n\nNew turns:\n{text}"}]
    try:
        raw, used, _ = llm.run_json(msgs, catalog.default())
        got = _s(raw.get("summary"), 2500) if used != "template" else ""
        return got or summary
    except Exception:
        log.exception("could not summarise the campaign chat")
        return summary
