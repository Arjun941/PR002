"""Create (or update) the Reachout key-4 assistant on ElevenLabs Agents, and wire it into .env.

    python -m backend.setup_elevenagent            # from the reachout folder; needs ELEVENLABS_API_KEY in .env

What it sets up on your ElevenLabs account:
  - an agent named "Reachout assistant" whose prompt uses the dynamic variables the app sends at call
    start ({{org}} {{title}} {{when}} {{venue}} {{details}} {{language}} {{answer}}),
  - client tools `record_outcome(outcome)` and `record_answer(question_id, option_number)` that the app
    handles (backend/elevenlabs.py),
  - per-conversation overrides for the system prompt, first message and language: each campaign sends its
    own prompt and everything it knows, so one agent serves every campaign.
It then writes ELEVENLABS_AGENT_ID (and ELEVENLABS_VOICE_ID, if empty) to .env. Safe to re-run: an agent
with the same name is updated in place. Audio: the agent uses ElevenLabs' default 16 kHz PCM; the bridge
reads the actual formats from the conversation metadata, so nothing here needs to match Exotel.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

ENV = Path(__file__).resolve().parent.parent / ".env"
API = "https://api.elevenlabs.io"
NAME = "Reachout assistant"
VOICE = os.getenv("SETUP_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # Sarah: mature, reassuring (premade, on every plan)

PROMPT = """You are a phone assistant for {{org}}. You have just called someone with an automated message about \
"{{title}}". They answered the automated menu with: {{answer}}. At the end they were asked if they have any other questions \
and started speaking, so you answer them: their first words reach you as soon as you connect, so do not greet them. Be warm, \
brief and natural, like a helpful receptionist: one or two short sentences at a time, no lists, no markdown, and let the \
caller talk.

What the call was about:
- Organisation: {{org}}
- About: {{title}}
- When: {{when}}
- Where: {{venue}}
- Details: {{details}}

Speak {{language}} unless the caller switches language. Answer using only the facts above; if you do not know something, \
say that someone will follow up. When the caller clearly says they will attend or pay, declines, or wants to reschedule, call the \
record_outcome tool with confirmed, declined or rescheduled, then say a short goodbye."""

CONFIG = {
    "name": NAME,
    "conversation_config": {
        "agent": {
            "first_message": "",  # empty: the agent waits; the caller's first words arrive as preroll audio
            "language": "en",
            "prompt": {
                "prompt": PROMPT,
                "tools": [{
                    "type": "client", "name": "record_outcome", "expects_response": True,
                    "description": "Save the caller's answer to the invitation or reminder.",
                    "parameters": {"type": "object", "required": ["outcome"], "properties": {
                        "outcome": {"type": "string", "description": "One of: confirmed, declined, rescheduled"}}},
                }, {
                    "type": "client", "name": "record_answer", "expects_response": True,
                    "description": "Save the caller's answer to one of the follow-up questions.",
                    "parameters": {"type": "object", "required": ["question_id", "option_number"], "properties": {
                        "question_id": {"type": "string", "description": "The question's id, for example q1"},
                        "option_number": {"type": "integer", "description": "The number of the option the caller chose"}}},
                }, {
                    "type": "client", "name": "end_call", "expects_response": False,
                    "description": "Hang up the call. Only when the caller asks to end it, or the whole conversation is finished and you "
                                   "have already said goodbye.",
                    "parameters": {"type": "object", "properties": {}},
                }],
            },
        },
        "tts": {"voice_id": VOICE},
    },
    "platform_settings": {"overrides": {"conversation_config_override": {"agent": {
        "language": True, "first_message": True, "prompt": {"prompt": True}}}}},
}


def read_env() -> dict[str, str]:
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_env(updates: dict[str, str]) -> None:
    text = ENV.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    for k, v in updates.items():
        if re.search(rf"^{k}=.*$", text, flags=re.M):
            text = re.sub(rf"^{k}=.*$", lambda _m, k=k, v=v: f"{k}={v}", text, count=1, flags=re.M)
        else:
            text += ("" if text.endswith(("\n", "\r")) else nl) + f"{k}={v}{nl}"
    ENV.write_text(text, encoding="utf-8")


def main() -> None:
    env = read_env()
    key = os.getenv("ELEVENLABS_API_KEY") or env.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("Set ELEVENLABS_API_KEY in .env first")
    h = {"xi-api-key": key}
    with httpx.Client(base_url=API, headers=h, timeout=60) as c:
        existing = c.get("/v1/convai/agents", params={"search": NAME})
        existing.raise_for_status()
        found = next((a for a in existing.json().get("agents", []) if a.get("name") == NAME), None)
        if found:
            r = c.patch(f"/v1/convai/agents/{found['agent_id']}", json=CONFIG)
            agent_id, verb = found["agent_id"], "updated"
        else:
            r = c.post("/v1/convai/agents/create", json=CONFIG)
            agent_id, verb = (r.json().get("agent_id") if r.status_code == 200 else None), "created"
        if r.status_code >= 400:
            sys.exit(f"ElevenLabs refused the agent config ({r.status_code}): {r.text[:600]}")
        print(f"{verb} agent {agent_id}")
    updates = {"ELEVENLABS_AGENT_ID": agent_id, "ELEVENLABS_AGENT_LANGUAGE_OVERRIDE": "1"}
    if not env.get("ELEVENLABS_VOICE_ID"):
        updates["ELEVENLABS_VOICE_ID"] = VOICE  # also voices the pre-recorded campaign scripts
    write_env(updates)
    print("wrote", ", ".join(updates), "to .env")


if __name__ == "__main__":
    main()
