"""Live assistants for key 4 (the only part of a call that is conversational), pluggable per campaign.

Every provider exposes `bridge(recv, send_audio, clear, variables, language, on_outcome)` (see
elevenlabs.bridge) and says where the caller's voice goes. Add a provider by adding a module with that
function and an entry in PROVIDERS. LIVE_AGENT in .env picks the default for new campaigns.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from . import elevenlabs, gemini_live

PROVIDERS = {
    "gemini": dict(label="Gemini Live", region="Google (global)", setup="GEMINI_API_KEY",
                   sends="Callers who press 4: their voice goes to Google (Gemini Live)"),
    "elevenlabs": dict(label="ElevenLabs agent", region="United States", setup="ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID",
                       sends="Callers who press 4: their voice goes to ElevenLabs, USA"),
}


def ready(key: str) -> bool:
    return {"gemini": gemini_live.ready, "elevenlabs": elevenlabs.agent_ready}.get(key, lambda: False)()


def bridge(key: str) -> Callable[..., Awaitable[None]]:
    return {"gemini": gemini_live.bridge, "elevenlabs": elevenlabs.bridge}[key]


def default() -> str:
    """LIVE_AGENT if set and usable, else the first provider that is configured, else ElevenLabs (the original)."""
    pick = os.getenv("LIVE_AGENT", "")
    if pick in PROVIDERS and ready(pick):
        return pick
    return next((k for k in PROVIDERS if ready(k)), "elevenlabs")


def any_ready() -> bool:
    return any(ready(k) for k in PROVIDERS)
