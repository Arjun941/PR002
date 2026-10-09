"""Cost model in INR. One place for every rate, so the dashboard's "spent so far" and the
builder's pre-launch estimate agree.

The rates are placeholders until the first real Exotel and Sarvam bills arrive; calibrate
them here, not in the callers.
"""
from __future__ import annotations

import math

# Telephony: an answered keypad call fits in one 60 s pulse; extra pulses for long scripts.
# agent_extra: one escalated call on the ElevenLabs agent, about a minute (~$0.10/min plus its LLM).
COST = {"answered": 0.7, "agent_extra": 9.0, "unanswered": 0.3}
RETRY_ESTIMATE_PER_CALL = 0.45

# Sarvam: about ₹15 per 10k characters; ElevenLabs: about $0.22 per 1k characters on the Creator plan.
TTS_PER_CHAR = {"piper": 0.0, "sarvam": 15 / 10_000, "elevenlabs": 18 / 1_000}
DRAFT_CALL = {"template": 0.0, "chatgpt": 0.0, "ollama": 0.0, "sarvam": 0.5}  # the single LLM call per campaign

CHARS_PER_SEC = 14       # spoken rate used to turn script length into call length
KEYPRESS_SEC = 6         # time to listen and press a key
ESCALATION_RATE = 0.08   # share of answered calls that press 4 for the assistant
PICKUP = {"seminar": .58, "clinic": .71, "school": .64, "payment": .49}
SPOKEN = ("greeting", "message", "menu", "goodbye")


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


def estimate(*, kind: str, by_language: dict[str, int], scripts: dict[str, dict], max_attempts: int,
             escalation: bool, voice: str, text: str, avg_name_chars: float = 8) -> dict:
    """Expected campaign cost before launch, with the assumptions shown to the user."""
    p = PICKUP.get(kind, .6)
    m = max(1, max_attempts)
    attempts_each = sum((1 - p) ** k for k in range(m))
    answered_each = 1 - (1 - p) ** m
    n = sum(by_language.values())

    telephony = synth_chars = 0.0
    longest = 0.0
    for lang, count in by_language.items():
        s = scripts.get(lang, {})
        seconds = sum(len(s.get(f, "")) for f in SPOKEN) / CHARS_PER_SEC + KEYPRESS_SEC
        longest = max(longest, seconds)
        answered_call = COST["answered"] * max(1, math.ceil(seconds / 60))
        telephony += count * (answered_each * answered_call + (attempts_each - answered_each) * COST["unanswered"])
        synth_chars += sum(len(s.get(f, "")) for f in (*SPOKEN, "voicemail")) + count * (avg_name_chars + 4)

    answered = n * answered_each
    agent = answered * ESCALATION_RATE * COST["agent_extra"] if escalation else 0.0
    synth = synth_chars * TTS_PER_CHAR.get(voice, 0.0)
    draft = DRAFT_CALL.get(text, 0.0)
    total = telephony + agent + synth + draft

    lines = [
        {"label": "Phone calls", "detail": f"About {round(n * attempts_each):,} calls, {round(answered):,} expected to answer", "inr": telephony},
        {"label": "Voice pre-synthesis", "detail": f"{round(synth_chars):,} characters, once per language plus each name", "inr": synth},
        {"label": "Assistant escalations", "detail": f"About {ESCALATION_RATE:.0%} of answered calls press 4" if escalation else "Off: keypad only", "inr": agent},
        {"label": "Script drafting", "detail": "One language-model call for the whole campaign", "inr": draft},
    ]
    return {
        "lines": [l | {"inr": round(l["inr"], 2)} for l in lines],
        "total_inr": round(total, 2),
        "per_recipient_inr": round(total / n, 2) if n else 0,
        "recipients": n,
        "expected_calls": round(n * attempts_each),
        "expected_answered": round(answered),
        # The pitch: what the same campaign costs if a full voice agent handled every answered call.
        "all_agent_inr": round(telephony + synth + draft + answered * COST["agent_extra"], 2),
        "assumptions": {"pickup": p, "max_attempts": m, "call_seconds": round(longest),
                        "escalation_rate": ESCALATION_RATE if escalation else 0},
    }
