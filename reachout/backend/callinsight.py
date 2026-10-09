"""After a recorded call: what was asked and what the person answered, from the call audio.

The live agents (ElevenLabs, Gemini) only save the main outcome and the keypad questions through their
tools; anything else they ask (a name, a marital status, a preferred time...) was said and then lost.
This sends the call's stereo recording (our side on the left, the person on the right) to Gemini once
and keeps, on the call's history record:
  summary     two or three sentences
  qa          every question asked and the person's answer, in English (names kept as said)
  transcript  who said what, in order
  final       the person's final answer to the call's main question
One Gemini call per recorded call. It needs GEMINI_API_KEY; CALL_ANALYSIS=0 turns it off. The audio
goes to Google for this (the campaign pages say so). The audio-input request format is UNVERIFIED:
check the first result on the History page.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import store
from .dbcommon import answers as saved_answers, public_recipient
from .llm import _parse

log = logging.getLogger("reachout.callinsight")
FINALS = ("confirmed", "declined", "rescheduled", "unclear")


def enabled() -> bool:
    return os.getenv("CALL_ANALYSIS", "1") != "0" and bool(os.getenv("GEMINI_API_KEY"))


def _prompt(call: dict, c: dict | None) -> str:
    e = (c or {}).get("event") or {}
    qs = "\n".join(f'- {q["id"]}: {q["label"]} (options: {" | ".join(q["options"])})' for q in (c or {}).get("questions") or [])
    return f"""This is a recording of an automated phone call. LEFT channel: our side (an AI agent or recorded prompts)
calling for {e.get("org") or call.get("org") or "the organisation"}. RIGHT channel: the person who was called.
The call is about: {e.get("title") or call.get("campaign_name") or "an event"}{f" on {e['date']}" if e.get("date") else ""}.
{"Multiple-choice questions the call may have asked (fill `choices` for each of these ids, based on what the person said):" + chr(10) + qs if qs else ""}

Return JSON only, in exactly this shape:
{{"choices": {{"<question id>": "the exact text of the listed option the person's spoken answer matches, copied from the list; '' if they did not answer or none fits"}},
  "summary": "2-3 sentences: what the call was about and what the person said",
  "final": "confirmed | declined | rescheduled | unclear (the person's answer to the main question)",
  "qa": [{{"question": "a question or request our side made, in English", "answer": "what the person replied, in English; '' if they did not answer"}}],
  "transcript": [{{"speaker": "agent | person", "text": "what was said, in the language it was said"}}]}}

Rules: include in qa EVERY question our side asked and every detail the person gave (names, dates, ages, status,
numbers, preferences), even if it was not a keypad question. Use only what is actually said in the recording;
never guess. Keep names of people and places as said. If a channel is silent, say so in the summary."""


def _run(wav: bytes, prompt: str) -> dict:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=os.getenv("GEMINI_ANALYSIS_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.1-flash-lite",
        contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav"), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1))
    return _parse(resp.text or "")


def _clean(raw: dict) -> dict:
    text = lambda v, n: str(v or "").strip()[:n]
    qa = [{"question": text(x.get("question"), 300), "answer": text(x.get("answer"), 500)}
          for x in (raw.get("qa") if isinstance(raw.get("qa"), list) else [])[:40] if isinstance(x, dict) and x.get("question")]
    turns = [{"speaker": "person" if x.get("speaker") == "person" else "agent", "text": text(x.get("text"), 1000)}
             for x in (raw.get("transcript") if isinstance(raw.get("transcript"), list) else [])[:300]
             if isinstance(x, dict) and x.get("text")]
    final = raw.get("final") if raw.get("final") in FINALS else "unclear"
    choices = raw.get("choices") if isinstance(raw.get("choices"), dict) else {}
    return {"summary": text(raw.get("summary"), 1200), "qa": qa, "transcript": turns, "final_heard": final,
            "choices": {str(k): text(v, 80) for k, v in choices.items()}}


def _reconcile(call: dict, c: dict | None, choices: dict[str, str]) -> list[dict]:
    """A live agent saves an answer by counting options, and can count wrong ("non-vegetarian" saved as option 3).
    What the person actually said, heard in the recording, wins: fix the saved answer and say what was changed.
    Only live calls: on a keypad call the key the person pressed is the answer."""
    rid = call.get("recipient_id")
    if not c or not rid or call.get("mode") != "live":
        return []
    fixed = []
    for q in c.get("questions") or []:
        said = (choices.get(q["id"]) or "").strip().lower()
        match = next((i for i, o in enumerate(q["options"], 1) if o.strip().lower() == said), None) if said else None
        r = store.recipient(rid)
        have = saved_answers(r).get(q["id"]) if r else None
        if match and r and have != str(match):
            store.set_answer(rid, q["id"], str(match))
            was = q["options"][int(have) - 1] if have and have.isdigit() and 0 < int(have) <= len(q["options"]) else None
            fixed.append({"question": q["label"], "saved": was, "heard": q["options"][match - 1]})
            log.info("call %s: %s corrected to what was said", call["id"], q["id"])
    return fixed


async def analyse(call_id: str, stereo_wav: bytes) -> None:
    """Adds summary, Q&A and transcript to the call's history record. Never raises."""
    call = store.get_call(call_id)
    if not call:
        return
    if not enabled():
        call["analysis"] = {"status": "off", "note": "Set GEMINI_API_KEY to keep what was asked and answered"
                            if os.getenv("CALL_ANALYSIS", "1") != "0" else "Turned off (CALL_ANALYSIS=0)"}
        store.save_call(call)
        return
    c = store.campaign(call["campaign_id"]) if call.get("campaign_id") else None
    try:
        raw = await asyncio.to_thread(_run, stereo_wav, _prompt(call, c))
        found = _clean(raw)
        fixed = _reconcile(call, c, found.pop("choices"))
        r = store.recipient(call["recipient_id"]) if call.get("recipient_id") else None
        call = (store.get_call(call_id) or call) | found | {"analysis": {"status": "done", "by": "Gemini"}, "answers_corrected": fixed}
        if r:
            call["answers"] = public_recipient(r, (c or {}).get("questions") or [])["answers"]
        log.info("call %s: %d answers kept", call_id, len(call["qa"]))
    except Exception as exc:  # network, quota, format: the call itself is already saved
        log.warning("call %s: analysis failed (%s)", call_id, type(exc).__name__)
        call = (store.get_call(call_id) or call) | {"analysis": {"status": "failed", "note": type(exc).__name__}}
    store.save_call(call)
