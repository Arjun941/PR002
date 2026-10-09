"""Campaign dialer. Every few seconds, for each running campaign, claim the next due recipient and ring the
phone page (webphone.py) if one is online and free: one call at a time, no calling-hours window (the phone is
yours). A campaign with no phone online shows a note and waits. Unanswered calls are retried per the campaign's
retry policy, and calls with no result after STALE_MINUTES are given up on.
"""
from __future__ import annotations

import asyncio
import logging

from . import store, webphone

log = logging.getLogger("reachout.dialer")

STALE_MINUTES = 10  # no result by then: give up on the attempt
WAITING = "Waiting for the phone: open /phone on a device and keep it open."


def _ring(c: dict) -> None:
    phone = webphone.idle_phone()
    if not phone:
        if webphone.connected() == 0 and c.get("note") != WAITING:
            store.set_note(c["id"], WAITING)
        return
    if c.get("note") == WAITING:
        store.set_note(c["id"], None)
    for cc, r in store.claim_due([c], 1):
        phone.state = "ringing"  # reserve it before the task starts
        asyncio.create_task(webphone.call(cc, r, phone))


async def _tick() -> None:
    running = [c for c in store.campaigns() if c["status"] == "running" and not c["simulated"]]
    store.expire_stale(store.ago(minutes=STALE_MINUTES))
    for c in running:
        store.refresh_status(c)
        _ring(c)


async def run() -> None:
    while True:
        await asyncio.sleep(2)
        try:
            await _tick()
        except Exception:
            log.exception("dialer tick failed")
