"""Providers page API: the two providers Reachout can use (ElevenLabs, Gemini), what each can do, what is
set up and where data goes. A saved default pre-selects the provider for new campaigns; each campaign
still picks its own. Connection checks make one small real request."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import catalog, elevenlabs, gemini_live, settings, webphone

router = APIRouter(prefix="/api")


@router.get("/providers")
def providers():
    default = catalog.default()
    items = []
    for key in catalog.ORDER:
        p = catalog.PROVIDERS[key]
        items.append({
            "key": key, "label": p["label"], "region": p["region"], "sends": p["sends"],
            "default": key == default, "available": catalog.available(key),
            "model": gemini_live.model() if key == "gemini" else None,
            "caps": [{"key": cap, "label": catalog.CAP_LABELS[cap], "supported": catalog.supports(key, cap),
                      "ready": catalog.ready(key, cap), "missing": catalog.missing(key, cap)} for cap in p["caps"]],
        })
    return {"providers": items, "default": default,
            "webphone": {"connected": webphone.connected(), "path": "/phone", "token_required": bool(os.getenv("PHONE_TOKEN"))}}


class DefaultReq(BaseModel):
    provider: str


@router.put("/providers/default")
def set_default(body: DefaultReq):
    if body.provider not in catalog.PROVIDERS:
        raise HTTPException(400, "Unknown provider")
    if not catalog.available(body.provider):
        raise HTTPException(400, f"{catalog.label(body.provider)} is not set up yet")
    settings.save(default_provider=body.provider)
    return {"default": body.provider}


@router.post("/providers/{key}/check")
async def check(key: str):
    if key not in catalog.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    if not catalog.ready(key, "live"):
        raise HTTPException(400, f"Set {', '.join(catalog.missing(key, 'live'))} first")
    if key == "gemini":
        return await gemini_live.check()
    try:
        await elevenlabs._signed_url()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: could not get a conversation link for the agent"}
    return {"ok": True}
