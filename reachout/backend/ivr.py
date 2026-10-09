"""Hybrid mode: the keypad-first IVR call, then the live agent for anyone with a question.

The call plays pre-synthesised audio (audio.py): greeting + name + message + menu, then waits for a key.
1/2/3 save the outcome; any other key replays the menu. No key after the menu (asked twice) plays the
voicemail message and ends, and the call counts as voicemail so the retry policy applies.

After a 1/2/3 answer the campaign's follow-up questions are asked in order (skipping those meant only for
people who confirm when they did not). Each takes one key; a wrong key or silence repeats it once, then it
is skipped. The call ends by asking "any other questions?" and listening for a few seconds. A caller who
starts speaking (a loudness check, no speech recognition) is handed to the campaign's live agent together
with what they have said so far; silence, a key or hanging up ends the call with the goodbye.

`line` is anything with the PhoneLine interface of webphone.py: recv(timeout), play(pcm), send(pcm),
clear(), finish(), until, _now(), call_sid. Audio is PCM16 8 kHz mono. recv() yields Exotel-shaped events
({"event": "media" | "dtmf" | "stop" | "timeout"}) so the live engines can read it directly.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
from array import array
from collections import deque

from . import callctx, catalog, endcall, store
from .store import LANGUAGES

log = logging.getLogger("reachout.ivr")

DIGITS = {"1": "confirmed", "2": "declined", "3": "rescheduled"}
FRAME_BYTES = 320  # 20 ms of 16-bit mono at 8 kHz
MENU_WAIT = float(os.getenv("MENU_WAIT_SECONDS", "8"))  # silence after the menu before asking again
DOUBT_WAIT = float(os.getenv("DOUBT_WAIT_SECONDS", "6"))  # how long to listen for a question at the end
VAD_RMS = int(os.getenv("VAD_RMS", "700"))  # a 20 ms frame louder than this (16-bit RMS) counts as speech
VAD_MIN_MS = 400  # this much speech within one second means the caller is asking something


class Hangup(Exception):
    """The person put the phone down."""


def record_outcome(call_sid: str | None, outcome: str | None, channel: str) -> bool:
    """Saves an answered call's result (keypad digit, or the agent's record_outcome tool).
    outcome=None only marks the channel, e.g. when the caller is handed to the agent."""
    if not call_sid or outcome not in (None, *DIGITS.values()):
        return False
    return store.mark_outcome(call_sid, outcome, channel)


def record_dtmf(call_sid: str | None, digit: str) -> bool:
    return bool(DIGITS.get(digit)) and record_outcome(call_sid, DIGITS[digit], "keypad")


def record_answer(call_sid: str | None, question: dict, digit: str) -> bool:
    """A follow-up question's answer (a key that is one of its options) for the call's recipient."""
    if not call_sid or not digit.isdigit() or not 0 < int(digit) <= len(question["options"]):
        return False
    return store.add_answer(call_sid, question["id"], digit)


async def _escalate(line, c: dict, r: dict, outcome: str, question: bytes) -> bool:
    """The caller asked a question at the end: the campaign's live agent answers.
    False if it could not be reached."""
    provider = c.get("provider") or c.get("agent_provider") or "elevenlabs"
    instructions = callctx.instructions(c, r, ivr_done=outcome)
    ended = asyncio.Event()
    record_outcome(line.call_sid, None, "agent")
    log.info("call %s: handing over to the %s agent", line.call_sid, provider)
    try:
        await endcall.run(catalog.engine(provider)(
            line.recv, endcall.AfterEnd(line.send, ended), line.clear, {"language": LANGUAGES.get(r["language"], r["language"]), "answer": outcome},
            r["language"], lambda o: record_outcome(line.call_sid, o, "agent"), preroll=question, instructions=instructions,
            on_end=ended.set), ended, lambda: line._now() >= line.until + 0.8)
    except Exception as exc:  # connect failed: the caller is still on the IVR call
        log.warning("call %s: live agent unavailable (%s)", line.call_sid, type(exc).__name__)
        record_outcome(line.call_sid, None, "keypad")
        return False
    return True


async def _ask(line, q: dict, prompt: bytes) -> None:
    """One follow-up question: a key that is one of its options is saved; otherwise ask once more."""
    for _ in range(2):
        await line.play(prompt)
        deadline = line.until + MENU_WAIT
        while (msg := await line.recv(timeout=max(0.0, deadline - line._now()))) is not None:
            event = msg.get("event")
            if event == "stop":
                raise Hangup()
            if event == "timeout":
                break
            if event == "dtmf":
                digit = str((msg.get("dtmf") or {}).get("digit", ""))
                await line.clear()
                if record_answer(line.call_sid, q, digit):
                    log.info("call %s: %s answered", line.call_sid, q["id"])
                    return
                break
        else:
            raise Hangup()


async def _follow_ups(line, c: dict, outcome: str, clips: dict) -> None:
    rec = store.recipient_by_call(line.call_sid) if line.call_sid else None
    done = store.answers(rec) if rec else {}  # a call-back: skip what was answered before the earlier call was cut off
    for q in c.get("questions") or []:
        if done.get(q["id"]):
            continue
        if (outcome == "confirmed" or not q.get("only_if_confirmed", True)) and clips["questions"].get(q["id"]):
            await _ask(line, q, clips["questions"][q["id"]])


def _loud(frame: bytes) -> bool:
    a = array("h")
    a.frombytes(frame)
    return bool(a) and math.sqrt(sum(x * x for x in a) / len(a)) > VAD_RMS


async def _hear_question(line) -> bytes | None:
    """After the closing prompt: the caller's audio so far once they start speaking; None on
    silence, a key press or hang-up. Audio from before the prompt has finished is ignored."""
    start = line.until
    deadline = start + DOUBT_WAIT
    last_second: deque[tuple[bytes, bool]] = deque(maxlen=1000 // 20)
    rest = b""
    while (msg := await line.recv(timeout=max(0.0, deadline - line._now()))) is not None:
        event = msg.get("event")
        if event in ("stop", "timeout", "dtmf"):
            return None
        if event != "media" or line._now() < start:
            continue
        rest += base64.b64decode(msg["media"]["payload"])
        while len(rest) >= FRAME_BYTES:
            frame, rest = rest[:FRAME_BYTES], rest[FRAME_BYTES:]
            loud = _loud(frame)
            last_second.append((frame, loud))
            if loud:  # someone is talking: do not cut them off at the deadline
                deadline = max(deadline, line._now() + 1.0)
            if sum(v for _, v in last_second) * 20 >= VAD_MIN_MS:
                return b"".join(f for f, _ in last_second)
    return None


async def _closing(line, c: dict, r: dict, outcome: str, clips: dict) -> None:
    """End of an answered call: offer the agent for any questions, otherwise say goodbye."""
    provider = c.get("provider") or c.get("agent_provider") or "elevenlabs"
    if c["escalation"] and clips.get("doubts") and catalog.ready(provider, "live"):
        await line.play(clips["doubts"])
        question = await _hear_question(line)
        if question:
            log.info("call %s: caller has a question", line.call_sid)
            if await _escalate(line, c, r, outcome, question):
                return
    await line.play(clips["goodbye"])
    await line.finish()


async def campaign_call(line, c: dict, r: dict, clips: dict[str, bytes]) -> None:
    """The whole hybrid call. Raises Hangup if the person hangs up mid-prompt."""
    await line.play(clips["intro"] + clips["menu"])
    asked = 1
    while True:
        deadline = line.until + MENU_WAIT
        msg = await line.recv(timeout=max(0.0, deadline - line._now()))
        if msg is None or msg.get("event") == "stop":
            return
        event = msg.get("event")
        if event == "timeout":
            if asked < 2:  # ask once more before treating it as voicemail
                asked += 1
                await line.play(clips["menu"])
                continue
            await line.play(clips["voicemail"])
            await line.finish()
            return
        if event != "dtmf":
            continue  # caller audio is not used at the keypad menu
        digit = str((msg.get("dtmf") or {}).get("digit", ""))
        log.info("call %s: dtmf %s", line.call_sid, digit)
        await line.clear()  # a key press interrupts whatever is playing
        if record_dtmf(line.call_sid, digit):
            await _follow_ups(line, c, DIGITS[digit], clips)
            await _closing(line, c, r, DIGITS[digit], clips)
            return
        await line.play(clips["menu"])
