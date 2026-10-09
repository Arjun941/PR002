"""Exotel REST client (Phase 1). Outbound call to a number, routed into a flow that
contains the Voicebot/Stream applet pointing at our /ws/exotel WebSocket.

Credentials come from the environment (.env), never the repo:
  EXOTEL_SID, EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_SUBDOMAIN (default api.exotel.com),
  EXOTEL_CALLER_ID (your ExoPhone), EXOTEL_APP_ID (the flow with the voicebot applet).
"""
from __future__ import annotations

import os

import httpx


def mask(number: str) -> str:
    """Never log or return a full phone number."""
    digits = number.strip()
    return digits[:3] + "•" * max(len(digits) - 5, 0) + digits[-2:] if len(digits) > 5 else "•••"


def configured() -> bool:
    return all(os.getenv(k) for k in ("EXOTEL_SID", "EXOTEL_API_KEY", "EXOTEL_API_TOKEN",
                                       "EXOTEL_CALLER_ID", "EXOTEL_APP_ID"))


def auth() -> tuple[str, str]:
    return os.environ["EXOTEL_API_KEY"], os.environ["EXOTEL_API_TOKEN"]


async def place_call(to: str, *, custom_field: str | None = None, status_callback: str | None = None,
                     record: bool = False) -> dict:
    """Campaign calls pass custom_field (recipient id) and status_callback so the result comes back
    to /api/telephony/status. Param names are UNVERIFIED like the rest of Phase 1."""
    sid = os.environ["EXOTEL_SID"]
    host = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
    url = f"https://{host}/v1/Accounts/{sid}/Calls/connect.json"
    flow = f"http://my.exotel.com/{sid}/exoml/start_voice/{os.environ['EXOTEL_APP_ID']}"
    data = {"From": to, "CallerId": os.environ["EXOTEL_CALLER_ID"], "Url": flow}
    if custom_field:
        data["CustomField"] = custom_field
    if status_callback:
        data |= {"StatusCallback": status_callback, "StatusCallbackEvents[0]": "terminal",
                 "StatusCallbackContentType": "application/json"}
    if record:
        data["Record"] = "true"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, auth=auth(), data=data)
    resp.raise_for_status()
    call = resp.json().get("Call", {})
    return {"call_sid": call.get("Sid"), "status": call.get("Status"), "to": mask(to)}
