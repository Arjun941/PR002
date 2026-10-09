"""Lets a live voice agent end the call itself, but never in the middle of its own goodbye.

The agent (ElevenLabs or Gemini) is given an `end_call` tool. It calls it only when the person asks to end the call, or when the whole
conversation is finished (decision saved, questions asked, nothing more to say) and it has said goodbye. The tool just raises a flag
(`on_end`); `run` then waits for the audio already sent to finish playing and closes the conversation, which ends the call. If the agent
never ends it, the call lasts until the person hangs up or the usual time cap, as before.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Awaitable, Callable

log = logging.getLogger("reachout.endcall")
GOODBYE_MAX = 12.0  # never wait longer than this for the goodbye to finish
GRACE = 1.5  # the agent's last audio can still be arriving when it calls the tool: let it arrive before judging the line quiet
POLL = 0.25

INSTRUCTION = ("ENDING THE CALL: you have an end_call tool. Use it in only two cases: (1) the person asks to end the call, says they have to go, "
               "or says goodbye; (2) the whole conversation is finished: their decision is saved, every follow-up question has been asked "
               "and answered, and they have nothing more to ask. In both cases your goodbye is your last words: say it, then call end_call "
               "silently. Never say that you are ending, closing or hanging up the call, or that it has ended: no announcement, just the goodbye. "
               "Never end the call while a question is waiting for an answer, while they are still talking, or just because there was a pause.")


NOTICE_INSTRUCTION = ("ENDING THE CALL: you have an end_call tool and you MUST use it. This call only passes on a message. Say the message, "
                      "ask if they have any questions and answer them; as soon as they say they have none (or say no, thanks, ok, bye, "
                      "or stay quiet after your question), say one short goodbye and call end_call straight away. Also call it if they ask "
                      "to end the call. Never say that you are ending, closing or hanging up the call, or that it has ended: no announcement, "
                      "the goodbye is your last word and the call simply ends. Never leave the line open waiting: when the message is "
                      "delivered and nothing is left to answer, the call is finished.")


class AfterEnd:
    """Wraps the audio the agent sends to the caller. Once the agent has called end_call, only the tail of the goodbye it was already
    saying may still arrive; a new utterance after a pause is the agent reacting to the tool ("the call has been ended successfully"),
    which nobody wants to hear, so it is dropped. Whatever the model decides to say, the call just ends after the goodbye."""

    QUIET_GAP = 0.6  # seconds without audio after end_call: whatever follows is a new utterance

    def __init__(self, send: Callable[[bytes], Awaitable[None]], ended: asyncio.Event):
        self.send, self.ended = send, ended
        self.last = 0.0
        self.muted = False

    async def __call__(self, pcm: bytes) -> None:
        now = asyncio.get_running_loop().time()
        if self.ended.is_set() and (self.muted or now - self.last > self.QUIET_GAP):
            if not self.muted:
                log.info("dropping what the agent said after ending the call")
            self.muted = True
            return
        self.last = now
        await self.send(pcm)


async def run(engine: Awaitable[None], ended: asyncio.Event, drained: Callable[[], bool]) -> None:
    """Runs the conversation. When `ended` is set (the agent called end_call), waits until `drained()` says the audio already sent has
    finished playing (at most GOODBYE_MAX seconds), then stops the conversation. Errors from the engine are raised, as without this."""
    task = asyncio.ensure_future(engine)
    waiter = asyncio.ensure_future(ended.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
            return
        log.info("the agent ended the call")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GOODBYE_MAX
        await asyncio.wait({task}, timeout=GRACE)
        while loop.time() < deadline and not task.done() and not drained():
            await asyncio.sleep(POLL)
    finally:
        waiter.cancel()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
