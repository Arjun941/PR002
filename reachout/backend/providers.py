"""Providers page API: every provider Reachout can use, grouped by job, with what is set up and where
data goes. Read-only except the connection check for Gemini Live."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from . import agents, audio, elevenlabs, gemini_live, llm

router = APIRouter(prefix="/api")

TEXT_SETUP = {"template": "", "chatgpt": "Connect on the New campaign page", "ollama": "OLLAMA_MODEL (and OLLAMA_URL)",
              "sarvam": "SARVAM_API_KEY"}
VOICE_SETUP = {"piper": "Not wired up yet", "sarvam": "Not wired up yet", "elevenlabs": "ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID"}


def _item(key, p, available, setup, **extra):
    return {"key": key, "label": p["label"], "region": p["region"], "sends": p["sends"], "available": available,
            "setup": "" if available else setup, **extra}


@router.get("/providers")
def providers():
    default_agent = agents.default()
    return {"groups": [
        {"key": "live", "title": "Live voice assistant",
         "help": "Answers callers who ask a question at the end of the call. The only part of a call that runs a live voice model, so it is the part that costs.",
         "items": [_item(k, v, agents.ready(k), v["setup"], default=k == default_agent and agents.ready(k),
                         model=gemini_live.model() if k == "gemini" else None, checkable=k == "gemini")
                   for k, v in agents.PROVIDERS.items()]},
        {"key": "voice", "title": "Call audio",
         "help": "Speaks the campaign script. Scripts are synthesised once per language, before the campaign starts.",
         "items": [_item(k, v, audio.ready(k), VOICE_SETUP.get(k, "")) for k, v in llm.VOICE_PROVIDERS.items()]},
        {"key": "text", "title": "Script drafting",
         "help": "Writes the scripts in every language with one call per campaign. A person reviews them before launch.",
         "items": [_item(k, v, llm.available(k), TEXT_SETUP.get(k, ""), live=llm.live(k))
                   for k, v in llm.TEXT_PROVIDERS.items()]},
    ]}


@router.post("/providers/gemini/check")
async def check_gemini():
    if not gemini_live.ready():
        raise HTTPException(400, "GEMINI_API_KEY is not set")
    return await gemini_live.check()
