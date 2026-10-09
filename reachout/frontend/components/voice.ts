"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

/** Languages people can speak to the assistant in (BCP-47 tags, named in their own script). */
export const SPEECH_LANGS: [tag: string, name: string][] = [
  ["en-IN", "English"], ["hi-IN", "हिन्दी"], ["mr-IN", "मराठी"], ["ta-IN", "தமிழ்"], ["kn-IN", "ಕನ್ನಡ"], ["ml-IN", "മലയാളം"],
];
export const AUTO: [tag: string, name: string] = ["auto", "Auto-detect"];  // server recognition only
const LANG_KEY = "reachout.speechLang";
const FIRST_WORDS_MS = 10_000;  // nothing said at all by then: give up
const END_SILENCE_MS = 3500;    // after speech, this much quiet means they have finished (sentences pause ~1-2 s)
const MAX_RECORD_MS = 120_000;
const SPEECH_RMS = 0.02;        // mic level (0..1) that counts as speaking, for the recorder's end-of-speech check

export interface ServerSTT { key: string; label: string; region: string; sends: string }
type State = "idle" | "listening" | "transcribing";

// The Web Speech API is not in TypeScript's DOM types; this is the part used here.
interface Recognition {
  lang: string; interimResults: boolean; continuous: boolean;
  start(): void; stop(): void;
  onresult: ((e: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
}
const browserRecognizer = (): (new () => Recognition) | undefined => {
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition || w.webkitSpeechRecognition) as (new () => Recognition) | undefined;
};
const browserLang = () => SPEECH_LANGS.find(([t]) => t.split("-")[0] === navigator.language.split("-")[0])?.[0] ?? "en-IN";

/** The remembered speech language (default: auto-detect). Read after mount so server and client HTML match. */
export function useSpeechLang(): [string, (tag: string) => void] {
  const [lang, setLang] = useState(AUTO[0]);
  useEffect(() => {
    try {
      const saved = localStorage.getItem(LANG_KEY);
      if (saved && [AUTO, ...SPEECH_LANGS].some(([t]) => t === saved)) setLang(saved);
    } catch { /* storage blocked: keep the default */ }
  }, []);
  return [lang, (tag: string) => { setLang(tag); try { localStorage.setItem(LANG_KEY, tag); } catch { /* not remembered */ } }];
}

/**
 * Speech -> text. A configured speech service on the server (Sarvam or ElevenLabs) is preferred: it is
 * more accurate for Indian languages and can detect the language. Without one, Chrome and Edge recognise
 * speech themselves (free; audio goes to Google or Microsoft). Either way listening continues until the
 * person has clearly finished (END_SILENCE_MS of quiet) or taps stop; the text is handed back for review,
 * never sent on its own.
 */
export function useVoice({ lang, server, onText, onError }: {
  lang: string; server: ServerSTT[]; onText: (text: string, where: string) => void; onError: (msg: string) => void;
}) {
  const [state, setState] = useState<State>("idle");
  const [interim, setInterim] = useState("");
  const [level, setLevel] = useState(0);
  // Browser support is only known in the browser: decided after mount, so the first render matches the server's.
  const [browserOk, setBrowserOk] = useState(false);
  useEffect(() => { setBrowserOk(!!browserRecognizer()); }, []);
  const stopRef = useRef<() => void>(() => {});
  const cb = useRef({ onText, onError });
  cb.current = { onText, onError };
  useEffect(() => () => stopRef.current(), []);

  const mode = server.length ? "server" : browserOk ? "browser" : null;

  const listenInBrowser = () => {
    const SR = browserRecognizer()!;
    let final = "", heard = false, stopped = false, failed = false, timer = 0, r: Recognition;
    const quietFor = (ms: number) => { clearTimeout(timer); timer = window.setTimeout(() => { stopped = true; r.stop(); }, ms); };
    const run = () => {
      r = new SR();
      r.lang = lang === AUTO[0] ? browserLang() : lang;
      r.interimResults = true; r.continuous = true;
      r.onresult = e => {
        let partial = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const res = e.results[i];
          if (res.isFinal) final += res[0].transcript.trim() + " "; else partial += res[0].transcript;
        }
        heard = true;
        setInterim((final + partial).trim());
        quietFor(END_SILENCE_MS);
      };
      r.onerror = e => {
        if (e.error === "no-speech" && !stopped) return;  // a pause, not the end: onend restarts
        failed = true; stopped = true;
        if (e.error === "not-allowed") cb.current.onError("Allow microphone access to speak to the assistant.");
        else if (e.error !== "aborted") {
          setBrowserOk(false);  // e.g. "network": this browser's speech service is not usable here
          cb.current.onError(`Your browser couldn't recognise speech (${e.error}). Type instead, or ask for a speech service to be set up on the server.`);
        }
      };
      // Browsers end recognition on their own after a pause, even in continuous mode: carry on until the
      // person has finished or tapped stop, keeping what was already recognised.
      r.onend = () => {
        if (!stopped) { run(); return; }
        clearTimeout(timer); setState("idle"); setInterim("");
        if (!failed && final.trim()) cb.current.onText(final.trim(), `Recognised by your browser (${/Edg\//.test(navigator.userAgent) ? "Microsoft" : "Google"}).`);
        else if (!failed && !heard) cb.current.onError("I didn't hear anything. Tap the microphone and speak.");
      };
      r.start();
    };
    stopRef.current = () => { stopped = true; r?.stop(); };
    run(); setState("listening"); quietFor(FIRST_WORDS_MS);
  };

  const recordForServer = async () => {
    let stream: MediaStream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
    catch { cb.current.onError("Allow microphone access to speak to the assistant."); return; }
    const rec = new MediaRecorder(stream), chunks: Blob[] = [];
    // End-of-speech check on the mic level: stop after END_SILENCE_MS of quiet once they have spoken.
    const ctx = new AudioContext(), meter = ctx.createAnalyser(), buf = new Float32Array(1024);
    meter.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(meter);
    const began = performance.now();
    let spoke = false, lastLoud = began;
    const poll = window.setInterval(() => {
      meter.getFloatTimeDomainData(buf);
      const rms = Math.sqrt(buf.reduce((a, v) => a + v * v, 0) / buf.length);
      setLevel(Math.min(1, rms * 12));
      const now = performance.now();
      if (rms > SPEECH_RMS) { spoke = true; lastLoud = now; }
      if ((spoke && now - lastLoud > END_SILENCE_MS) || (!spoke && now - began > FIRST_WORDS_MS) || now - began > MAX_RECORD_MS) stop();
    }, 100);
    const stop = () => { if (rec.state === "recording") rec.stop(); };
    rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      clearInterval(poll); setLevel(0); void ctx.close(); stream.getTracks().forEach(t => t.stop());
      if (!spoke) { setState("idle"); cb.current.onError("I didn't hear anything. Tap the microphone and speak."); return; }
      setState("transcribing");
      try {
        const r = await api<{ text: string; provider: string; sends: string }>(`/assistant/transcribe?lang=${encodeURIComponent(lang)}`,
          { method: "POST", headers: { "Content-Type": "audio/wav" }, body: await toWav(new Blob(chunks)) });
        if (r.text) cb.current.onText(r.text, `Recognised by ${r.provider}. ${r.sends}.`);
        else cb.current.onError("I didn't catch any words. Try again a little closer to the microphone.");
      } catch (e) { cb.current.onError((e as Error).message); }
      finally { setState("idle"); }
    };
    stopRef.current = stop;
    rec.start(); setState("listening");
  };

  const toggle = () => {
    if (state === "listening") { stopRef.current(); return; }
    if (state !== "idle") return;
    if (mode === "server") void recordForServer();
    else if (mode === "browser") listenInBrowser();
    else onError("Voice input needs Chrome or Edge here, or SARVAM_API_KEY / ELEVENLABS_API_KEY on the server.");
  };

  return { state, interim, level, toggle, mode };
}

/** Any recorded audio -> 16 kHz mono 16-bit WAV, which every speech-to-text service accepts. */
async function toWav(blob: Blob): Promise<Blob> {
  const ctx = new AudioContext();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  void ctx.close();
  const rate = 16000;
  const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
  const src = off.createBufferSource();
  src.buffer = decoded; src.connect(off.destination); src.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const view = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const tag = (at: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i)); };
  tag(0, "RIFF"); view.setUint32(4, 36 + pcm.length * 2, true); tag(8, "WAVE");
  tag(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  tag(36, "data"); view.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i])) * 0x7fff, true);
  return new Blob([view], { type: "audio/wav" });
}
