"""Cost model in INR. One place for every rate, so the dashboard's "spent so far" and the
builder's pre-launch estimate agree.

The rates are placeholders until the first real Exotel, ElevenLabs and Gemini bills arrive; calibrate
them here, not in the callers.
"""
from __future__ import annotations

import math

# Telephony: an answered keypad call fits in one 60 s pulse; extra pulses for long scripts.
# agent_extra: one escalated call on the ElevenLabs agent, about a minute (~$0.10/min plus its LLM).
COST = {"answered": 0.7, "agent_extra": 9.0, "unanswered": 0.3}
RETRY_ESTIMATE_PER_CALL = 0.45

# Pre-synthesised IVR audio: ElevenLabs about $0.22 per 1k characters on the Creator plan.
TTS_PER_CHAR = {"elevenlabs": 18 / 1_000}
DRAFT_CALL = 0.5  # the single LLM call per campaign (script text plus the agent prompt)
# A full live conversation, per minute: ElevenLabs agents about $0.10, Gemini Live roughly a third of that.
LIVE_PER_MIN = {"elevenlabs": 9.0, "gemini": 3.0}
WEB_CALL_MINUTES = 2  # a web phone call is a live conversation start to finish

CHARS_PER_SEC = 14       # spoken rate used to turn script length into call length
KEYPRESS_SEC = 6         # time to listen and press a key
ESCALATION_RATE = 0.08   # share of answered calls that ask the assistant a question at the end
PICKUP = {"seminar": .58, "clinic": .71, "school": .64, "payment": .49}
SPOKEN = ("greeting", "message", "menu", "doubts", "goodbye")  # doubts: only with the assistant on


def recipient_cost(r: dict) -> float:
    """What one recipient has cost so far."""
    if r["outcome"] == "pending":
        return r["attempts"] * COST["unanswered"]
    cost = (r["attempts"] - 1) * COST["unanswered"]
    if r["outcome"] in ("confirmed", "declined", "rescheduled"):
        cost += COST["answered"] + (COST["agent_extra"] if r["channel"] == "agent" else 0)
    else:
        cost += COST["unanswered"]
    return cost


def _spoken(s: dict, fields=SPOKEN) -> int:
    return sum(len(s.get(f, "")) for f in fields) + sum(len(t) for t in (s.get("questions") or {}).values())


def estimate(*, kind: str, by_language: dict[str, int], scripts: dict[str, dict], max_attempts: int,
             provider: str, mode: str, questions: int = 0, avg_name_chars: float = 8) -> dict:
    """Expected campaign cost before launch, with the assumptions shown to the user. No telephony charge:
    calls go to the phone page."""
    p = PICKUP.get(kind, .6)
    m = max(1, max_attempts)
    attempts_each = sum((1 - p) ** k for k in range(m))
    answered_each = 1 - (1 - p) ** m
    n = sum(by_language.values())
    answered = n * answered_each
    rate = LIVE_PER_MIN.get(provider, 9.0)
    all_live = answered * WEB_CALL_MINUTES * rate

    synth_chars = 0.0
    seconds = 0.0
    for lang, count in by_language.items():
        s = scripts.get(lang, {})
        seconds = max(seconds, _spoken(s) / CHARS_PER_SEC + KEYPRESS_SEC * (1 + questions))
        synth_chars += _spoken(s, (*SPOKEN, "voicemail")) + count * (avg_name_chars + 4)

    if mode == "live":
        lines = [
            {"label": "Live conversation", "detail": f"About {WEB_CALL_MINUTES} min per answered call at the provider's live rate", "inr": all_live},
            {"label": "IVR audio", "detail": "Not used in live mode", "inr": 0.0},
        ]
        call_seconds = WEB_CALL_MINUTES * 60
        total = all_live + DRAFT_CALL
    else:
        synth = synth_chars * TTS_PER_CHAR["elevenlabs"]
        agent = answered * ESCALATION_RATE * rate
        lines = [
            {"label": "IVR audio", "detail": f"{round(synth_chars):,} characters, synthesised once per language plus each name", "inr": synth},
            {"label": "Agent takeovers", "detail": f"About {ESCALATION_RATE:.0%} of answered calls ask a question at the end", "inr": agent},
        ]
        call_seconds = round(seconds)
        total = synth + agent + DRAFT_CALL
    lines.append({"label": "Script drafting", "detail": "One language-model call for the whole campaign", "inr": DRAFT_CALL})
    return {
        "lines": [l | {"inr": round(l["inr"], 2)} for l in lines],
        "total_inr": round(total, 2),
        "per_recipient_inr": round(total / n, 2) if n else 0,
        "recipients": n,
        "expected_calls": round(n * attempts_each),
        "expected_answered": round(answered),
        # The pitch: what the same campaign costs if the live agent handled every answered call.
        "all_agent_inr": round(all_live + DRAFT_CALL, 2),
        "assumptions": {"pickup": p, "max_attempts": m, "call_seconds": call_seconds,
                        "escalation_rate": ESCALATION_RATE if mode == "hybrid" else 0},
    }
