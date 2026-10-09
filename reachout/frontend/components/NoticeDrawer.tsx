"use client";
import { useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import type { AudienceInfo, NoticeKind } from "@/lib/types";
import { Icon } from "@/components/Icon";
import { useShell } from "@/components/Shell";
import { Notice } from "@/components/ui";

interface Turn { role: "user" | "assistant"; content: string }
interface Draft { reply: string; texts: Record<string, string>; missing: string[]; warnings: string[] }

const QUICK: Record<NoticeKind, string[]> = {
  reminder: ["Remind them about the event", "Remind them to arrive 15 minutes early", "Remind them to bring their ID"],
  update: ["The time has changed", "The venue has changed", "The date has changed", "The event is cancelled"],
};
const DEFAULT_AUDIENCE: Record<NoticeKind, string[]> = {
  reminder: ["confirmed", "rescheduled"], update: ["confirmed", "rescheduled", "pending", "voicemail", "no_answer"],
};
const GREETING: Record<NoticeKind, string> = {
  reminder: "Tell me what the reminder should say, or pick a quick start. I'll write it in every language of this campaign using the event's details.",
  update: "Tell me what has changed (the new time, the venue, a cancellation...). I'll write the update in every language using only what you tell me.",
};

/** Local date-time input value for "now plus ten minutes". */
const soon = () => {
  const d = new Date(Date.now() + 10 * 60_000);
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
};

/** A dropdown of check boxes (who gets the message), each with how many people it has. */
function AudiencePicker({ info, pick, setPick }: { info: AudienceInfo | null; pick: string[]; setPick: (v: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);
  const names = pick.map(k => info?.labels[k] ?? k);
  const summary = !names.length ? "Choose who gets it" : names.length === 1 ? names[0] : names.length === 2 && names.join(", ").length <= 26 ? names.join(", ") : `${names.length} groups selected`;
  return (
    <div className="aud" ref={box} onKeyDown={e => { if (e.key === "Escape" && open) { e.stopPropagation(); setOpen(false); } }}>
      <button type="button" className="select aud-btn" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <span className={`aud-sum${names.length ? "" : " muted"}`}>{summary}</span><Icon name="chevron" size={14} />
      </button>
      {open && info && (
        <div className="aud-pop" role="listbox" aria-multiselectable="true">
          {Object.entries(info.labels).map(([k, label]) => {
            const n = info.counts[k] ?? 0;
            return (
              <label key={k} className={`aud-item${n || pick.includes(k) ? "" : " off"}`}>
                <input type="checkbox" checked={pick.includes(k)} disabled={!n && !pick.includes(k)}
                  onChange={() => setPick(pick.includes(k) ? pick.filter(x => x !== k) : [...pick, k])} />
                <span>{label}</span><span className="n">{n}</span>
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** The popup for reminders and updates: write the message (assistant or by hand), choose who gets it by outcome, send now or later. */
export function NoticeDrawer({ cid, languages, onClose, onSent }: {
  cid: string; languages: { code: string; name: string }[]; onClose: () => void; onSent: () => void;
}) {
  const { toast } = useShell();
  const [kind, setKind] = useState<NoticeKind>("reminder");
  const [mode, setMode] = useState<"assistant" | "manual">("assistant");
  const [turns, setTurns] = useState<Turn[]>([{ role: "assistant", content: GREETING.reminder }]);
  const [text, setText] = useState("");
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [tab, setTab] = useState(languages[0]?.code ?? "en");
  const [manualLang, setManualLang] = useState(languages[0]?.code ?? "en");
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<AudienceInfo | null>(null);
  const [pick, setPick] = useState<string[]>(DEFAULT_AUDIENCE.reminder);
  const [people, setPeople] = useState(0);
  const [when, setWhen] = useState<"now" | "later">("now");
  const [at, setAt] = useState(soon());
  const log = useRef<HTMLDivElement>(null);

  // How many different people the chosen groups add up to (someone can be in two groups).
  useEffect(() => {
    let live = true;
    api<AudienceInfo>(`/campaigns/${cid}/notices/audience?outcomes=${encodeURIComponent(pick.join(","))}`)
      .then(r => { if (live) { setInfo(r); setPeople(r.selected); } }).catch(() => { /* keep what we had */ });
    return () => { live = false; };
  }, [cid, pick]);
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }); }, [turns, busy]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const changeKind = (k: NoticeKind) => {
    if (k === kind) return;
    setKind(k); setTexts({}); setPick(DEFAULT_AUDIENCE[k]);
    setTurns([{ role: "assistant", content: GREETING[k] }]);
  };
  const langName = (c: string) => languages.find(l => l.code === c)?.name ?? c;
  const say = (content: string) => setTurns(p => [...p, { role: "assistant", content }]);

  const ask = async (content: string) => {
    const msg = content.trim();
    if (!msg || busy) return;
    const next: Turn[] = [...turns, { role: "user", content: msg }];
    setTurns(next); setText(""); setBusy(true);
    try {
      const r = await post<Draft>(`/campaigns/${cid}/notices/draft`, { kind, messages: next.slice(1) });
      say(r.reply || (Object.keys(r.texts).length ? "Here is the message." : "I need a little more detail."));
      if (Object.keys(r.texts).length) setTexts(r.texts);
    } catch (e) {
      toast((e as Error).message, "error");
      setTurns(next.slice(0, -1)); setText(msg);
    } finally { setBusy(false); }
  };

  const translate = async () => {
    const own = (texts[manualLang] ?? "").trim();
    if (!own || busy) return;
    setBusy(true);
    try {
      const r = await post<Draft>(`/campaigns/${cid}/notices/draft`, { kind, messages: [], manual: { lang: manualLang, text: own } });
      setTexts(p => ({ ...p, ...r.texts, [manualLang]: own }));
      toast(`Translated into ${languages.length - 1} other language${languages.length > 2 ? "s" : ""}. Check each one.`);
    } catch (e) { toast((e as Error).message, "error"); }
    finally { setBusy(false); }
  };

  const complete = languages.every(l => (texts[l.code] ?? "").trim());
  const timeOk = when === "now" || (!!at && new Date(at).getTime() > Date.now() - 60_000);
  const canSend = complete && pick.length > 0 && people > 0 && timeOk && !busy;
  const hasTexts = Object.values(texts).some(t => t.trim());
  const tabLangs = mode === "manual" ? languages.filter(l => l.code !== manualLang) : languages;  // manual: the language you type in is the box above
  const curTab = tabLangs.some(l => l.code === tab) ? tab : tabLangs[0]?.code ?? "";

  const send = async () => {
    setBusy(true);
    try {
      await post(`/campaigns/${cid}/notices`, { kind, texts, outcomes: pick, send_at: when === "later" ? new Date(at).toISOString() : null });
      toast(when === "now" ? "Sending now. People are called one at a time." : `Scheduled for ${new Date(at).toLocaleString("en-IN")}`);
      onSent(); onClose();
    } catch (e) { toast((e as Error).message, "error"); setBusy(false); }
  };

  const summary = !complete ? "Write the message in every language to continue"
    : !pick.length ? "Choose who gets it"
    : people === 0 ? "Nobody matches right now"
    : `${people} ${people === 1 ? "person" : "people"} · ${when === "now" ? "sent now" : `scheduled ${new Date(at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}`}`;

  return (
    <div className="overlay open" onMouseDown={e => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="modal nt-modal" role="dialog" aria-modal="true" aria-label="Reminder or update">
        <div className="nt-head">
          <h2><Icon name="bell" />Remind or update</h2>
          <button className="btn sm ghost" aria-label="Close" onClick={onClose} disabled={busy}><Icon name="x" size={14} /></button>
        </div>

        <div className="nt-body">
          <section className="nt-section">
            <h3>1 · What to send</h3>
            <div className="nt-grid">
              <div className="nt-field"><label htmlFor="nt-kind">Type</label>
                <select id="nt-kind" className="select" value={kind} onChange={e => changeKind(e.target.value as NoticeKind)}>
                  <option value="reminder">Reminder</option>
                  <option value="update">Update (something changed)</option>
                </select></div>
              <div className="nt-field"><label htmlFor="nt-mode">Who writes the message</label>
                <select id="nt-mode" className="select" value={mode} onChange={e => setMode(e.target.value as "assistant" | "manual")}>
                  <option value="assistant">The assistant writes it for me</option>
                  <option value="manual">I write it myself</option>
                </select></div>
              {mode === "assistant" ? (
                <div className="nt-field"><label htmlFor="nt-quick">Quick start</label>
                  <select id="nt-quick" className="select" value="" disabled={busy} onChange={e => { if (e.target.value) void ask(e.target.value); }}>
                    <option value="">Pick a starting point…</option>
                    {QUICK[kind].map(q => <option key={q} value={q}>{q}</option>)}
                  </select></div>
              ) : (
                <div className="nt-field"><label htmlFor="nt-lang">I am writing in</label>
                  <select id="nt-lang" className="select" value={manualLang} onChange={e => setManualLang(e.target.value)}>
                    {languages.map(l => <option key={l.code} value={l.code}>{l.name}</option>)}
                  </select></div>
              )}
            </div>
          </section>

          <section className="nt-section">
            <h3>2 · The message</h3>
            {mode === "assistant" ? (
              <>
                <div className="nt-log" ref={log} aria-live="polite">
                  {turns.map((t, i) => <p key={i} className={`bubble ${t.role}`}>{t.content}</p>)}
                  {busy && <p className="bubble assistant"><span className="spinner" /> Writing</p>}
                </div>
                <form className="nt-row" onSubmit={e => { e.preventDefault(); void ask(text); }}>
                  <input className="input" value={text} maxLength={1500} disabled={busy} aria-label="Message to the assistant"
                    placeholder={hasTexts ? "Ask for a change, e.g. make it shorter" : kind === "update" ? "What has changed?" : "What should the reminder say?"}
                    onChange={e => setText(e.target.value)} />
                  <button className="btn primary" type="submit" disabled={busy || !text.trim()}>Send</button>
                </form>
              </>
            ) : (
              <div className="nt-field">
                <textarea className="input" rows={3} maxLength={600} lang={manualLang} aria-label={`Your message in ${langName(manualLang)}`}
                  value={texts[manualLang] ?? ""} onChange={e => setTexts(p => ({ ...p, [manualLang]: e.target.value }))}
                  placeholder={`e.g. Hello {name}, ${kind === "update" ? "the venue has moved to Hall B." : "the event is tomorrow at 9 am."}`} />
                <div className="nt-row">
                  <span className="hint" style={{ flex: 1 }}>{"{name}"} is replaced with each person&apos;s name. Write it as it should be spoken.</span>
                  <button className="btn" disabled={busy || !(texts[manualLang] ?? "").trim() || languages.length < 2} onClick={() => void translate()}>
                    {busy ? <span className="spinner" /> : <Icon name="wand" size={14} />}Translate to the other languages
                  </button>
                </div>
              </div>
            )}

            {hasTexts && (
              <div className="nt-texts">
                {tabLangs.length > 0 && (
                  <>
                    <div className="tabs" role="tablist">
                      {tabLangs.map(l => (
                        <button key={l.code} role="tab" aria-selected={curTab === l.code} className={curTab === l.code ? "on" : ""} onClick={() => setTab(l.code)}>
                          {l.name}{!(texts[l.code] ?? "").trim() && <i className="dot" title="Missing" />}
                        </button>
                      ))}
                    </div>
                    <textarea className="input" rows={3} maxLength={600} lang={curTab} aria-label={`Message in ${langName(curTab)}`} value={texts[curTab] ?? ""}
                      onChange={e => setTexts(p => ({ ...p, [curTab]: e.target.value }))} />
                  </>
                )}
                {!complete && <Notice>Each person hears the message in their own language. Still empty: {languages.filter(l => !(texts[l.code] ?? "").trim()).map(l => l.name).join(", ")}.{mode === "manual" ? " Use Translate above, or type them." : ""}</Notice>}
              </div>
            )}
          </section>

          <section className="nt-section">
            <h3>3 · Who gets it, and when</h3>
            <div className="nt-grid">
              <div className="nt-field"><span className="label">Who gets it (by their outcome)</span>
                <AudiencePicker info={info} pick={pick} setPick={setPick} /></div>
              <div className="nt-field"><label htmlFor="nt-when">When</label>
                <select id="nt-when" className="select" value={when} onChange={e => setWhen(e.target.value as "now" | "later")}>
                  <option value="now">Send now</option>
                  <option value="later">Schedule for later</option>
                </select></div>
              {when === "later" && (
                <div className="nt-field"><label htmlFor="nt-at">Send at</label>
                  <input id="nt-at" className="input" type="datetime-local" value={at} onChange={e => setAt(e.target.value)} /></div>
              )}
            </div>
            <p className="hint" style={{ marginTop: 10 }}>
              The group is picked again when it is time, so a scheduled message reaches whoever matches then. The phone page must be open. People are called one at a
              time; anyone who does not answer is tried once more after 30 minutes.
            </p>
          </section>
        </div>

        <div className="nt-foot">
          <span className="sum">{summary}</span>
          <div className="acts">
            <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="btn primary" disabled={!canSend} onClick={() => void send()}>
              {busy ? <span className="spinner" /> : <Icon name="phone" size={14} />}{when === "now" ? "Send now" : "Schedule"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
