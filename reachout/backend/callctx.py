"""Everything a live agent is told about one call: the campaign's system prompt (with this person's
details filled in) plus the campaign's facts, the written script for their language, the follow-up
questions and how to save answers. Shared by every provider; the engine adds only its own tool syntax.
"""
from __future__ import annotations

from . import endcall
from .dbcommon import ANSWERED, answers, missing_questions
from .store import LANGUAGES

PLACEHOLDERS = ("name", "language", "org", "title", "date", "time", "venue")
# The agent talks; the keypad menu belongs to the automated (IVR) call only.
VOICE_RULE = ("This is a spoken phone call. NEVER tell the person to press a key or a number, never read out option numbers, and "
              "never mention a keypad or menu. Ask everything in plain words, listen to their answer in their own words (any phrasing, "
              "in their language), and work out which option they mean. If it is not clear, ask one short question to clarify.")


def event_facts(e: dict) -> str:
    when = " at ".join(x for x in (e.get("date", ""), e.get("time", "")) if x)
    rows = (("Organisation", e.get("org")), ("About", e.get("title")), ("When", when), ("Where", e.get("venue")),
            ("Details", e.get("details")))
    return "\n".join(f"- {k}: {v}" for k, v in rows if v) or "- (no details given)"


def default_system_prompt(e: dict, questions: list[dict] | None = None) -> str:
    """The fallback when no model wrote one (and the fill-in for older campaigns)."""
    org = e.get("org") or "our organisation"
    qs = ("\n- Then ask the follow-up questions one at a time, in a natural way, and note each answer." if questions else "")
    return (
        f"You are a friendly, professional phone assistant calling on behalf of {org}. You are speaking with {{name}}, "
        f"in {{language}}. Your goal is to tell them about {e.get('title', 'this call')} and find out whether they will attend "
        "(or pay, or want to reschedule).\n"
        "How the call goes:\n"
        "- Greet {name} warmly, say who you are calling for, and give the key facts in one or two short sentences.\n"
        f"- Ask whether they can make it, and save their answer.{qs}\n"
        "- Ask if they have any other questions and answer them using only the facts you were given. If you do not "
        "know, say someone will follow up. Never invent dates, prices or promises.\n"
        "- Thank them and say goodbye.\n"
        "Speak like a person on the phone: short sentences, no lists, no markdown. Let them talk and stop when they "
        "interrupt. Keep the whole call under three minutes. If they ask you to stop calling, apologise and end the call."
    )


def fill(text: str, c: dict, r: dict) -> str:
    e = c.get("event") or {}
    vals = {"name": r.get("name", ""), "language": LANGUAGES.get(r.get("language"), r.get("language", "")),
            "org": e.get("org", ""), "title": e.get("title", ""), "date": e.get("date", ""), "time": e.get("time", ""),
            "venue": e.get("venue", "")}
    for k, v in vals.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def script_for(c: dict, r: dict) -> dict:
    scripts = c.get("scripts") or {}
    return scripts.get(r.get("language")) or next(iter(scripts.values()), {})


def follow_up(c: dict, r: dict) -> list[dict]:
    """The questions still to ask when this call is a call-back to someone who already gave their decision, else []."""
    return missing_questions(c.get("questions"), r)


def opening(c: dict, r: dict) -> str:
    """What the agent says first: the campaign's reviewed greeting and message, in the person's language. A call-back to someone
    who already answered only says who is calling again and that a few details are missing."""
    if follow_up(c, r):
        e = c.get("event") or {}
        return fill(f"Hello {{name}}, this is {e.get('org') or 'us'} calling back about {e.get('title') or 'our earlier call'}. "
                    "Our call was cut off before we got a few details, so I will ask just those now.", c, r)
    s = script_for(c, r)
    return fill(" ".join(x.strip() for x in (s.get("greeting", ""), s.get("message", "")) if x.strip()), c, r)


def instructions(c: dict, r: dict, ivr_done: str = "", on_end_tool: bool = True) -> str:
    """The full system instruction for this call. ivr_done: hybrid mode, the person has already been through the
    automated menu (and chose this answer) and is now asking a question."""
    e, s, qs = c.get("event") or {}, script_for(c, r), c.get("questions") or []
    base = fill((c.get("system_prompt") or "").strip() or default_system_prompt(e, qs), c, r)
    lang = LANGUAGES.get(r.get("language"), r.get("language", ""))
    parts = [
        base, "",
        "CALL CONTEXT",
        f"You are speaking with {r.get('name', 'the recipient')} (language: {lang}"
        + (f", group: {r['segment']}" if r.get("segment") else "") + ").",
        "Facts about this call (use only these):", event_facts(e),
        "", VOICE_RULE,
    ]
    if on_end_tool:
        parts += ["", endcall.INSTRUCTION]
    written = [(k, s.get(k)) for k in ("greeting", "message", "doubts", "goodbye") if s.get(k)]  # not "menu": that is keys
    if written:
        parts += ["", f"The approved script in {lang}. Say it in your own natural words, keeping the facts exactly:"]
        parts += [f"- {k}: {fill(v, c, r)}" for k, v in written]
    if ivr_done:
        parts += ["", f"The person has just finished an automated menu and chose: {ivr_done}. Their decision and follow-up "
                      "answers are already saved. They then started asking a question: their first words reach you "
                      "straight away, so answer them without a greeting. Answer briefly from the facts above; when they "
                      "have nothing more to ask, say a short goodbye."]
        return "\n".join(parts)
    todo = follow_up(c, r)
    if todo:  # a call-back: the decision is already saved, only some answers are missing
        said = answers(r)
        parts += ["", f"THIS IS A CALL-BACK. The person already told us their decision on an earlier call: {r.get('outcome')}. That is "
                      "saved: do not ask for it again and do not repeat the invitation. The earlier call was cut off before these questions "
                      "were answered, so ask only these, one at a time, in words, and record each answer. If they say they changed their "
                      "mind about the decision, record the new decision."]
        for q in todo:
            parts.append(f"- {q['id']}: {q['label']}. Choices: {' | '.join(q['options'])}.")
        parts += ["", "To save an answer with record_answer, option_number is the choice's position in that question's list of choices "
                      "(the first choice is 1). Count carefully and check it matches what the person actually said before saving:"]
        for q in todo:
            parts.append(f"  {q['id']}: " + ", ".join(f"{i} = {o}" for i, o in enumerate(q["options"], 1)))
        done = [q for q in qs if said.get(q["id"])]
        if done:
            parts += ["", "Already answered earlier (do not ask again): " + ", ".join(q["label"] for q in done) + "."]
        return "\n".join(parts)
    parts += ["", "Record the person's decision as soon as it is clear: confirmed (will attend or pay), declined, or "
                  "rescheduled (needs another time)."]
    if qs:
        parts += ["", "After that, ask these follow-up questions one at a time, in words, and record each answer. The "
                      "choices are what the person can answer; say them naturally (or ask open-ended and match what they say):"]
        for q in qs:
            only = " (only if they confirmed)" if q.get("only_if_confirmed", True) else ""
            parts.append(f"- {q['id']}: {q['label']}{only}. Choices: {' | '.join(q['options'])}.")
        parts += ["", "To save an answer with record_answer, option_number is the choice's position in that question's list of choices "
                      "(the first choice is 1). Count carefully and check it matches what the person actually said before saving:"]
        for q in qs:
            parts.append(f"  {q['id']}: " + ", ".join(f"{i} = {o}" for i, o in enumerate(q["options"], 1)))
    return "\n".join(parts)
