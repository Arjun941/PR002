"use client";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { fmt, KIND } from "@/lib/format";
import {
  PREFILL_KEY, SCRIPT_FIELDS, questionText, type Prefill, type Question, type BuilderOptions, type ContactsCheck, type Draft, type Estimate, type EventDetails,
  type Script, type TextProvider, type VoiceProvider,
} from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs, useShell } from "@/components/Shell";
import { QuestionsEditor } from "@/components/QuestionsEditor";
import { ErrorView, HandlingCard, Notice, PageHead, Skeleton } from "@/components/ui";

const STEPS = ["Event", "Contacts", "Scripts", "Review and launch"];
const FIELD: Record<(typeof SCRIPT_FIELDS)[number], [label: string, hint: string, rows: number]> = {
  greeting: ["Greeting", "{name} is replaced with each person's name.", 2],
  message: ["Message", "What the call is about.", 3],
  menu: ["Keypad menu (main answer)", "Keys are fixed: 1 confirm, 2 decline, 3 reschedule.", 2],
  voicemail: ["Voicemail", "Played when nobody responds after the greeting.", 2],
  goodbye: ["Goodbye", "", 1],
};
const TITLE_HINT: Record<string, string> = {
  seminar: "Pune AI Summit", clinic: "your follow-up with Dr Rao", school: "the parent-teacher meeting", payment: "the Term 2 fee of ₹4,500",
};
const SAMPLE = "name,phone,language,segment\nAsha Kulkarni,8943198705,ml,Class 5\nRavi Menon,+91 8301920200,en,Class 6\n";

const EMPTY_EVENT: EventDetails = { org: "", kind: "seminar", title: "", date: "", time: "", venue: "", details: "" };

export default function NewCampaignPage() {
  useCrumbs([["Campaigns", "/campaigns"], ["New campaign"]]);
  const router = useRouter();
  const { toast, modal } = useShell();
  const { data: opts, error, retry } = useData(() => api<BuilderOptions>("/builder/options"), []);

  const [step, setStep] = useState(0);
  const [ev, setEv] = useState<EventDetails>(EMPTY_EVENT);
  const [langs, setLangs] = useState<string[]>(["en"]);
  const [textP, setTextP] = useState<TextProvider>("template");
  const [voiceP, setVoiceP] = useState<VoiceProvider>("piper");
  const [escalation, setEscalation] = useState(false);
  const [agentP, setAgentP] = useState("");
  const [record, setRecord] = useState(false);
  const [csv, setCsv] = useState("");
  const [check, setCheck] = useState<{ key: string; res: ContactsCheck } | null>(null);
  const [draft, setDraft] = useState<{ key: string; d: Draft; warnings: string[] } | null>(null);
  const [name, setName] = useState("");
  const [tab, setTab] = useState("en");
  const [est, setEst] = useState<{ key: string; e: Estimate } | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState<"" | "draft" | "contacts" | "chatgpt">("");

  // Prefer a configured model (templates are the fallback) and a voice that can make real calls.
  // An assistant hand-off keeps the model that wrote its scripts, so the draft is not marked stale.
  const prefilledModel = useRef(false);
  useEffect(() => {
    const best = opts?.text_providers.find(p => p.key !== "template" && p.available);
    if (best && !prefilledModel.current) setTextP(best.key as TextProvider);
    const voice = opts?.voice_providers.find(p => p.available);
    if (voice) setVoiceP(voice.key as VoiceProvider);
    if (opts) setAgentP(a => a || opts.default_agent);
  }, [opts]);

  // Event described to the dashboard assistant: fill the form once, the person still reviews everything.
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(PREFILL_KEY);
      if (!raw) return;
      sessionStorage.removeItem(PREFILL_KEY);
      const p = JSON.parse(raw) as Prefill;
      const event = { ...EMPTY_EVENT, ...p.event };
      const languages = p.languages?.length ? p.languages : ["en"];
      setEv(event);
      setLangs(languages);
      if (p.text_provider) { prefilledModel.current = true; setTextP(p.text_provider); }
      if (p.draft && p.text_provider) {
        // Keyed exactly like a draft made here (escalation starts off), so editing the event still flags it stale.
        setDraft({ key: JSON.stringify([event, languages, p.text_provider, false]), d: p.draft, warnings: p.warnings ?? [] });
        setName(p.draft.name);
        setTab(languages[0]);
      }
      toast(p.draft ? "Event and scripts filled in by the assistant. Add contacts, then review the scripts."
        : "Event filled in from your description. Check it, then add contacts.");
    } catch { /* unreadable or unavailable storage: start with an empty form */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Back from the ChatGPT sign-in: show the result once, then clean the address bar.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const result = q.get("chatgpt");
    if (!result) return;
    if (result === "connected") toast("ChatGPT connected");
    else toast(q.get("reason") || "ChatGPT sign-in failed", "error");
    window.history.replaceState(null, "", window.location.pathname);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const draftKey = JSON.stringify([ev, langs, textP, escalation]);
  const contactsKey = JSON.stringify([csv, langs]);
  const d = draft?.d;
  const contacts = check?.key === contactsKey ? check.res : null;
  const questions = d?.questions ?? [];
  const asks = escalation && !!opts?.escalation.available;  // the call ends with "any other questions?"
  const scriptsBody = d ? Object.fromEntries(langs.filter(l => d.scripts[l]).map(l =>
    [l, { ...Object.fromEntries(SCRIPT_FIELDS.map(f => [f, d.scripts[l][f]])),
      questions: Object.fromEntries(questions.map(q => [q.id, d.scripts[l].questions?.[q.id] ?? ""])),
      ...(asks ? { doubts: d.scripts[l].doubts ?? "" } : {}) }])) : {};
  const estBody = d && contacts ? {
    kind: ev.kind, by_language: contacts.by_language, scripts: scriptsBody, retry: d.retry, questions,
    escalation, record, voice_provider: voiceP, text_provider: textP, agent_provider: agentP,
  } : null;
  const estKey = JSON.stringify(estBody);

  useEffect(() => {
    if (step !== 3 || !estBody) return;
    let live = true;
    post<Estimate>("/builder/estimate", estBody)
      .then(e => { if (live) setEst({ key: estKey, e }); })
      .catch(e => { if (live) toast((e as Error).message, "error"); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, estKey]);

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!opts) return <main className="view"><Skeleton /></main>;

  const langName = (c: string) => opts.languages.find(l => l.code === c)?.name ?? c;
  const provider = opts.text_providers.find(p => p.key === textP)!;
  const chosenAgent = opts.agent_providers.find(p => p.key === agentP);
  const estimate = est?.key === estKey ? est.e : null;

  const ok = [
    !!ev.title.trim() && langs.length > 0,
    !!contacts && contacts.count > 0 && contacts.error_count === 0,
    !!d && !!name.trim() && langs.every(l => d.scripts[l] && SCRIPT_FIELDS.every(f => d.scripts[l][f].trim())) &&
      questions.every(q => q.label.trim() && q.options.every(o => o.trim()) &&
        langs.every(l => d.scripts[l]?.questions?.[q.id]?.trim())) &&
      (!asks || langs.every(l => d.scripts[l]?.doubts?.trim())),
  ];
  const reachable = (i: number) => ok.slice(0, i).every(Boolean);
  // English placeholder text left in another language: whole scripts, or questions still in their English fallback.
  const placeholders = d ? langs.filter(l => d.scripts[l]?.placeholder ||
    (l !== "en" && questions.some(q => d.scripts[l]?.questions?.[q.id] === questionText(q)))) : [];

  const setE = (patch: Partial<EventDetails>) => setEv(p => ({ ...p, ...patch }));
  const toggleLang = (c: string) => setLangs(p => p.includes(c) ? p.filter(x => x !== c) : [...p, c]);

  const checkContacts = async () => {
    setBusy("contacts");
    try {
      const res = await post<ContactsCheck>("/builder/contacts", { csv, languages: langs });
      setCheck({ key: contactsKey, res });
      return res;
    } catch (e) { toast((e as Error).message, "error"); return null; }
    finally { setBusy(""); }
  };

  const connectChatGPT = async () => {
    setBusy("chatgpt");
    try {
      const r = await post<{ url: string }>("/auth/chatgpt/start");
      window.location.href = r.url; // the form resets on return; the sign-in is a full-page trip
    } catch (e) { toast((e as Error).message, "error"); setBusy(""); }
  };

  const disconnectChatGPT = async () => {
    setBusy("chatgpt");
    try { await post("/auth/chatgpt/disconnect"); setTextP("template"); retry(); }
    catch (e) { toast((e as Error).message, "error"); }
    finally { setBusy(""); }
  };

  const makeDraft = async () => {
    setBusy("draft");
    try {
      const r = await post<{ draft: Draft; warnings: string[] }>("/builder/draft",
        { event: ev, languages: langs, text_provider: textP, escalation, agent_provider: agentP });
      setDraft({ key: draftKey, d: r.draft, warnings: r.warnings });
      setName(r.draft.name);
      setTab(langs[0]);
      setReviewed(false);
    } catch (e) { toast((e as Error).message, "error"); }
    finally { setBusy(""); }
  };

  const editScript = (l: string, f: (typeof SCRIPT_FIELDS)[number], v: string) => setDraft(p => p && {
    ...p, d: { ...p.d, scripts: { ...p.d.scripts, [l]: { ...p.d.scripts[l], [f]: v, placeholder: false } } },
  });
  const editQuestionText = (l: string, id: string, v: string) => setDraft(p => p && {
    ...p, d: { ...p.d, scripts: { ...p.d.scripts, [l]: { ...p.d.scripts[l], questions: { ...p.d.scripts[l].questions, [id]: v } } } },
  });
  // Question edits keep each language's spoken text in step: an untouched English fallback follows the
  // question; text the AI translated or someone wrote is left alone (the editor asks them to update it).
  const setQuestions = (next: Question[]) => setDraft(p => {
    if (!p) return p;
    const old = new Map((p.d.questions ?? []).map(q => [q.id, q]));
    const scripts = Object.fromEntries(Object.entries(p.d.scripts).map(([l, sc]) => [l, {
      ...sc, questions: Object.fromEntries(next.map(q => {
        const prev = sc.questions?.[q.id], o = old.get(q.id);
        return [q.id, prev === undefined || (o && prev === questionText(o)) ? questionText(q) : prev];
      })),
    }]));
    return { ...p, d: { ...p.d, questions: next, scripts } };
  });
  const editRetry = (k: "max_attempts" | "gap_hours", v: number) => setDraft(p => p && {
    ...p, d: { ...p.d, retry: { ...p.d.retry, [k]: v } },
  });

  const next = async () => {
    if (step === 1 && !contacts) {
      const res = await checkContacts();
      if (!res || !res.count || res.error_count) return;
    }
    setStep(s => s + 1);
    window.scrollTo({ top: 0 });
  };

  const launch = () => {
    if (!d || !contacts || !estimate) return;
    const live = opts.launch_mode === "live";
    modal({
      title: live ? "Start calling?" : "Start a simulated campaign?",
      body: (
        <>
          {live
            ? `Calls start now and run only within calling hours (${opts.call_window}). You can pause at any time.`
            : "Demo mode: no real calls are placed. Outcomes are simulated so you can watch the dashboard fill in."}
          <dl className="facts">
            <div><dt>Recipients</dt><dd>{fmt.int(contacts.count)}</dd></div>
            <div><dt>Languages</dt><dd>{langs.map(langName).join(", ")}</dd></div>
            <div><dt>Retries</dt><dd>Up to {d.retry.max_attempts} attempts, {d.retry.gap_hours} h apart</dd></div>
            <div><dt>Follow-up questions</dt><dd>{questions.length ? questions.map(q => q.label).join(", ") : "None"}</dd></div>
            <div><dt>Estimated cost</dt><dd>{fmt.inr2(estimate.total_inr)}</dd></div>
          </dl>
        </>
      ),
      confirmLabel: live ? `Call ${fmt.int(contacts.count)} people` : "Start simulation",
      onConfirm: async () => {
        const r = await post<{ id: string; mode: string }>("/campaigns", {
          name: name.trim(), event: ev, languages: langs, text_provider: textP, voice_provider: voiceP,
          escalation, agent_provider: agentP, record, contacts_csv: csv, scripts: scriptsBody, questions, retry: d.retry, reviewed,
        });
        toast(r.mode === "live" ? "Campaign launched" : "Simulated campaign started");
        router.push(`/campaigns/${r.id}`);
      },
    });
  };

  return (
    <main className="view enter">
      <PageHead title="New campaign" sub="Describe the event, add contacts, review the drafted scripts and the cost, then launch." />

      <nav className="steps" aria-label="Steps">
        {STEPS.map((label, i) => (
          <button key={label} className={`${step === i ? "on" : ""} ${i < step && ok[i] ? "done" : ""}`}
            disabled={!reachable(i)} onClick={() => setStep(i)} aria-current={step === i ? "step" : undefined}>
            <span className="num">{i < step && ok[i] ? <Icon name="check" size={12} /> : i + 1}</span>{label}
          </button>
        ))}
      </nav>

      {step === 0 && (
        <section className="card form">
          <div className="row-2">
            <div className="field"><label htmlFor="org">Organisation</label>
              <input id="org" className="input" placeholder="Greenfield School" value={ev.org} onChange={e => setE({ org: e.target.value })} />
            </div>
            <div className="field"><label htmlFor="kind">Type</label>
              <select id="kind" className="select" value={ev.kind} onChange={e => setE({ kind: e.target.value as EventDetails["kind"] })}>
                {opts.kinds.map(k => <option key={k.value} value={k.value}>{k.label}</option>)}
              </select>
            </div>
          </div>
          <div className="field"><label htmlFor="title">What is it about?</label>
            <input id="title" className="input" placeholder={`e.g. ${TITLE_HINT[ev.kind]}`} value={ev.title} onChange={e => setE({ title: e.target.value })} />
          </div>
          <div className="row-3">
            <div className="field"><label htmlFor="date">{ev.kind === "payment" ? "Due date" : "Date"}</label>
              <input id="date" className="input" placeholder="18 October" value={ev.date} onChange={e => setE({ date: e.target.value })} />
            </div>
            <div className="field"><label htmlFor="time">Time</label>
              <input id="time" className="input" placeholder="10:30 am" value={ev.time} onChange={e => setE({ time: e.target.value })} />
            </div>
            <div className="field"><label htmlFor="venue">Venue</label>
              <input id="venue" className="input" placeholder="Main hall" value={ev.venue} onChange={e => setE({ venue: e.target.value })} />
            </div>
          </div>
          <div className="field"><label htmlFor="details">Anything else people should hear</label>
            <textarea id="details" className="input" rows={3} maxLength={600} value={ev.details} onChange={e => setE({ details: e.target.value })}
              placeholder="What to bring, the amount due, a number to call back…" />
          </div>
          <div className="field"><span className="label">Languages</span>
            <div className="toggles" role="group" aria-label="Languages">
              {opts.languages.map(l => (
                <button key={l.code} className={`toggle${langs.includes(l.code) ? " on" : ""}`} aria-pressed={langs.includes(l.code)}
                  onClick={() => toggleLang(l.code)}>{l.name}</button>
              ))}
            </div>
            <span className="hint">Each language gets its own script. Each contact hears the language set in the list.</span>
          </div>
          <div className="field"><span className="label">Who drafts the scripts</span>
            <div className="choices">
              {opts.text_providers.map(p => (
                <button key={p.key} className={`choice${textP === p.key ? " on" : ""}`} disabled={!p.available}
                  aria-pressed={textP === p.key} onClick={() => setTextP(p.key as TextProvider)}>
                  <b>{p.label}{!p.available && <em>Not configured</em>}</b><small>{p.sends}</small>
                </button>
              ))}
            </div>
            <div className="toggles" style={{ marginTop: 8 }}>
              {opts.chatgpt.connected ? (
                <>
                  <span className="hint">Drafts use the ChatGPT plan of {opts.chatgpt.email ?? "the connected account"}, shared by everyone using this server.
                    If it is unavailable or out of allowance, drafting falls back to the other models, then the templates.</span>
                  <button className="btn sm" disabled={busy === "chatgpt"} onClick={() => void disconnectChatGPT()}>Disconnect ChatGPT</button>
                </>
              ) : (
                <>
                  <button className="btn" disabled={busy === "chatgpt"} onClick={() => void connectChatGPT()}>
                    {busy === "chatgpt" && <span className="spinner" />}Connect ChatGPT
                  </button>
                  <span className="hint">Needs a ChatGPT Plus or Pro plan. Opens OpenAI to sign in; open this dashboard on the machine that runs the server.
                    Without it, drafting uses the other models or the templates.</span>
                </>
              )}
            </div>
          </div>
          <div className="field"><span className="label">Who voices the calls</span>
            <div className="choices">
              {opts.voice_providers.map(p => {
                const off = !p.available && opts.launch_mode === "live";  // simulations need no audio
                return (
                  <button key={p.key} className={`choice${voiceP === p.key ? " on" : ""}`} aria-pressed={voiceP === p.key}
                    disabled={off} onClick={() => setVoiceP(p.key as VoiceProvider)}>
                    <b>{p.label}{!p.available && <em>{p.key === "elevenlabs" ? "Not configured" : "Simulation only"}</em>}</b>
                    <small>{p.sends}</small>
                  </button>
                );
              })}
            </div>
            <span className="hint">Scripts are synthesised once per language before calls start, plus each recipient&apos;s name.
              {voiceP === "elevenlabs" && " The campaign shows “Preparing voice” until that is done."}</span>
          </div>
          <label className="check">
            <input type="checkbox" checked={escalation && opts.escalation.available} disabled={!opts.escalation.available}
              onChange={e => setEscalation(e.target.checked)} />
            <span>End by asking for any other questions; a live voice assistant answers them
              <small>{!opts.escalation.available ? "Set GEMINI_API_KEY (Gemini Live), or the ElevenLabs agent keys, to turn this on."
                : `Only callers who start asking something reach it, and those calls cost more. ${chosenAgent?.sends ?? opts.escalation.sends}.`}</small></span>
          </label>
          {escalation && opts.escalation.available && (
            <div className="field"><span className="label">Which assistant answers</span>
              <div className="choices">
                {opts.agent_providers.map(p => (
                  <button key={p.key} className={`choice${agentP === p.key ? " on" : ""}`} disabled={!p.available}
                    aria-pressed={agentP === p.key} onClick={() => setAgentP(p.key)}>
                    <b>{p.label}{!p.available && <em>Not configured</em>}</b><small>{p.sends}</small>
                  </button>
                ))}
              </div>
            </div>
          )}
          <label className="check">
            <input type="checkbox" checked={record} onChange={e => setRecord(e.target.checked)} />
            <span>Record calls<small>Recordings are personal data. Playback needs a PIN and every play is logged.</small></span>
          </label>
        </section>
      )}

      {step === 1 && (
        <>
          <section className="card form">
            <div className="field"><label htmlFor="csv">Contact list</label>
              <textarea id="csv" className="input mono" rows={9} value={csv} onChange={e => setCsv(e.target.value)} spellCheck={false}
                placeholder={"name, phone, language, segment\nAsha Kulkarni, 98765 43210, ml, Class 5"} />
              <span className="hint">
                CSV with name and phone; language ({langs.join(", ")}) and segment are optional. A header row is optional.
                Missing language means {langName(langs[0])}.
              </span>
            </div>
            <div className="toggles">
              <label className="btn">
                <Icon name="upload" />Upload CSV
                <input type="file" accept=".csv,text/csv,text/plain" hidden
                  onChange={async e => { const f = e.target.files?.[0]; if (f) setCsv(await f.text()); e.target.value = ""; }} />
              </label>
              {!csv && <button className="btn ghost" onClick={() => setCsv(SAMPLE)}>Use a sample</button>}
              <button className="btn" disabled={!csv.trim() || busy === "contacts"} onClick={() => void checkContacts()}>
                {busy === "contacts" ? <span className="spinner" /> : <Icon name="check" />}Check list
              </button>
            </div>
          </section>
          {contacts && (
            <section className="card">
              <div className="card-head"><h2>{fmt.int(contacts.count)} contacts ready</h2>
                <div className="kv">
                  {langs.map(l => <span key={l}>{langName(l)} <b>{fmt.int(contacts.by_language[l] ?? 0)}</b></span>)}
                  {Object.entries(contacts.segments).slice(0, 4).map(([s, n]) => <span key={s}>{s} <b>{fmt.int(n)}</b></span>)}
                </div>
              </div>
              {contacts.error_count > 0 && (
                <>
                  <div style={{ padding: "12px 16px 8px" }}>
                    <Notice>Fix {fmt.int(contacts.error_count)} row{contacts.error_count > 1 ? "s" : ""} before continuing.</Notice>
                  </div>
                  <ul className="err-list">
                    {contacts.errors.map(e => <li key={e.line}><b>Line {e.line}</b>{e.error}</li>)}
                  </ul>
                </>
              )}
              {contacts.preview.length > 0 && (
                <div className="table-wrap">
                  <table className="table">
                    <thead><tr><th>Name</th><th>Phone</th><th>Language</th><th>Segment</th></tr></thead>
                    <tbody>{contacts.preview.map((r, i) => (
                      <tr key={i}><td>{r.name}</td><td className="mono muted">{r.phone}</td><td>{r.language}</td><td className="muted">{r.segment}</td></tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
              <div className="table-foot"><span>Numbers are masked here; full numbers are used only to dial.</span></div>
            </section>
          )}
        </>
      )}

      {step === 2 && (!d ? (
        <section className="card empty">
          <h2>Draft every script in one go</h2>
          <p>{provider.label} writes the greeting, message, keypad menu, voicemail and goodbye in{" "}
            {langs.map(langName).join(", ")}, picks the follow-up questions this event needs (food preference, T-shirt
            size and so on, or none), and suggests a retry policy. One call for the whole campaign; you review everything before launch.</p>
          <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>{provider.sends}.</p>
          <p style={{ marginTop: 16 }}>
            <button className="btn primary" disabled={busy === "draft"} onClick={() => void makeDraft()}>
              {busy === "draft" ? <><span className="spinner" />Drafting</> : <><Icon name="wand" />Draft scripts</>}
            </button>
          </p>
          {busy === "draft" && <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>Writing every language in one go can take up to a minute.</p>}
        </section>
      ) : (
        <>
          <div className="stack-gap">
            {draft!.key !== draftKey && (
              <Notice>
                The event details, languages or model changed after this draft. Redraft, or edit the scripts by hand.{" "}
                <button className="btn sm" disabled={busy === "draft"} onClick={() => void makeDraft()}>
                  {busy === "draft" ? <span className="spinner" /> : <Icon name="refresh" size={14} />}Redraft
                </button>
              </Notice>
            )}
            {draft!.warnings.length > 0 && <Notice><ul>{draft!.warnings.map(w => <li key={w}>{w}</li>)}</ul></Notice>}
            {d.notes && <Notice kind="info">Note from the model: {d.notes}</Notice>}
          </div>
          <section className="card form">
            <div className="row-2">
              <div className="field"><label htmlFor="cname">Campaign name</label>
                <input id="cname" className="input" maxLength={80} value={name} onChange={e => setName(e.target.value)} />
              </div>
              <div className="row-2">
                <div className="field"><label htmlFor="att">Attempts per person</label>
                  <select id="att" className="select" value={d.retry.max_attempts} onChange={e => editRetry("max_attempts", +e.target.value)}>
                    {[1, 2, 3, 4].map(n => <option key={n} value={n}>{n}</option>)}
                  </select>
                </div>
                <div className="field"><label htmlFor="gap">Hours between tries</label>
                  <input id="gap" className="input" type="number" min={1} max={48} value={d.retry.gap_hours}
                    onChange={e => editRetry("gap_hours", Math.max(1, Math.min(48, +e.target.value || 1)))} />
                </div>
              </div>
            </div>
          </section>
          <QuestionsEditor questions={questions} onChange={setQuestions} escalation={escalation && opts.escalation.available}
            payment={ev.kind === "payment"} />
          <section className="card">
            <div className="tabs" role="tablist">
              {langs.map(l => (
                <button key={l} role="tab" aria-selected={tab === l} className={tab === l ? "on" : ""} onClick={() => setTab(l)}>
                  {langName(l)}{d.scripts[l]?.placeholder && <i className="dot" title="English placeholder" />}
                </button>
              ))}
            </div>
            {d.scripts[tab] ? (
              <div className="form">
                {SCRIPT_FIELDS.map(f => (
                  <div className="field" key={f}><label htmlFor={`s-${f}`}>{FIELD[f][0]}</label>
                    <textarea id={`s-${f}`} className="input" rows={FIELD[f][2]} value={d.scripts[tab][f]}
                      onChange={e => editScript(tab, f, e.target.value)} lang={tab} />
                    {FIELD[f][1] && <span className="hint">{FIELD[f][1]}</span>}
                  </div>
                ))}
                {asks && (
                  <div className="field"><label htmlFor="s-doubts">Closing question</label>
                    <textarea id="s-doubts" className="input" rows={2} lang={tab} value={d.scripts[tab].doubts ?? ""}
                      onChange={e => setDraft(p => p && { ...p, d: { ...p.d, scripts: { ...p.d.scripts, [tab]: { ...p.d.scripts[tab], doubts: e.target.value } } } })} />
                    <span className="hint">Asked last. A caller who starts speaking is connected to the assistant.
                      {!d.scripts[tab].doubts && " Redraft to have the AI write it in every language."}</span>
                  </div>
                )}
                {questions.map((q, i) => (
                  <div className="field" key={q.id}><label htmlFor={`s-${q.id}`}>Question {i + 1}: {q.label || "untitled"}</label>
                    <textarea id={`s-${q.id}`} className="input" rows={2} lang={tab} value={d.scripts[tab].questions?.[q.id] ?? ""}
                      onChange={e => editQuestionText(tab, q.id, e.target.value)} />
                    <span className="hint">Read out every option with its key: {q.options.map((o, k) => `${k + 1} ${o || "…"}`).join(", ")}.</span>
                  </div>
                ))}
              </div>
            ) : <div className="empty"><h2>No {langName(tab)} script yet</h2><p>Redraft to add it.</p></div>}
          </section>
        </>
      ))}

      {step === 3 && d && contacts && (!estimate ? <Skeleton /> : (
        <>
          <section className="grid-2">
            <div className="card">
              <div className="card-head"><h2>Estimated cost</h2></div>
              <dl className="facts cost-lines" style={{ margin: 16 }}>
                {estimate.lines.map(l => (
                  <div key={l.label}><dt>{l.label}<small>{l.detail}</small></dt><dd>{fmt.inr2(l.inr)}</dd></div>
                ))}
                <div className="total"><dt>Total<small>{fmt.inr2(estimate.per_recipient_inr)} per recipient</small></dt><dd>{fmt.inr2(estimate.total_inr)}</dd></div>
              </dl>
              <div className="compare">
                <span>The same campaign with a voice agent on every answered call</span>
                <span>{fmt.inr2(estimate.all_agent_inr)} · <b>{fmt.pct(1 - estimate.total_inr / Math.max(estimate.all_agent_inr, 0.01))} saved</b></span>
              </div>
            </div>
            <div className="card">
              <div className="card-head"><h2>{name}</h2><span className="chip">{KIND[ev.kind]}</span></div>
              <dl className="facts" style={{ margin: 16 }}>
                <div><dt>Recipients</dt><dd>{fmt.int(contacts.count)}</dd></div>
                <div><dt>Languages</dt><dd>{langs.map(langName).join(", ")}</dd></div>
                <div><dt>Retries</dt><dd>Up to {d.retry.max_attempts} attempts, {d.retry.gap_hours} h apart</dd></div>
                <div><dt>Expected answers</dt><dd>{fmt.int(estimate.expected_answered)} of {fmt.int(estimate.recipients)}</dd></div>
                <div><dt>Calling hours</dt><dd>{opts.call_window}</dd></div>
              </dl>
              <p className="muted" style={{ padding: "0 16px 16px", fontSize: 12 }}>
                Assumes {fmt.pct(estimate.assumptions.pickup)} pick up per attempt and calls of about {estimate.assumptions.call_seconds} s.
                Rates are estimates until the first bill.
              </p>
            </div>
          </section>
          <HandlingCard h={estimate.handling} title="Where this campaign's data is processed" />
          <div className="stack-gap">
            {placeholders.length > 0 && (
              <Notice>{placeholders.map(langName).join(", ")} still {placeholders.length > 1 ? "use" : "uses"} the English placeholder.
                Those recipients will hear English unless you edit the script.</Notice>
            )}
            {opts.launch_mode === "simulated" && <Notice kind="info">Demo mode: launching simulates calls. Nobody is phoned.</Notice>}
            {opts.launch_mode === "unavailable" && (
              <Notice>Calling is not set up. Configure Exotel, PUBLIC_URL and WEBHOOK_TOKEN in .env, or start the server with DEMO=1 to simulate.</Notice>
            )}
          </div>
          <section className="card form">
            <label className="check">
              <input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)} />
              <span>I have read every script in every language and they are right to send
                <small>Nothing is called until you launch.</small></span>
            </label>
          </section>
        </>
      ))}

      <div className="card builder-foot" style={{ borderTop: 0 }}>
        <button className="btn" disabled={step === 0} onClick={() => setStep(s => s - 1)}>Back</button>
        {step < 3 ? (
          <button className="btn primary" disabled={!ok[step] && !(step === 1 && csv.trim() && !contacts) || busy !== ""}
            onClick={() => void next()}>
            {busy === "contacts" && <span className="spinner" />}Next: {STEPS[step + 1]}
          </button>
        ) : (
          <button className="btn primary" disabled={!reviewed || !estimate || opts.launch_mode === "unavailable"} onClick={launch}>
            <Icon name="phone" />{opts.launch_mode === "simulated" ? "Launch simulation" : "Launch"}
          </button>
        )}
      </div>
    </main>
  );
}
