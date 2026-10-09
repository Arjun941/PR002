"""The providers Reachout can use, and what each can do. Exactly two for now: ElevenLabs and Gemini.

A campaign picks ONE provider and it does everything that provider is able to:
  draft  - writes the scripts and the agent's system prompt (one model call per campaign)
  voice  - pre-synthesised IVR audio for hybrid campaigns (ElevenLabs only; see audio.py)
  live   - the conversation itself: the whole call in live mode, the closing questions in hybrid mode
Providers that cannot draft text (ElevenLabs has no text model) borrow the first one that can, and the
UI says so. `setup` names the .env variables each capability needs.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from . import settings

ORDER = ("elevenlabs", "gemini")

PROVIDERS = {
    "elevenlabs": dict(
        label="ElevenLabs", region="United States",
        sends="Script text goes to ElevenLabs, USA. In a live conversation the caller's voice does too.",
        caps={"draft": None, "voice": ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"),
              "live": ("ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID")}),
    "gemini": dict(
        label="Gemini", region="Google (global)",
        sends="Event details go to Google. In a live conversation the caller's voice does too.",
        caps={"draft": ("GEMINI_API_KEY",), "voice": None, "live": ("GEMINI_API_KEY",)}),
}
CAP_LABELS = {"draft": "Writes scripts and the agent prompt", "voice": "Pre-synthesised IVR audio (hybrid mode)",
              "live": "Live conversation"}


def label(key: str) -> str:
    return PROVIDERS[key]["label"] if key in PROVIDERS else "ChatGPT (connected plan)" if key == "chatgpt" else key


def supports(key: str, cap: str) -> bool:
    return key in PROVIDERS and PROVIDERS[key]["caps"].get(cap) is not None


def missing(key: str, cap: str) -> list[str]:
    need = PROVIDERS[key]["caps"].get(cap) or ()
    return [v for v in need if not os.getenv(v)]


def ready(key: str, cap: str) -> bool:
    return supports(key, cap) and not missing(key, cap)


def available(key: str) -> bool:
    return any(ready(key, cap) for cap in PROVIDERS[key]["caps"])


def default() -> str:
    """The saved default if it is usable, else the first provider that can hold a live conversation."""
    pick = settings.load().get("default_provider", "")
    if pick in PROVIDERS and available(pick):
        return pick
    return next((k for k in ORDER if ready(k, "live")), next((k for k in ORDER if available(k)), ORDER[0]))


def drafter(key: str) -> str | None:
    """The provider that writes this campaign's text: the chosen one if it can, else the first that can."""
    if ready(key, "draft"):
        return key
    return next((k for k in ORDER if ready(k, "draft")), None)


def engine(key: str) -> Callable[..., Awaitable[None]]:
    """The conversation bridge for a provider (same signature as elevenlabs.bridge)."""
    from . import elevenlabs, gemini_live
    return {"elevenlabs": elevenlabs.bridge, "gemini": gemini_live.bridge}[key]
