"use client";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { KIND } from "@/lib/format";
import { PREFILL_KEY, type AssistantReply, type ChatGPTStatus, type Draft, type Prefill, type ProviderKey } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useShell } from "@/components/Shell";
import { AUTO, SPEECH_LANGS, useSpeechLang, useVoice, type ServerSTT } from "@/components/voice";

interface Turn { role: "user" | "assistant"; content: string; voice?: boolean }
type Ready = AssistantReply & { event: NonNullable<AssistantReply["event"]> };
const GREETING: Turn = {
  role: "assistant",
  content: "Tell me about the event, e.g. “Greenfield School parent-teacher meeting on 18 October at 10 am in the main hall, in Hindi and English.” I'll ask for anything missing, then write the call scripts in your languages.",
};

const asProvider = (k: string): ProviderKey => (k === "elevenlabs" ? "elevenlabs" : "gemini");

/** Floating assistant bar at the bottom of every page (the builder has its own form, so it hides there). */
export function AssistantChat() {
  const router = useRouter();
  const pathname = usePathname();
  const { toast } = useShell();
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([GREETING]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<AssistantReply | null>(null);
  const [scripts, setScripts] = useState<{ draft: Draft; warnings: string[] } | null>(null);
  const [drafting, setDrafting] = useState(false);
  const log = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const { data: voiceOpts } = useData(() => api<{ server: ServerSTT[] }>("/assistant/voice"), []);
  const { data: gpt, retry: reloadGpt } = useData(() => api<ChatGPTStatus>("/auth/chatgpt"), []);
  const [linking, setLinking] = useState(false);
  const [speechLang, setSpeechLang] = useSpeechLang();
  const [voiceNote, setVoiceNote] = useState("");
  const voiced = useRef(false);
  // Speaking is another way to type: the transcript lands in the box to check and correct, then Send
  // It is never sent on its own, so a misheard word can be fixed first.
  const voice = useVoice({
    lang: speechLang, server: voiceOpts?.server ?? [],
    onText: (t, where) => {
      setText(prev => (prev.trim() ? `${prev.trim()} ${t}` : t));
      voiced.current = true;
      setVoiceNote(`${where} Check the text, fix anything misheard, then press Send.`);
      setOpen(true);
      requestAnimationFrame(() => input.current?.focus());
    },
    onError: msg => toast(msg, "error"),
  });
  const listening = voice.state === "listening";

  // Back from the ChatGPT sign-in: say how it went once, then clean the address bar.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const result = q.get("chatgpt");
    if (!result) return;
    if (result === "connected") { toast("ChatGPT connected"); setOpen(true); }
    else toast(q.get("reason") || "ChatGPT sign-in failed", "error");
    window.history.replaceState(null, "", window.location.pathname);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }); }, [turns, busy, drafting, open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !document.querySelector(".overlay")) setOpen(false); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (pathname.startsWith("/campaigns/new")) return null;

  const ready = last?.ready && last.event ? last as Ready : null;
  const say = (content: string) => setTurns(p => [...p, { role: "assistant", content }]);

  // Everything required is known: the same AI writes the scripts in the chosen languages (one call per campaign).
  const writeScripts = async (r: Ready) => {
    setDrafting(true);
    try {
      const res = await post<{ draft: Draft; warnings: string[] }>("/builder/draft",
        { event: r.event, languages: r.languages, provider: asProvider(r.provider_key), mode: "hybrid" });
      setScripts(res);
      say(`Scripts written in ${r.language_names.join(", ")}. Open the builder to add contacts and review every line before anyone is called.`);
    } catch (e) {
      toast((e as Error).message, "error");
      say("I couldn't write the scripts just now. You can still open the builder and draft them there.");
    } finally { setDrafting(false); }
  };

  const send = async () => {
    const content = text.trim();
    if (!content || busy || drafting) return;
    const next: Turn[] = [...turns, { role: "user", content, voice: voiced.current }];
    voiced.current = false;
    setTurns(next); setText(""); setVoiceNote(""); setBusy(true); setOpen(true); setScripts(null);
    try {
      // The greeting is UI only; the server sees the real conversation (text only, typed or spoken).
      const r = await post<AssistantReply>("/assistant/chat", { messages: next.slice(1).map(({ role, content }) => ({ role, content })) });
      setTurns(p => [...p, { role: "assistant", content: r.reply }]);
      setLast(r);
      if (r.ready && r.event) void writeScripts(r as Ready);
    } catch (e) {
      toast((e as Error).message, "error");
      setTurns(next.slice(0, -1)); setText(content); // let them retry without retyping (or re-speaking)
    } finally { setBusy(false); input.current?.focus(); }
  };

  const openBuilder = (r: Ready) => {
    const prefill: Prefill = { event: r.event, languages: r.languages, provider: asProvider(r.provider_key),
      draft: scripts?.draft, warnings: scripts?.warnings };
    try { sessionStorage.setItem(PREFILL_KEY, JSON.stringify(prefill)); } catch { /* private mode: the form just starts empty */ }
    setOpen(false); setTurns([GREETING]); setLast(null); setScripts(null);
    router.push("/campaigns/new");
  };

  const connectGpt = async () => {
    setLinking(true);
    try {
      const r = await post<{ url: string }>("/auth/chatgpt/start", { next: "/overview" });
      window.location.href = r.url;
    } catch (e) { toast((e as Error).message, "error"); setLinking(false); }
  };

  const disconnectGpt = async () => {
    setLinking(true);
    try { await post("/auth/chatgpt/disconnect"); reloadGpt(); }
    catch (e) { toast((e as Error).message, "error"); }
    finally { setLinking(false); }
  };

  const reset = () => { setTurns([GREETING]); setLast(null); setScripts(null); setText(""); input.current?.focus(); };

  return (
    <div className={`dock${open ? " open" : ""}`}>
      {open && (
        <section className="dock-panel" aria-label="Campaign assistant">
          <div className="dock-head">
            <h2><Icon name="wand" />Campaign assistant</h2>
            <div className="dock-tools">
              {last && <span className="muted">via {last.provider}</span>}
              {turns.length > 1 && <button className="btn sm ghost" onClick={reset} disabled={busy || drafting}>New chat</button>}
              <button className="btn sm ghost" aria-label="Minimise assistant" onClick={() => setOpen(false)}><Icon name="minus" size={14} /></button>
            </div>
          </div>
          {gpt && (
            <div className="chat-conn">
              {gpt.connected ? (
                <>
                  <span>Using the ChatGPT plan of <b>{gpt.email ?? "the connected account"}</b>. If it runs out, our own model takes over.</span>
                  <button className="btn sm" type="button" disabled={linking} onClick={() => void disconnectGpt()}>Disconnect</button>
                </>
              ) : (
                <>
                  <span>Connect ChatGPT so the assistant and script drafts run on your plan (Plus or Pro). Opens OpenAI to sign in; use this dashboard on the machine that runs the server.</span>
                  <button className="btn primary sm" type="button" disabled={linking} onClick={() => void connectGpt()}>
                    {linking && <span className="spinner" />}Connect ChatGPT
                  </button>
                </>
              )}
            </div>
          )}
          {voiceNote && <div className="voice-note"><Icon name="mic" size={14} />{voiceNote}</div>}
          <div className="chat-log" ref={log} aria-live="polite">
            {turns.map((t, i) => (
              <p key={i} className={`bubble ${t.role}`}>{t.voice && <span title="Spoken"><Icon name="mic" size={13} /></span>}{t.content}</p>
            ))}
            {busy && <p className="bubble assistant"><span className="spinner" /> Thinking</p>}
            {drafting && ready && <p className="bubble assistant"><span className="spinner" /> Writing scripts in {ready.language_names.join(", ")}. This can take up to a minute.</p>}
          </div>
          {last && last.missing.length > 0 && (
            <div className="missing"><span>Still needed:</span>{last.missing.map(m => <span key={m.key} className="chip">{m.label}</span>)}</div>
          )}
          {ready && !drafting && (
            <div className="chat-ready">
              <dl className="facts">
                <div><dt>Type</dt><dd>{KIND[ready.event.kind] || ready.event.kind}
                  {ready.suggested?.includes("kind") && <em className="sugg">AI chose</em>}</dd></div>
                <div><dt>About</dt><dd>{ready.event.title}{ready.event.org && ` · ${ready.event.org}`}
                  {ready.suggested?.includes("title") && <em className="sugg">AI wording</em>}</dd></div>
                <div><dt>When</dt><dd>{[ready.event.date, ready.event.time].filter(Boolean).join(" at ")}</dd></div>
                {ready.event.venue && <div><dt>Where</dt><dd>{ready.event.venue}</dd></div>}
                {ready.event.details && <div><dt>Details</dt><dd>{ready.event.details}
                  {ready.suggested?.includes("details") && <em className="sugg">AI wording</em>}</dd></div>}
                {scripts && <div><dt>Questions</dt><dd>{scripts.draft.questions?.length
                  ? scripts.draft.questions.map(q => q.label).join(", ") : "None needed"}</dd></div>}
                <div><dt>Scripts</dt><dd>{scripts
                  ? ready.languages.map(l => `${ready.language_names[ready.languages.indexOf(l)]}${scripts.draft.scripts[l]?.placeholder ? " (English placeholder)" : ""}`).join(", ")
                  : "Not written yet"}</dd></div>
              </dl>
              <button className="btn primary" onClick={() => openBuilder(ready)}><Icon name="wand" />Open in campaign builder</button>
            </div>
          )}
        </section>
      )}
      <form className="dock-bar" onSubmit={e => { e.preventDefault(); void send(); }}>
        <button type="button" className="dock-toggle" aria-label={open ? "Minimise assistant" : "Open assistant"} aria-expanded={open}
          onClick={() => setOpen(o => !o)}><Icon name="wand" /></button>
        <input ref={input} className="input" value={listening ? voice.interim : text} maxLength={2000} readOnly={listening}
          onChange={e => setText(e.target.value)} onFocus={() => setOpen(true)} aria-label="Message the campaign assistant"
          disabled={busy || drafting || voice.state === "transcribing"}
          placeholder={listening ? (voice.mode === "server" ? "Listening… tap stop when you have finished" : "Listening…")
            : voice.state === "transcribing" ? "Turning your voice into text…"
            : last?.missing.length ? `Tell me the ${last.missing.map(m => m.label).join(", ")}…` : "Describe your event, or tap the mic and speak…"} />
        <select className="dock-lang" aria-label="Language you speak in" title="Language you speak in" value={speechLang}
          disabled={listening} onChange={e => setSpeechLang(e.target.value)}>
          <option value={AUTO[0]}>{voice.mode === "browser" ? "Auto (browser language)" : AUTO[1]}</option>
          {SPEECH_LANGS.map(([t, n]) => <option key={t} value={t}>{n}</option>)}
        </select>
        <button type="button" className={`mic-btn${listening ? " on" : ""}`} aria-pressed={listening}
          aria-label={listening ? "Stop listening" : "Speak instead of typing"}
          title={!voice.mode ? "Voice input needs Chrome or Edge, or a speech service on the server" : listening ? "Stop listening" : "Speak instead of typing"}
          style={{ "--lvl": voice.level } as React.CSSProperties}
          disabled={busy || drafting || voice.state === "transcribing"} onClick={() => { setOpen(true); voice.toggle(); }}>
          {voice.state === "transcribing" ? <span className="spinner" /> : <Icon name={listening ? "stop" : "mic"} />}
        </button>
        <button className="btn primary" type="submit" disabled={busy || drafting || listening || !text.trim()}>Send</button>
      </form>
    </div>
  );
}
