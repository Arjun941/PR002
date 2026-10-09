# Reachout: project context for Claude Code

Hackathon project (PR 002): turn a template plus event details into a live multilingual outbound
calling campaign for institutions (seminars, clinic reminders, school-parent notices, payment
reminders). Telephony was Exotel; it was removed for now (2026-10-09, on request) and calls ring a phone page (`/phone`). `exotel.py` and `voicebot.py` are in git history.

## Design principles (do not drift from these)

1. Cost is a feature. Target users are schools, clinics and small institutions. The cheapest AI
   minute is the one never run. (The app itself shows no cost figures or estimates: removed on request, 2026-10-10.)
2. Hybrid call flow, keypad first:
   greet -> play pre-synthesised message -> collect DTMF (1 confirm, 2 decline, 3 reschedule)
   -> per-event follow-up keypad questions -> (assistant on) "any other questions?" and a short
   listening window: only a caller who starts speaking reaches the full LLM + streaming TTS agent.
   (Changed by the user on 2026-10-09: the agent used to be on key 4.)
3. Pre-synthesise templates once per language per campaign. Per-recipient variable slots (name,
   date, venue) are synthesised in a batch before the campaign starts.
4. Self-hosted orchestrator (laptop, small VPS or institution server). No Vapi-style platform fee.
5. Providers are pluggable (STT, TTS, LLM), each tagged with region and what leaves the machine
   (audio, text or nothing). Only ElevenLabs and Gemini are wired up for now (`backend/catalog.py`);
   add another there. Do not rely on CPU Whisper for live calls.
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
- Data lives in SQLite by default, or MongoDB when `MONGODB_URI` is set. `backend/store.py` is the only door:
  every query is a function there, implemented in `store_sqlite.py` and `store_mongo.py` (shared helpers in
  `dbcommon.py`). No raw SQL outside `store_sqlite.py`; a new data operation needs both backends. Copy an existing
  SQLite file with `python -m backend.migrate_to_mongo`. Mongo phone numbers are plain text until Phase 6 encryption.
  Sign in with ChatGPT (`backend/chatgpt.py`, strip in the assistant panel): when connected, `llm.run_json` tries the
  connected plan first for drafts and the assistant, then the campaign's own model, then templates. It only writes text;
  live calls stay on ElevenLabs/Gemini. One account per install. Refresh/`/models`/SSE details UNVERIFIED.
  Reminders and updates (`backend/notices.py`, `NoticeDrawer.tsx`, "Remind / update" on the campaign page): a spoken message about the
  event, written by the assistant (or typed, then translated into every campaign language), sent to people chosen by outcome (confirmed,
  declined, voicemail, unfinished ...) now or at a set time. Delivery is a web-phone call run by a background runner, one call at a time,
  through `webphone._converse` with a synthetic campaign (`_notice`); the audience is decided when it is due; calls never touch a
  recipient's campaign outcome/answers and appear in History tagged Reminder/Update. Stored in `notices` (both backends).
  Call history (`/history` page, `backend/history.py`): `recwire.py` writes one record per call when it ends
  (`store.save_call`, table/collection `call_history`: ring/answer/end times, outcome, keypad answers, recording status
  with the reason when there is none); `callinsight.py` then sends the stereo recording (agent left, person right) to
  Gemini once for summary, every question asked + answer (`qa`) and a transcript (`CALL_ANALYSIS=0` turns it off; audio
  request format UNVERIFIED). Recordings are one per call (`recording_audio` keyed by call id; recipient
  `recording_url` = `app:<call id>`). recwire also rewrites the phone's "incoming" message so the caller shown is the
  event's org, not the recipient's own name.
  Call recording: `backend/recwire.py` is an ASGI middleware that watches the `/ws/phone` socket from outside (no
  edits to webphone/ivr/the agent engines), so live-agent and IVR calls are both recorded; `callrec.py` mixes the two
  sides to one WAV, `recstore.py` saves it (optional Fernet `RECORDINGS_KEY`) in `recording_audio`, and it is deleted
  with its campaign. Every call is recorded unless `RECORD_CALLS=campaign` (the builder hard-codes record=0 now). Demo data is opt-in: `DEMO=1` seeds sample
  campaigns into an empty DB and simulates calls for campaigns flagged `simulated` (`backend/demo.py`).
- Phase 4: builder API in `backend/builder.py`, drafting in `backend/llm.py` (Gemini / built-in
  templates; non-English template output is flagged as an English placeholder), rates and estimate in
  UI at `frontend/app/campaigns/new`. Launch is refused unless the dialer is ready
  (removed with Exotel: campaigns now launch without telephony setup).
- Phase 5: `backend/dialer.py` places calls (concurrency, `CALL_WINDOW`, retry policy, pause on
  auth/network errors) and handles `POST /api/telephony/status`; the voicebot saves DTMF 1/2/3 by
  call sid. Answered with no digit = voicemail. `backend/recordings.py`: PIN-unlocked 15-minute
  session cookie, audio proxied through the server, every unlock/play in `access_log`.
- Dashboard assistant: floating bar at the bottom of every page except the builder, mounted in `Shell`
  (`frontend/components/AssistantChat.tsx`, `backend/assistant.py`, `POST /api/assistant/chat`). It asks until
  every required field for the campaign type is known: `assistant.REQUIRED` (org, title, date, time, venue,
  languages; payment: due date + amount instead of time/venue). The server computes `missing` itself and
  ignores the model's `ready`; languages are never assumed. When complete, the bar calls `/api/builder/draft`
  with the same provider to write the scripts in those languages (the campaign's one drafting call) and hands
  event + draft to the builder via sessionStorage (`Prefill`), keyed so later edits mark the draft stale.
  It never creates or launches a campaign: contacts and the script review stay with a human.
  Uses `llm.run_json`, the same Gemini drafting as campaigns; 503 if no key is set.
- Voice input in the assistant bar (`frontend/components/voice.ts`): mic + speech-language picker
  (Auto-detect default, or en/hi/mr/ta/kn/ml-IN; remembered per browser, read after mount to avoid hydration
  mismatches). A server speech service is preferred when configured (more accurate for Indian languages, can
  auto-detect): the browser records, stops after 3.5 s of quiet once speech began (mic-level check) or on tap,
  converts to 16 kHz WAV and POSTs `/api/assistant/transcribe` (`backend/stt.py`: Sarvam, then ElevenLabs Scribe;
  formats UNVERIFIED). Without one, Chrome/Edge recognition is used (restarted when the browser ends it on a
  pause). The transcript goes into the input box to review and edit, never sent on its own; then the assistant prompt accepts any Indian language or a mix, replies in the
  user's language, writes event fields in English, and asks to confirm unclear transcribed facts.
- Dynamic call questions: the drafting call also picks 0-4 follow-up keypad questions for the event
  (`llm._questions` sanitises: ids q1..q4, 2-6 options, option n = key n, `only_if_confirmed`), with spoken text
  per language in `scripts[lang].questions`. The main 1/2/3(/4) answer stays fixed so outcomes keep their meaning.
  Stored in `campaigns.questions`; answers in `recipients.answers` ({id: key}). The voicebot asks them after the
  main answer (`_follow_ups`; wrong key or silence repeats once, then skips). Builder: `QuestionsEditor`; campaign
  page: answer counts per option. The assistant may expand title/details wording (flagged `suggested`) but facts
  (dates, venues, amounts, names) still come only from the user.
- ElevenLabs is the call voice and the end-of-call agent (`backend/elevenlabs.py`). Live campaigns start in
  status `preparing`: `backend/audio.py` synthesises every script phrase per language and every
  recipient name once (greeting split around `{name}`), caches PCM16 8 kHz in `AUDIO_DIR`
  (content-addressed, so repeats are free), then sets `running`. Failure pauses the campaign with a
  `note`; resume re-runs only what is missing. ElevenLabs and Gemini both make IVR audio (`catalog` cap `voice`).
- `backend/voicebot.py` call flow: intro + menu -> key 1/2/3 saves outcome -> follow-up questions ->
  `_closing`: with the assistant on, play `scripts[lang].doubts` ("any other questions?") and listen
  `DOUBT_WAIT_SECONDS`; `_hear_question` is a loudness check (`VAD_RMS`, 400 ms of speech within 1 s), no
  STT. Speech -> the ElevenLabs agent (signed URL, audio converted 8 kHz <-> agent format, the last second
  of caller audio sent first as `preroll`, `{{answer}}` = keypad outcome, `record_outcome` client tool,
  capped at `AGENT_MAX_SECONDS`); silence, a key or hang-up -> goodbye. Other keys at the menu replay it;
  no key after asking twice -> voicemail message (voicemail boxes never reach the agent). Calls with no
  campaign audio (the test call) get the Phase 1 tone test. Escalation availability depends on
  `ELEVENLABS_AGENT_ID`, not on the drafting model.
- Delete: `DELETE /api/campaigns/{id}` (`store.delete_campaign`, both backends) removes campaign,
  recipients and call log; refused (409) while running or preparing; logged as `campaign.delete`.
- Testing gotcha: `.env` sets `MONGODB_URI` (Atlas, the real data) and `backend/__init__.py` loads it, so a
  test that only sets `REACHOUT_DB` still hits Atlas. Set `MONGODB_URI=` (empty) for SQLite tests, or a
  throwaway `MONGODB_DB` on the local mongod.
- UNVERIFIED (written from memory): ElevenLabs TTS `output_format=ulaw_8000`, signed-URL endpoint,
  agent WebSocket event names; Exotel `clear` event; Exotel callback formats.

- Providers (`backend/catalog.py`): exactly ElevenLabs and Gemini. Each declares capabilities with the env vars they need:
  `draft` (writes scripts + the agent system prompt: Gemini only), `voice` (pre-synthesised IVR audio: ElevenLabs only, whichever provider runs the call; Gemini speech was dropped for its tiny quota) and
  `live` (the conversation: both). A campaign stores one `provider` (ElevenLabs cannot draft, so Gemini writes its text) and
  a `mode`, `live` or `hybrid`. Default provider for new campaigns: Providers page -> `settings.json`.
  Sarvam was written (STT + chat + TTS conversation engine) and removed on request; ask before reviving it.
- System prompt: the drafting call also writes `system_prompt` (English, `{name}`/`{language}` placeholders); it is shown
  and editable in the builder and on the campaign page. `backend/callctx.py` builds what a live agent gets per call:
  the prompt filled for that person + event facts + the approved script in their language + follow-up questions + how to
  record outcomes. Engines (`gemini_live.bridge`, `elevenlabs.bridge`) take `instructions`, `opening`, `questions`,
  `on_answer`; ElevenLabs gets them as a per-conversation override (the agent must allow prompt/first_message/language
  overrides and have the `record_answer` tool: re-run `python -m backend.setup_elevenagent` after changes there).
- Phone (`backend/webphone.py`, page `backend/static/phone.html`, `/phone` + `WS /ws/phone`): a browser page that rings; the only
  telephony. `dialer._ring` claims one recipient at a time (no call window; note "Waiting for the phone" when none is online)
  and `webphone.call` runs the campaign's mode. Audio is 8 kHz telephone quality both ways, noise-gated on the mic.
  - live mode: `_converse` runs the provider's engine for the whole call; it connects while the phone rings (opening held
    until answer).
  - hybrid mode: `_hybrid` + `ivr.py` play the pre-synthesised IVR (greeting+name, message, menu; keys on the page's in-call keypad
    save the outcome and follow-up answers; voicemail line after two silent menus), then "any other questions?"; speaking hands
    the call to the provider's live engine with the IVR answer in its instructions.
  - IVR audio (`audio.py`): synthesised when a hybrid campaign is created (status "preparing"), listened to in the builder
    (`POST /api/builder/preview-audio`) and on the campaign page ("IVR Responses", Listen buttons, Synthesize/Resynthesize via
    `POST /api/campaigns/{id}/ivr/synthesize`). Cache is content-addressed per provider+voice+model+text.
  - Verified: both providers' live calls with a scripted fake page, IVR logic with a fake line (confirm+follow-up, voicemail,
    replay), preview/synthesise/listen via the API. Not verified: a hybrid call end to end with a person, the hybrid agent
    takeover, Gemini IVR for a campaign with many names (quota).

- Campaign agent (`backend/campaignagent.py`, `POST /api/assistant/agent`, dock chat `frontend/components/AssistantChat.tsx`):
  the same chat bar is the dashboard assistant (scope "create"), the builder agent ("builder") and a campaign's agent
  ("campaign", chosen by URL). Every turn the model gets the campaign's full current state + a summary of the older chat + the
  last `WINDOW` turns, and returns `{reply, edits, actions}`; `campaignagent.clean` whitelists and clamps it. Builder: the
  browser applies edits to the form through `lib/agentbridge.ts` (the builder page registers a bridge; actions: `redraft`).
  Campaign: the server applies them through `edit_campaign` (so only while paused/finished; actions `pause`,
  `resynthesize`; event, name, provider, mode, retry, system prompt, scripts) and saves the chat on the campaign
  (`chat`, `chat_summary`; oldest turns are folded into the summary after `SUMMARISE_AT`). The create-scope chat is carried
  to the builder (sessionStorage `reachout.chat`) and saved on the campaign at creation (`CreateReq.chat`), so the agent on
  the campaign page continues it. Everything on a created campaign is editable (it must be paused or finished), by hand (Edit page, per-question Save button) and by the agent (`question_ops`: add/update/remove). Changing or removing options keeps saved answers meaningful: `main._remap_answers` moves each saved key press to its option's new position by text, keeps it for an in-place rename, and clears it if the option is gone. A new or changed question's spoken text is translated into every language (`/builder/translate-question`) unless the agent wrote it.
  Verified via the API (builder + campaign scopes, memory across turns); the UI wiring is type-checked but not clicked through.

## Roadmap

- [~] Phase 1 (code written, untested against real Exotel: needs credentials, Cloudflare Tunnel, a flow with the voicebot applet): Exotel skeleton. Outbound call via API to a test number, voicebot/stream applet to
      a WebSocket endpoint (Cloudflare Tunnel for local dev), play a prompt, read a DTMF digit
      back. Do this first: it is the riskiest integration. Check Exotel's audio format and
      whether answering-machine detection is available.
- [~] Phase 2 (code written with ElevenLabs voice; untested on a real call): DTMF-first call state machine,
      pre-synthesised templates, voicemail handling, persistent storage (SQLite) replacing the in-memory mock data.
- [~] Phase 3 (ElevenLabs agent at the end of the call, untested on a real call): conversational escalation. Consider Pipecat or LiveKit Agents as the pipeline;
      write only the Exotel transport. Providers: ElevenLabs and Gemini (Sarvam postponed).
- [x] Phase 4: campaign builder (paste event details, pick languages, review drafts
      before launch, launch).
- [~] Phase 5 (code written; real-call path tested only against a mocked Exotel): dashboard on real
      data; recording playback behind access control.
- [ ] Phase 6: encryption at rest, retention and auto-delete, access logs, README with the
      architecture.

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

- Languages and contacts of an existing campaign are editable (Edit page sections save straight away; the agent via `languages` and
  `contact_ops`). `PATCH /api/campaigns/{id}` with `languages`: new ones get the script translated (`llm.translate_scripts`, one
  call, flagged if a language came back unusable); removing one is refused while contacts still use it. `POST /api/campaigns/{id}/contacts`
  adds (CSV), updates (name/language/segment) and removes contacts; removing erases the contact's call log, history and recordings. A
  finished campaign that gets new contacts becomes paused. The agent sees only contact counts, never names or numbers; it targets people
  with filters (language, segment, outcome, name_contains) and never bulk-changes without one.
