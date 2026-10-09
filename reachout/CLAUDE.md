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

- Phase 0 done: FastAPI mock API in `backend/main.py`, Next.js (App Router, TypeScript) dashboard in
  `frontend/`, proxying `/api` to FastAPI (converted from vanilla JS). Response shapes are the contract real data must fill in, so avoid changing
  them without updating `frontend/lib/types.ts` and the pages.
- Demo simulator (`DEMO_LIVE`) must be removed or made opt-in once real calls exist.

- Phase 1 code: `backend/exotel.py` (REST call via `POST /api/telephony/test-call`), `backend/voicebot.py`
  (`/ws/exotel`: plays tones, reads DTMF back as beeps). Exotel API/event/audio-format details were
  written from memory and are UNVERIFIED; check them on the first real call. Copy `.env.example` to `.env`.

## Roadmap

- [~] Phase 1 (code written, untested against real Exotel: needs credentials, Cloudflare Tunnel, a flow with the voicebot applet): Exotel skeleton. Outbound call via API to a test number, voicebot/stream applet to
      a WebSocket endpoint (Cloudflare Tunnel for local dev), play a prompt, read a DTMF digit
      back. Do this first: it is the riskiest integration. Check Exotel's audio format and
      whether answering-machine detection is available.
- [ ] Phase 2: DTMF-first call state machine, pre-synthesised templates, voicemail handling,
      persistent storage (SQLite) replacing the in-memory mock data.
- [ ] Phase 3: conversational escalation. Consider Pipecat or LiveKit Agents as the pipeline;
      write only the Exotel transport. Sarvam as the default India-hosted provider, Ollama + Piper
      as the local option.
- [ ] Phase 4: campaign builder (paste event details, pick languages, review drafts, cost estimate
      before launch, launch).
- [ ] Phase 5: dashboard on real data; recording playback behind access control.
- [ ] Phase 6: encryption at rest, retention and auto-delete, access logs, README with the cost
      comparison and architecture.

## Conventions

- Python 3.11+, FastAPI. Keep dependencies minimal; hackathon time is short.
- Frontend: Next.js App Router + TypeScript, client components, no UI library. Match the existing dark
  UI tokens in `frontend/app/globals.css`.
- Secrets (Exotel keys, provider API keys) go in `.env`, never in the repo. Add `.env` to
  `.gitignore` before the first commit.
- Never log full phone numbers or transcripts at INFO level.
- Run the app: `uvicorn backend.main:app --reload` (API on :8000) and, in `frontend/`, `npm install` then
  `npm run dev`; open http://localhost:3000. Set `BACKEND_URL` if the API is elsewhere.
- When adding a feature, update the roadmap checkboxes above.
