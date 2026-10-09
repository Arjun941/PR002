"""Browser test UI for Gemini Live: talk to it from your laptop mic, no Asterisk needed.

  pip install flask flask-sock google-genai
  set GEMINI_API_KEY=...   (PowerShell: $env:GEMINI_API_KEY="...")
  python web_ui.py         # open http://localhost:5000, click Start, use headphones

Browser sends 16 kHz PCM16 over a WebSocket; we relay it to Gemini Live and stream Gemini's
24 kHz PCM16 reply (plus transcripts) back. Protocol: binary frames = audio, text frames = JSON events.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading

from flask import Flask, Response, request
from flask_sock import Sock
from google import genai
from google.genai import types

MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
PROMPT = ("You are Reachout's test voice assistant. Greet the user briefly, then answer in one or two "
          "short sentences. Match the user's language.")

app = Flask(__name__)
sock = Sock(app)


async def relay(inbox: "asyncio.Queue[bytes | None]", outbox: "queue.Queue[bytes | str | None]") -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"], system_instruction=PROMPT,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text="Greet the user.")]), turn_complete=True)
        outbox.put(json.dumps({"type": "ready"}))

        async def send():
            while (pcm := await inbox.get()) is not None:
                await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

        async def recv():
            while True:  # receive() ends after every turn; keep listening for the next one
                async for msg in session.receive():
                    sc = msg.server_content
                    if not sc:
                        continue
                    if sc.interrupted:
                        outbox.put(json.dumps({"type": "interrupted"}))
                    if sc.input_transcription and sc.input_transcription.text:
                        outbox.put(json.dumps({"type": "you", "text": sc.input_transcription.text}))
                    if sc.output_transcription and sc.output_transcription.text:
                        outbox.put(json.dumps({"type": "gemini", "text": sc.output_transcription.text}))
                    if sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                outbox.put(part.inline_data.data)
                    if sc.turn_complete:
                        outbox.put(json.dumps({"type": "turn_end"}))

        tasks = [asyncio.create_task(send()), asyncio.create_task(recv())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            t.result()


TOKEN = os.getenv("PHONE_TOKEN", "")  # if set, /ws requires ?token=... (the relay is reachable from the internet)


@sock.route("/ws")
def ws_handler(ws):
    if TOKEN and request.args.get("token") != TOKEN:
        ws.close(reason=1008, message="bad token")
        return
    outbox: queue.Queue = queue.Queue()
    loop = asyncio.new_event_loop()
    inbox: asyncio.Queue = asyncio.Queue()  # only touched via loop.call_soon_threadsafe / inside the loop

    def run():
        try:
            loop.run_until_complete(relay(inbox, outbox))
        except Exception as e:  # surface API/model errors in the page
            outbox.put(json.dumps({"type": "error", "text": f"{type(e).__name__}: {e}"}))
        finally:
            outbox.put(None)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        while True:
            while True:  # drain everything Gemini produced
                try:
                    item = outbox.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    return
                ws.send(item)
            data = ws.receive(timeout=0.02)
            if isinstance(data, bytes):
                loop.call_soon_threadsafe(inbox.put_nowait, data)
    except Exception:
        pass
    finally:
        loop.call_soon_threadsafe(inbox.put_nowait, None)


@app.get("/phone")
def phone():
    """Same mock-phone page that is deployed to Vercel, served from here so it needs no other host."""
    with open(os.path.join(os.path.dirname(__file__), "phone", "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gemini Live test</title>
<style>
  body { font: 15px system-ui, sans-serif; background: #0e1013; color: #e6e8eb; margin: 0; }
  main { max-width: 640px; margin: 0 auto; padding: 32px 16px; }
  h1 { font-size: 20px; font-weight: 500; }
  p.hint { color: #8b929c; }
  button { font: inherit; padding: 10px 18px; border-radius: 8px; border: 0; cursor: pointer;
           background: #4de8b2; color: #04130d; font-weight: 600; }
  button.stop { background: #2a2f37; color: #e6e8eb; }
  #status { margin-left: 12px; color: #8b929c; }
  #log { margin-top: 24px; display: flex; flex-direction: column; gap: 8px; }
  .m { padding: 10px 12px; border-radius: 10px; background: #171a1f; max-width: 85%; }
  .you { align-self: flex-end; background: #1d2b26; }
  .err { background: #3a1d1d; color: #ffb4b4; align-self: stretch; max-width: 100%; }
  #meter { height: 6px; width: 160px; background: #171a1f; border-radius: 3px; margin-top: 14px; overflow: hidden; }
  #meter i { display: block; height: 100%; width: 0; background: #4de8b2; }
</style>
<main>
  <h1>Gemini Live test</h1>
  <p class="hint">Use headphones, or Gemini will hear itself. Speak after the greeting; talk over it to interrupt.</p>
  <button id="btn">Start</button><span id="status">idle</span>
  <div id="meter"><i></i></div>
  <div id="log"></div>
</main>
<script>
const btn = document.getElementById('btn'), statusEl = document.getElementById('status'),
      log = document.getElementById('log'), bar = document.querySelector('#meter i');
let ws, ctx, stream, node, playCtx, playAt = 0, sources = [], last = {};

const workletSrc = `class P extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) {
      const out = new Int16Array(ch.length); let peak = 0;
      for (let i = 0; i < ch.length; i++) { const s = Math.max(-1, Math.min(1, ch[i])); out[i] = s * 32767; peak = Math.max(peak, Math.abs(s)); }
      this.port.postMessage({ pcm: out.buffer, peak }, [out.buffer]);
    }
    return true;
  }
}
registerProcessor('p', P);`;

function say(kind, text) {
  // merge streamed transcript chunks into one bubble per speaker turn
  if (last.kind === kind && last.el.isConnected && kind !== 'err') { last.el.textContent += text; return; }
  const el = document.createElement('div');
  el.className = 'm ' + (kind === 'you' ? 'you' : kind === 'err' ? 'err' : '');
  el.textContent = text; log.appendChild(el); last = { kind, el };
}

function play(buf) {
  const pcm = new Int16Array(buf), f = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768;
  const ab = playCtx.createBuffer(1, f.length, 24000); ab.copyToChannel(f, 0);
  const src = playCtx.createBufferSource(); src.buffer = ab; src.connect(playCtx.destination);
  playAt = Math.max(playAt, playCtx.currentTime + 0.05);
  src.start(playAt); playAt += ab.duration;
  sources.push(src); src.onended = () => { sources = sources.filter(s => s !== src); };
}
function flush() { sources.forEach(s => { try { s.stop(); } catch {} }); sources = []; playAt = 0; }

async function start() {
  btn.disabled = true; statusEl.textContent = 'connecting…';
  stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  ctx = new AudioContext({ sampleRate: 16000 });
  playCtx = new AudioContext({ sampleRate: 24000 });
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([workletSrc], { type: 'text/javascript' })));
  node = new AudioWorkletNode(ctx, 'p');
  ctx.createMediaStreamSource(stream).connect(node);

  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.binaryType = 'arraybuffer';
  node.port.onmessage = e => {
    bar.style.width = Math.min(100, e.data.peak * 140) + '%';
    if (ws.readyState === 1 && ready) ws.send(e.data.pcm);
  };
  let ready = false;
  ws.onmessage = e => {
    if (e.data instanceof ArrayBuffer) return play(e.data);
    const m = JSON.parse(e.data);
    if (m.type === 'ready') { ready = true; statusEl.textContent = 'live'; btn.disabled = false; btn.textContent = 'Stop'; btn.className = 'stop'; }
    else if (m.type === 'interrupted') flush();
    else if (m.type === 'turn_end') last = {};
    else if (m.type === 'you' || m.type === 'gemini') say(m.type, m.text);
    else if (m.type === 'error') { say('err', m.text); stop(); }
  };
  ws.onclose = () => stop();
}

function stop() {
  try { ws && ws.close(); } catch {}
  stream && stream.getTracks().forEach(t => t.stop());
  ctx && ctx.close(); playCtx && playCtx.close();
  ws = ctx = playCtx = stream = null; flush(); bar.style.width = 0;
  statusEl.textContent = 'idle'; btn.disabled = false; btn.textContent = 'Start'; btn.className = '';
}

btn.onclick = () => (ws ? stop() : start().catch(e => { say('err', String(e)); stop(); }));
</script>
"""

if __name__ == "__main__":
    if not os.getenv("GEMINI_API_KEY"):
        raise SystemExit("Set GEMINI_API_KEY")
    app.run(port=5000, threaded=True)
