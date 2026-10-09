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
               "and answered, and they have nothing more to ask. In both cases say a short goodbye first, then call end_call. "
               "Never end the call while a question is waiting for an answer, while they are still talking, or just because there was a pause.")


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
