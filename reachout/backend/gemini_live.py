"""Gemini Live as the call's question assistant: a drop-in alternative to the ElevenLabs agent.

Same contract as elevenlabs.bridge(): connect the caller to the agent until either side hangs up.
Audio is PCM16 8 kHz mono on the telephony side; Gemini takes 16 kHz and answers at 24 kHz, so we
resample both ways. Gemini's own voice-activity detection decides turns and tells us when the
caller barges in (`interrupted`), at which point the queued telephony audio is cleared. The agent
can save the outcome with a `record_outcome` tool, as the ElevenLabs agent does.

Needs GEMINI_API_KEY. GEMINI_LIVE_MODEL picks the model (default gemini-3.8-live: ~1 s to first
audio in our tests; the 3.1 preview model was closing sessions with a 1011 error, the 2.5 native-audio
model took 7-14 s). The caller's words are never logged at INFO.
"""
from __future__ import annotations

import array
import asyncio
import base64
import logging
import os
import time
from typing import Awaitable, Callable

try:  # removed in Python 3.13; the pure-Python fallback below is slower but fine for 20 ms frames
    import audioop
except ImportError:  # pragma: no cover
    audioop = None

log = logging.getLogger("reachout.gemini_live")

MODEL_DEFAULT = "gemini-3.8-live"
OUTCOMES = ("confirmed", "declined", "rescheduled")


def model() -> str:
    return os.getenv("GEMINI_LIVE_MODEL") or MODEL_DEFAULT


def ready() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))



class Resampler:
    """Streaming 16-bit mono resampler between whole-number-friendly rates (8k/16k/24k)."""

    def __init__(self, src: int, dst: int):
        self.src, self.dst, self.state = src, dst, None
        self._tail = 0

    def __call__(self, pcm: bytes) -> bytes:
        if audioop:
            out, self.state = audioop.ratecv(pcm, 2, 1, self.src, self.dst, self.state)
            return out
        a = array.array("h", pcm[:len(pcm) - len(pcm) % 2])
        if self.dst > self.src:  # upsample by linear interpolation (8k -> 16k)
            k, out = self.dst // self.src, array.array("h")
            for i, s in enumerate(a):
                nxt = a[i + 1] if i + 1 < len(a) else s
                out.extend(int(s + (nxt - s) * j / k) for j in range(k))
            return out.tobytes()
        k = self.src // self.dst  # downsample by averaging (24k -> 8k)
        return array.array("h", (sum(a[i:i + k]) // k for i in range(0, len(a) - k + 1, k))).tobytes()


def system_prompt(variables: dict[str, str], language: str) -> str:
    when = variables.get("when", "")
    answer = variables.get("answer", "")
    gave = f"They answered the automated menu with: {answer}. " if answer else ""
    facts = "\n".join(f"- {k}: {v}" for k, v in (
        ("Organisation", variables.get("org")), ("About", variables.get("title")), ("When", when),
        ("Where", variables.get("venue")), ("Details", variables.get("details"))) if v)
    return (
        "You are a phone assistant for an organisation that has just called someone with an automated "
        f"message. {gave}At the end of the call they were asked if they have any other questions and started "
        "speaking, so you answer them. Be warm, brief and natural, like a helpful receptionist: "
        "one or two short sentences at a time, no lists, no markdown. Let the caller talk.\n"
        f"Reply in {variables.get('language') or language}, and switch if the caller does.\n"
        f"What the call was about:\n{facts or '- (no details given)'}\n"
        "Answer questions using only these facts; if you do not know, say someone will follow up. "
        "When the caller clearly confirms they will attend or pay, declines, or wants to reschedule, call "
        "the record_outcome tool with confirmed, declined or rescheduled, then say a short goodbye. "
        "Their first words reach you straight away: answer them without a greeting. If you hear nothing, ask how you can help."
    )


async def bridge(recv: Callable[[], Awaitable[dict | None]], send_audio: Callable[[bytes], Awaitable[None]],
                 clear: Callable[[], Awaitable[None]], variables: dict[str, str], language: str,
                 on_outcome: Callable[[str], bool], preroll: bytes = b"", *, instructions: str = "",
                 opening: str = "", questions: list[dict] | None = None,
                 on_answer: Callable[[str, str], bool] | None = None, on_end: Callable[[], None] | None = None) -> None:
    """recv() yields Exotel events (None when the call ends); audio is PCM16 8 kHz both ways.
    preroll: what the caller already said before we were connected; it is sent first.
    Outbound calls (the web phone) pass the campaign's `instructions`, an `opening` to start with, and the
    follow-up `questions` the agent can save answers to with record_answer."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    declarations = [types.FunctionDeclaration(
        name="record_outcome", description="Save the person's decision about the invitation or reminder.",
        parameters=types.Schema(type="OBJECT", properties={
            "outcome": types.Schema(type="STRING", enum=list(OUTCOMES))}, required=["outcome"]))]
    if questions and on_answer:
        declarations.append(types.FunctionDeclaration(
            name="record_answer", description="Save the person's answer to one of the follow-up questions.",
            parameters=types.Schema(type="OBJECT", properties={
                "question_id": types.Schema(type="STRING", enum=[q["id"] for q in questions]),
                "option_number": types.Schema(type="INTEGER", description="The 1-based number of the chosen option")},
                required=["question_id", "option_number"])))
    if on_end:
        declarations.append(types.FunctionDeclaration(
            name="end_call", description="Hang up the call. Only when the person asks to end it, or the whole conversation is finished "
                                         "and you have already said goodbye.", parameters=types.Schema(type="OBJECT", properties={})))
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=instructions or system_prompt(variables, language),
        tools=[types.Tool(function_declarations=declarations)],
        # Phone speakers and room noise leak into the mic: make the "caller started talking" detector less
        # twitchy so the agent is not cut off mid-sentence, and wait a little longer before deciding they are done.
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
            prefix_padding_ms=40, silence_duration_ms=int(os.getenv("GEMINI_SILENCE_MS", "600")))),
    )
    up, down = Resampler(8000, 16000), Resampler(24000, 8000)
    async with client.aio.live.connect(model=model(), config=config) as session:
        if preroll:  # the question they started asking; Gemini ends the turn when the live audio goes quiet
            for i in range(0, len(preroll), 640):
                await session.send_realtime_input(audio=types.Blob(data=up(preroll[i:i + 640]), mime_type="audio/pcm;rate=16000"))
        else:
            kick = ("The call has just connected and the person has answered. Begin now. Open with this, in your own natural "
                    f"words and their language: {opening}" if opening else "The caller just joined. Greet them briefly.")
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=kick)]), turn_complete=True)
        log.info("gemini live assistant connected (%s)", model())

        async def caller_to_agent() -> None:
            while (msg := await recv()) is not None:
                if msg.get("event") == "stop":
                    return
                if msg.get("event") == "media":
                    pcm = up(base64.b64decode(msg["media"]["payload"]))
                    await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

        async def agent_to_caller() -> None:
            while True:  # receive() ends after every turn; keep listening for the next one
                async for m in session.receive():
                    if m.tool_call:
                        responses = []
                        for fc in m.tool_call.function_calls:
                            a = fc.args or {}
                            if fc.name == "end_call" and on_end:
                                on_end()  # no tool response: answering it would make the model say "the call has ended"
                                continue
                            elif fc.name == "record_answer" and on_answer:
                                ok = on_answer(str(a.get("question_id", "")), str(a.get("option_number", "")))
                            else:
                                ok = fc.name == "record_outcome" and on_outcome(str(a.get("outcome", "")))
                            responses.append(types.FunctionResponse(
                                id=fc.id, name=fc.name, response={"result": "saved" if ok else "unknown tool or outcome"}))
                        if responses:
                            await session.send_tool_response(function_responses=responses)
                    sc = m.server_content
                    if not sc:
                        continue
                    if sc.interrupted:
                        await clear()
                    if sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await send_audio(down(part.inline_data.data))

        tasks = [asyncio.create_task(caller_to_agent()), asyncio.create_task(agent_to_caller())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception():
                log.warning("gemini live bridge ended with %s", type(t.exception()).__name__)


async def check() -> dict:
    """Open a session, ask for a one-word reply, report the time to first audio. For the Providers page."""
    from google import genai
    from google.genai import types

    t0 = time.monotonic()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    try:
        async with client.aio.live.connect(model=model(), config=types.LiveConnectConfig(response_modalities=["AUDIO"])) as s:
            connected = time.monotonic() - t0
            await s.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text="Say hello.")]), turn_complete=True)
            t1 = time.monotonic()

            async def first_audio() -> None:
                async for m in s.receive():
                    if m.server_content and m.server_content.model_turn:
                        return

            await asyncio.wait_for(first_audio(), 20)
            return {"ok": True, "model": model(), "connect_ms": round(connected * 1000),
                    "first_audio_ms": round((time.monotonic() - t1) * 1000)}
    except asyncio.TimeoutError:
        return {"ok": False, "model": model(), "error": "Connected, but no audio came back within 20 s"}
    except Exception as exc:
        return {"ok": False, "model": model(), "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
