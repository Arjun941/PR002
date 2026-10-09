# Reachout: project context for Claude Code

Hackathon project (PR 002): turn a template plus event details into a live multilingual outbound
calling campaign for institutions (seminars, clinic reminders, school-parent notices, payment
reminders). Telephony is Exotel (access provided at kickoff).

## Design principles (do not drift from these)

1. Cost is a feature. Target users are schools, clinics and small institutions. The cheapest AI
   minute is the one never run.
2. Hybrid call flow, keypad first:
   greet -> play pre-synthesised message -> collect DTMF (1 confirm, 2 decline, 3 reschedule,
   4 talk to assistant) -> short STT window with a tiny intent classifier only if needed ->
   full LLM + streaming TTS only on escalation or off-script speech.
3. Pre-synthesise templates once per language per campaign. Per-recipient variable slots (name,
   date, venue) are synthesised in a batch before the campaign starts.
4. Self-hosted orchestrator (laptop, small VPS or institution server). No Vapi-style platform fee.
5. Providers are pluggable (STT, TTS, LLM), each tagged with region and what leaves the machine
   (audio, text or nothing). Local models (Piper, Ollama, Whisper) are first-class options,
   mainly for batch pre-synthesis. Do not rely on CPU Whisper for live calls.
6. Contact lists and recordings are personal data: mask phone numbers in API responses, encrypt
   at rest, enforce retention, log access. The UI must show where voice and language processing
   happens for each campaign.
7. Voicemail: no DTMF or speech shortly after the greeting, or a detected beep -> mark voicemail,
   play a short pre-recorded message, schedule a retry. Use Exotel answering-machine detection if
   the plan includes it.
8. Agentic setup is one LLM call per campaign (draft scripts in all languages, DTMF menu, retry
   policy, voicemail text), reviewed by a human before launch. Never one LLM call per recipient
   for deterministic content.

## Current state

- Phase 0 done: FastAPI API in `backend/main.py`, Next.js (App Router, TypeScript) dashboard in
  `frontend/`, proxying `/api` to FastAPI. Response shapes are the contract, so avoid changing
  them without updating `frontend/lib/types.ts` and the pages.
- Data lives in SQLite (`backend/store.py`, stdlib only). Demo data is opt-in: `DEMO=1` seeds sample
  campaigns into an empty DB and simulates calls for campaigns flagged `simulated` (`backend/demo.py`).
- Phase 4: builder API in `backend/builder.py`, drafting in `backend/llm.py` (Ollama / Sarvam / built-in
  templates; non-English template output is flagged as an English placeholder), rates and estimate in
  `backend/costs.py`, UI at `frontend/app/campaigns/new`. Launch is refused unless the dialer is ready
  (Exotel + `PUBLIC_URL` + `WEBHOOK_TOKEN`) or `DEMO=1`.
- Phase 5: `backend/dialer.py` places calls (concurrency, `CALL_WINDOW`, retry policy, pause on
  auth/network errors) and handles `POST /api/telephony/status`; the voicebot saves DTMF 1/2/3 by
  call sid. Answered with no digit = voicemail. `backend/recordings.py`: PIN-unlocked 15-minute
  session cookie, audio proxied through the server, every unlock/play in `access_log`.
- Sign in with ChatGPT: `backend/chatgpt.py` is a drafting provider that spends the connected account's ChatGPT
  plan (OAuth + PKCE, loopback redirect to `/auth/callback`, credentials in `CHATGPT_AUTH_FILE`). Fallback
  when not connected, over the limit or ineligible: Ollama, then Sarvam, then templates (`llm.FALLBACK`), each
  step shown as a warning. Drafting only (`live=False`): key-4 escalation never uses it. One account per install,
  shared by all users of that server. This is OpenAI's local/open-source flow; a hosted multi-user deployment
  needs OpenAI approval. Refresh request, `/models` and SSE event names are UNVERIFIED: test on first sign-in.
- Dashboard assistant: chat on the Overview page (`frontend/components/AssistantChat.tsx`, `backend/assistant.py`,
  `POST /api/assistant/chat`) turns a plain-words description into the builder's event form (prefill via
  sessionStorage). It never creates or launches a campaign: contacts and the script review stay with a human.
  Uses `llm.run_json`, the same ChatGPT -> Ollama -> Sarvam chain as drafting; 503 if none is available.
- ElevenLabs is the call voice and the key-4 agent (`backend/elevenlabs.py`). Live campaigns start in
  status `preparing`: `backend/audio.py` synthesises every script phrase per language and every
  recipient name once (greeting split around `{name}`), caches PCM16 8 kHz in `AUDIO_DIR`
  (content-addressed, so repeats are free), then sets `running`. Failure pauses the campaign with a
  `note`; resume re-runs only what is missing. Only ElevenLabs can launch real calls today; Piper and
  Sarvam voices are simulation-only until their synthesis is written.
- `backend/voicebot.py` call flow: intro + menu -> key 1/2/3 saves outcome + goodbye; key 4 bridges the
  caller to the ElevenLabs agent (signed URL, audio converted 8 kHz <-> agent format, the agent's
  `record_outcome` client tool saves the outcome with channel `agent`); other keys replay the menu;
  no key after asking twice -> voicemail message, hang up (status callback marks voicemail). Calls with
  no campaign audio (the test call) get the Phase 1 tone test. Escalation availability now depends on
  `ELEVENLABS_AGENT_ID`, not on the drafting model.
- UNVERIFIED (written from memory): ElevenLabs TTS `output_format=ulaw_8000`, signed-URL endpoint,
  agent WebSocket event names; Exotel `clear` event; Sarvam/Ollama/Exotel callback formats.

- Phase 1 code: `backend/exotel.py` (REST call via `POST /api/telephony/test-call`), `backend/voicebot.py`
  (`/ws/exotel`: plays tones, reads DTMF back as beeps). Exotel API/event/audio-format details were
  written from memory and are UNVERIFIED; check them on the first real call. Copy `.env.example` to `.env`.

- Live assistant on key 4 is pluggable (`backend/agents.py`): Gemini Live (`backend/gemini_live.py`, default model
  gemini-3.8-live, `GEMINI_API_KEY`) or the ElevenLabs agent. Chosen per campaign (`agent_provider`), default via
  `LIVE_AGENT`. Providers page (`/providers`, `backend/providers.py`) lists every provider and tests Gemini Live.
  ElevenLabs agent is created by `python -m backend.setup_elevenagent` (writes ELEVENLABS_AGENT_ID/VOICE_ID to .env).
  Both assistants verified with a fake telephony line; not yet on a real Exotel call. Other Gemini Live models misbehaved in tests
  (3.1 preview closed sessions with 1011, 2.5 native audio took 7-14 s to reply).

## Roadmap

- [~] Phase 1 (code written, untested against real Exotel: needs credentials, Cloudflare Tunnel, a flow with the voicebot applet): Exotel skeleton. Outbound call via API to a test number, voicebot/stream applet to
      a WebSocket endpoint (Cloudflare Tunnel for local dev), play a prompt, read a DTMF digit
      back. Do this first: it is the riskiest integration. Check Exotel's audio format and
      whether answering-machine detection is available.
- [~] Phase 2 (code written with ElevenLabs voice; untested on a real call): DTMF-first call state machine,
      pre-synthesised templates, voicemail handling, persistent storage (SQLite) replacing the in-memory mock data.
- [~] Phase 3 (ElevenLabs agent bridge on key 4, untested on a real call): conversational escalation. Consider Pipecat or LiveKit Agents as the pipeline;
      write only the Exotel transport. Sarvam as the default India-hosted provider, Ollama + Piper
      as the local option.
- [x] Phase 4: campaign builder (paste event details, pick languages, review drafts, cost estimate
      before launch, launch).
- [~] Phase 5 (code written; real-call path tested only against a mocked Exotel): dashboard on real
      data; recording playback behind access control.
- [ ] Phase 6: encryption at rest, retention and auto-delete, access logs, README with the cost
      comparison and architecture.

## Conventions

- Python 3.11+, FastAPI. Keep dependencies minimal; hackathon time is short.
- Frontend: Next.js App Router + TypeScript, client components, no UI library. Match the existing dark
  UI tokens in `frontend/app/globals.css`.
- Secrets (Exotel keys, provider API keys) go in `.env`, never in the repo. `backend/__init__.py` loads
  `.env` before any module reads settings (several read env at import time); real env vars win. Add `.env` to
  `.gitignore` before the first commit.
- Never log full phone numbers or transcripts at INFO level.
- Run the app: `uvicorn backend.main:app --reload` (API on :8000) and, in `frontend/`, `npm install` then
  `npm run dev`; open http://localhost:3000. Set `BACKEND_URL` if the API is elsewhere.
- When adding a feature, update the roadmap checkboxes above.
