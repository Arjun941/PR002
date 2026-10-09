"use client";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { PREFILL_KEY, type AssistantReply, type ChatGPTStatus, type EventDetails } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useShell } from "@/components/Shell";

interface Turn { role: "user" | "assistant"; content: string }
const GREETING: Turn = {
  role: "assistant",
  content: "Tell me about the event, e.g. “Parent-teacher meeting on 18 October at 10 am in the main hall, in Hindi and English.” I'll fill in the campaign form for you to review.",
};

export function AssistantChat() {
  const router = useRouter();
  const { toast } = useShell();
  const [turns, setTurns] = useState<Turn[]>([GREETING]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState<AssistantReply | null>(null);
  const [note, setNote] = useState("");
  const log = useRef<HTMLDivElement>(null);
  const { data: gpt, retry } = useData(() => api<ChatGPTStatus>("/auth/chatgpt"), []);
  const [linking, setLinking] = useState(false);

  // Back from the ChatGPT sign-in: say how it went once, then clean the address bar.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const result = q.get("chatgpt");
    if (!result) return;
    if (result === "connected") toast("ChatGPT connected");
    else toast(q.get("reason") || "ChatGPT sign-in failed", "error");
    window.history.replaceState(null, "", window.location.pathname);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const connect = async () => {
    setLinking(true);
    try {
      const r = await post<{ url: string }>("/auth/chatgpt/start", { next: "/overview" });
      window.location.href = r.url;
    } catch (e) { toast((e as Error).message, "error"); setLinking(false); }
  };

  const disconnect = async () => {
    setLinking(true);
    try { await post("/auth/chatgpt/disconnect"); retry(); }
    catch (e) { toast((e as Error).message, "error"); }
    finally { setLinking(false); }
  };

  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }); }, [turns, busy]);

  const open = (r: AssistantReply & { event: EventDetails }) => {
    try { sessionStorage.setItem(PREFILL_KEY, JSON.stringify({ event: r.event, languages: r.languages })); } catch { /* private mode: the form just starts empty */ }
    router.push("/campaigns/new");
  };

  const send = async () => {
    const content = text.trim();
    if (!content || busy) return;
    const next = [...turns, { role: "user" as const, content }];
    setTurns(next); setText(""); setBusy(true); setReady(null); setNote("");
    try {
      // The greeting is UI only; the server sees the real conversation.
      const r = await post<AssistantReply>("/assistant/chat", { messages: next.slice(1) });
      setTurns(p => [...p, { role: "assistant", content: r.reply }]);
      setNote(`Answered by ${r.provider}`);
      if (r.ready && r.event) setReady(r);
    } catch (e) {
      toast((e as Error).message, "error");
      setTurns(next.slice(0, -1)); setText(content); // let them retry without retyping
    } finally { setBusy(false); }
  };

  return (
    <section className="card chat" aria-label="Campaign assistant">
      <div className="card-head"><h2>Describe your event</h2>{note && <span className="muted" style={{ fontSize: 12 }}>{note}</span>}</div>
      {gpt && (
        <div className="chat-conn">
          {gpt.connected ? (
            <>
              <span>Using the ChatGPT plan of <b>{gpt.email ?? "the connected account"}</b>. If it runs out, other configured models take over.</span>
              <button className="btn sm" disabled={linking} onClick={() => void disconnect()}>Disconnect</button>
            </>
          ) : (
            <>
              <span>Connect ChatGPT so this assistant runs on your plan (Plus or Pro). Opens OpenAI to sign in; use this dashboard on the machine that runs the server.</span>
              <button className="btn primary sm" disabled={linking} onClick={() => void connect()}>
                {linking && <span className="spinner" />}Connect ChatGPT
              </button>
            </>
          )}
        </div>
      )}
      <div className="chat-log" ref={log} aria-live="polite">
        {turns.map((t, i) => <p key={i} className={`bubble ${t.role}`}>{t.content}</p>)}
        {busy && <p className="bubble assistant"><span className="spinner" /> Thinking</p>}
      </div>
      {ready?.event && (
        <div className="chat-ready">
          <dl className="facts">
            <div><dt>Type</dt><dd>{ready.event.kind}</dd></div>
            <div><dt>About</dt><dd>{ready.event.title}</dd></div>
            {[ready.event.date, ready.event.time].filter(Boolean).length > 0 &&
              <div><dt>When</dt><dd>{[ready.event.date, ready.event.time].filter(Boolean).join(" at ")}</dd></div>}
            {ready.event.venue && <div><dt>Where</dt><dd>{ready.event.venue}</dd></div>}
            <div><dt>Languages</dt><dd>{ready.languages.join(", ")}</dd></div>
          </dl>
          <button className="btn primary" onClick={() => open(ready as AssistantReply & { event: EventDetails })}>
            <Icon name="wand" />Open in campaign builder
          </button>
        </div>
      )}
      <form className="chat-input" onSubmit={e => { e.preventDefault(); void send(); }}>
        <input className="input" value={text} maxLength={2000} onChange={e => setText(e.target.value)}
          placeholder="Describe the event, or answer my question…" aria-label="Message" disabled={busy} />
        <button className="btn primary" type="submit" disabled={busy || !text.trim()}>Send</button>
      </form>
    </section>
  );
}
