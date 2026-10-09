"use client";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { fmt, KIND } from "@/lib/format";
import {
  PREFILL_KEY, SCRIPT_FIELDS, questionText, type Prefill, type Question, type BuilderOptions, type ContactsCheck, type Draft, type Estimate, type EventDetails,
  type AgentEdits, type Mode, type ProviderKey, type Script,
} from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { Listen } from "@/components/Listen";
import { clearChat, loadChat, registerBuilder, type BuilderBridge } from "@/lib/agentbridge";
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

const OTHER = "__other__";
const EMPTY_EVENT: EventDetails = { org: "", kind: "seminar", title: "", date: "", time: "", venue: "", details: "" };

export default function NewCampaignPage() {
  useCrumbs([["Campaigns", "/campaigns"], ["New campaign"]]);
  const router = useRouter();
  const { toast, modal } = useShell();
  const { data: opts, error, retry } = useData(() => api<BuilderOptions>("/builder/options"), []);

  const [step, setStep] = useState(0);
  const [ev, setEv] = useState<EventDetails>(EMPTY_EVENT);
  const [langs, setLangs] = useState<string[]>(["en"]);
  const [provider, setProvider] = useState<ProviderKey | "">("");
  const [mode, setMode] = useState<Mode>("hybrid");
  const [csv, setCsv] = useState("");
  const [check, setCheck] = useState<{ key: string; res: ContactsCheck } | null>(null);
  const [draft, setDraft] = useState<{ key: string; d: Draft; warnings: string[] } | null>(null);
  const [name, setName] = useState("");
  const [tab, setTab] = useState("en");
  const [est, setEst] = useState<{ key: string; e: Estimate } | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState<"" | "draft" | "contacts">("");

  // Start on the saved default provider (an assistant hand-off keeps its own pick).
  const prefilledModel = useRef(false);
  useEffect(() => {
    if (opts && !prefilledModel.current) setProvider(p => p || opts.default_provider);
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
      if (p.provider) { prefilledModel.current = true; setProvider(p.provider); }
      if (p.draft) {
        // Keyed exactly like a draft made here, so editing the event still flags it stale.
        setDraft({ key: JSON.stringify([event, languages, "hybrid"]), d: p.draft, warnings: p.warnings ?? [] });
        setName(p.draft.name);
        setTab(languages[0]);
      }
      toast(p.draft ? "Event and scripts filled in by the assistant. Add contacts, then review the scripts."
        : "Event filled in from your description. Check it, then add contacts.");
    } catch { /* unreadable or unavailable storage: start with an empty form */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The campaign agent (the dock chat) reads and edits this form through a bridge, kept pointing at the latest closures.
  const bridge = useRef<BuilderBridge | null>(null);
  useEffect(() => registerBuilder({
    snapshot: () => bridge.current?.snapshot() ?? {}, apply: (e, a) => bridge.current?.apply(e, a) ?? [],
  }), []);

  const draftKey = JSON.stringify([ev, langs, mode]);
  const contactsKey = JSON.stringify([csv, langs]);
  const d = draft?.d;
  const contacts = check?.key === contactsKey ? check.res : null;
  const questions = d?.questions ?? [];
  const prov = opts?.providers.find(p => p.key === provider);
  const hybrid = mode === "hybrid";
  // Hybrid calls end with "any other questions?", answered live by the provider.
  const asks = hybrid && !!prov?.caps.live.ready;
  const scriptsBody = d ? Object.fromEntries(langs.filter(l => d.scripts[l]).map(l =>
    [l, { ...Object.fromEntries(SCRIPT_FIELDS.map(f => [f, d.scripts[l][f]])),
      questions: Object.fromEntries(questions.map(q => [q.id, d.scripts[l].questions?.[q.id] ?? ""])),
      ...(asks ? { doubts: d.scripts[l].doubts ?? "" } : {}) }])) : {};
  const estBody = d && contacts ? {
    kind: ev.kind, by_language: contacts.by_language, scripts: scriptsBody, retry: d.retry, questions,
    provider, mode,
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
  const voiceReady = opts.providers.some(p => p.caps.voice.ready);  // IVR audio is always ElevenLabs
  const unusable = !prov || !prov.caps.live.ready || (hybrid && !voiceReady);
  const estimate = est?.key === estKey ? est.e : null;
  const ok = [
    !!ev.title.trim() && !!ev.kind.trim() && langs.length > 0,
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

  const preview = (language: string, text: string) => fetch("/api/builder/preview-audio", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider, language, text }),
  });

  const makeDraft = async (o?: { ev: EventDetails; langs: string[]; mode: Mode; provider: string }) => {
    const use = o ?? { ev, langs, mode, provider };
    setBusy("draft");
    try {
      const r = await post<{ draft: Draft; warnings: string[] }>("/builder/draft",
        { event: use.ev, languages: use.langs, provider: use.provider, mode: use.mode });
      setDraft({ key: JSON.stringify([use.ev, use.langs, use.mode]), d: r.draft, warnings: r.warnings });
      setName(r.draft.name);
      setTab(use.langs[0]);
      setReviewed(false);
    } catch (e) { toast((e as Error).message, "error"); }
    finally { setBusy(""); }
  };

  const editScript = (l: string, f: (typeof SCRIPT_FIELDS)[number], v: string) => setDraft(p => p && {
    ...p, d: { ...p.d, scripts: { ...p.d.scripts, [l]: { ...p.d.scripts[l], [f]: v, placeholder: false } } },
  });
  const editPrompt = (v: string) => setDraft(p => p && { ...p, d: { ...p.d, system_prompt: v } });
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

  bridge.current = {
    snapshot: () => ({
      step: STEPS[step], event: ev, languages: langs, provider, mode, name,
      providers: opts.providers.map(p => ({ key: p.key, label: p.label, live_ready: p.caps.live.ready, ivr_audio_ready: p.caps.voice.ready })),
      phones_online: opts.phones, has_draft: !!d,
      retry: d?.retry ?? null, system_prompt: d?.system_prompt ?? null,
      scripts: d ? Object.fromEntries(langs.filter(l => d.scripts[l]).map(l => [l, {
        ...Object.fromEntries(SCRIPT_FIELDS.map(f => [f, d.scripts[l][f]])), doubts: d.scripts[l].doubts ?? "", questions: d.scripts[l].questions ?? {} }])) : null,
      questions: d?.questions ?? [],
      contacts: contacts ? { count: contacts.count, by_language: contacts.by_language, segments: contacts.segments } : null,
      estimate_total_inr: estimate?.total_inr ?? null, draft_notes: d?.notes || null,
    }),
    apply: (e: AgentEdits, actions: string[]) => {
      const done: string[] = [];
      const nev = e.event ? { ...ev, ...e.event } as EventDetails : ev;
      const nl = e.languages ?? langs, nm = e.mode ?? mode, np = e.provider ?? provider;
      if (e.event) { setEv(nev); done.push("event"); }
      if (e.languages) { setLangs(nl); setTab(nl[0]); done.push("languages"); }
      if (e.provider) { setProvider(np); done.push("provider"); }
      if (e.mode) { setMode(nm); done.push("mode"); }
      if (e.name) { setName(e.name); done.push("name"); }
      if (d) {
        if (e.questions) { setQuestions(e.questions); done.push("questions"); }
        if (e.retry) { setDraft(p => p && { ...p, d: { ...p.d, retry: e.retry! } }); done.push("retry"); }
        if (e.system_prompt) { editPrompt(e.system_prompt); done.push("system prompt"); }
        if (e.scripts) {
          setDraft(p => p && { ...p, d: { ...p.d, scripts: Object.fromEntries(Object.entries(p.d.scripts).map(([l, sc]) => {
            const part = e.scripts?.[l];
            if (!part) return [l, sc];
            const { questions: qs, ...fields } = part;
            return [l, { ...sc, ...fields, questions: { ...sc.questions, ...(qs ?? {}) }, placeholder: false }];
          })) } });
          done.push("scripts");
        }
      }
      if (actions.includes("redraft")) { void makeDraft({ ev: nev, langs: nl, mode: nm, provider: np }); done.push("redraft"); }
      else if (d && e.scripts && (e.event || e.languages || e.mode)) {
        // The agent rewrote the scripts together with the event, so the draft still matches it.
        setDraft(p => p && { ...p, key: JSON.stringify([nev, nl, nm]) });
      }
      return done;
    },
  };

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
    modal({
      title: "Start calling?",
      body: (
        <>
          {hybrid
            ? `The IVR audio is made first (the campaign shows “Preparing”), then the phone rings for one person at a time. Callers with a question at the end are taken over by ${prov!.label}.`
            : `The phone rings for one person at a time, now, and ${prov!.label} holds the whole conversation.`}
          {" "}{opts.phones ? "A phone page is open and ready." : "No phone page is open yet: open /phone on a device, or the campaign waits."}
          <dl className="facts">
            <div><dt>Recipients</dt><dd>{fmt.int(contacts.count)}</dd></div>
            <div><dt>Provider</dt><dd>{prov!.label}, {hybrid ? "hybrid" : "live"} mode</dd></div>
            <div><dt>Languages</dt><dd>{langs.map(langName).join(", ")}</dd></div>
            <div><dt>Retries</dt><dd>Up to {d.retry.max_attempts} attempts, {d.retry.gap_hours} h apart</dd></div>
            <div><dt>Follow-up questions</dt><dd>{questions.length ? questions.map(q => q.label).join(", ") : "None"}</dd></div>
            <div><dt>Estimated cost</dt><dd>{fmt.inr2(estimate.total_inr)}</dd></div>
          </dl>
        </>
      ),
      confirmLabel: `Call ${fmt.int(contacts.count)} people`,
      onConfirm: async () => {
        const r = await post<{ id: string; mode: string }>("/campaigns", {
          name: name.trim(), event: ev, languages: langs, provider, mode, system_prompt: d.system_prompt,
          contacts_csv: csv, scripts: scriptsBody, questions, retry: d.retry, reviewed, chat: loadChat(),
        });
        clearChat();  // the agent chat now lives on the campaign
        toast(hybrid ? "Campaign created: making the IVR audio" : "Campaign launched");
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
              <select id="kind" className="select" value={opts.kinds.some(k => k.value === ev.kind) ? ev.kind : OTHER}
                onChange={e => setE({ kind: e.target.value === OTHER ? "" : e.target.value })}>
                {opts.kinds.map(k => <option key={k.value} value={k.value}>{k.label}</option>)}
                <option value={OTHER}>Other (type your own)…</option>
              </select>
              {!opts.kinds.some(k => k.value === ev.kind) && (
                <input className="input" style={{ marginTop: 8 }} maxLength={40} aria-label="Custom campaign type"
                  placeholder="e.g. Workshop reminder" value={ev.kind} onChange={e => setE({ kind: e.target.value })} />
              )}
            </div>
          </div>
          <div className="field"><label htmlFor="title">What is it about?</label>
            <input id="title" className="input" placeholder={`e.g. ${TITLE_HINT[ev.kind] ?? "your event or notice"}`} value={ev.title} onChange={e => setE({ title: e.target.value })} />
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
          <div className="field"><span className="label">Provider</span>
            <div className="choices">
              {opts.providers.map(p => {
                const off = !p.caps.live.ready && !p.caps.voice.ready;
                return (
                  <button key={p.key} className={`choice${provider === p.key ? " on" : ""}`} disabled={off}
                    aria-pressed={provider === p.key} onClick={() => setProvider(p.key)}>
                    <b>{p.label}{off && <em>Not configured</em>}{p.key === opts.default_provider && !off && <em>Default</em>}</b>
                    <small>{p.sends}</small>
                    <small>{[p.caps.live.ready && "Live conversation", p.caps.voice.ready && "Makes the IVR audio",
                      p.caps.draft.ready ? "Writes scripts" : "Scripts written by Gemini"].filter(Boolean).join(" · ")}</small>
                  </button>
                );
              })}
            </div>
            <span className="hint">One provider runs the campaign. Add keys in .env to turn others on; see the Providers page.</span>
          </div>
          <div className="field"><span className="label">Mode</span>
            <div className="choices">
              <button className={`choice${hybrid ? " on" : ""}`} aria-pressed={hybrid} onClick={() => setMode("hybrid")}>
                <b>Hybrid{!voiceReady && <em>ElevenLabs not set up</em>}</b>
                <small>Pre-synthesised IVR (always ElevenLabs voice) with the keypad: greeting, message, menu and follow-up questions. It then asks if they
                  have any further questions, and the agent takes over only if they do. Cheapest per call.</small>
              </button>
              <button className={`choice${!hybrid ? " on" : ""}`} aria-pressed={!hybrid} onClick={() => setMode("live")}>
                <b>Live</b>
                <small>The agent takes over the whole call and talks it through with them. Most natural, costs the most.</small>
              </button>
            </div>
            <span className="hint">Calls ring the phone page (/phone): {opts.phones ? `${opts.phones} online now.` : "none online yet."}</span>
          </div>
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
          <p>{prov?.caps.draft.ready ? prov.label : "Gemini"} writes the greeting, message, keypad menu, voicemail and goodbye in{" "}
            {langs.map(langName).join(", ")}, the agent&apos;s system prompt, the follow-up questions this event needs (food preference,
            T-shirt size and so on, or none), and a retry policy. One call for the whole campaign; you review everything before launch.</p>
          {prov && !prov.caps.draft.ready && <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>{prov.label} cannot write text, so Gemini drafts and {prov.label} runs the calls.</p>}
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
                The event details, languages or closing question changed after this draft. Redraft, or edit the scripts by hand.{" "}
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
          <section className="card form">
            <div className="card-head"><h2>Agent system prompt</h2></div>
            <div className="field">
              <textarea id="sysprompt" className="input mono" rows={12} maxLength={6000} value={d.system_prompt} spellCheck={false}
                aria-label="Agent system prompt" onChange={e => editPrompt(e.target.value)} />
              <span className="hint">Written for this campaign. {prov?.label ?? "The provider"} gets it on every call, together with the event
                facts, the approved script in each person&apos;s language and the follow-up questions. {"{name}"} and {"{language}"} are filled in per person.
                Edit freely, or redraft to have it written again.</span>
            </div>
          </section>
          <QuestionsEditor questions={questions} onChange={setQuestions} escalation={asks}
            payment={ev.kind === "payment"} />
          <section className="card">
            <div className="card-head"><h2>{hybrid ? "IVR responses" : "Approved script"}</h2>
              <span className="muted" style={{ fontSize: 13 }}>{hybrid
                ? "Synthesised when you launch. Listen to any line to check how it sounds."
                : "The agent says these in its own words; it keeps the facts."}</span></div>
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
                  <div className="field" key={f}>
                    <label htmlFor={`s-${f}`} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      {FIELD[f][0]}{hybrid && <Listen disabled={!voiceReady || !d.scripts[tab][f].trim()} load={() => preview(tab, d.scripts[tab][f])} />}
                    </label>
                    <textarea id={`s-${f}`} className="input" rows={FIELD[f][2]} value={d.scripts[tab][f]}
                      onChange={e => editScript(tab, f, e.target.value)} lang={tab} />
                    {FIELD[f][1] && <span className="hint">{FIELD[f][1]}</span>}
                  </div>
                ))}
                {asks && (
                  <div className="field">
                    <label htmlFor="s-doubts" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      Closing question<Listen disabled={!voiceReady || !d.scripts[tab].doubts?.trim()} load={() => preview(tab, d.scripts[tab].doubts ?? "")} />
                    </label>
                    <textarea id="s-doubts" className="input" rows={2} lang={tab} value={d.scripts[tab].doubts ?? ""}
                      onChange={e => setDraft(p => p && { ...p, d: { ...p.d, scripts: { ...p.d.scripts, [tab]: { ...p.d.scripts[tab], doubts: e.target.value } } } })} />
                    <span className="hint">Asked last. A caller who starts speaking is connected to the assistant.
                      {!d.scripts[tab].doubts && " Redraft to have the AI write it in every language."}</span>
                  </div>
                )}
                {questions.map((q, i) => (
                  <div className="field" key={q.id}>
                    <label htmlFor={`s-${q.id}`} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      Question {i + 1}: {q.label || "untitled"}
                      {hybrid && <Listen disabled={!voiceReady || !d.scripts[tab].questions?.[q.id]?.trim()} load={() => preview(tab, d.scripts[tab].questions?.[q.id] ?? "")} />}
                    </label>
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
              <div className="card-head"><h2>{name}</h2><span className="chip">{KIND[ev.kind] || ev.kind}</span></div>
              <dl className="facts" style={{ margin: 16 }}>
                <div><dt>Recipients</dt><dd>{fmt.int(contacts.count)}</dd></div>
                <div><dt>Languages</dt><dd>{langs.map(langName).join(", ")}</dd></div>
                <div><dt>Retries</dt><dd>Up to {d.retry.max_attempts} attempts, {d.retry.gap_hours} h apart</dd></div>
                <div><dt>Expected answers</dt><dd>{fmt.int(estimate.expected_answered)} of {fmt.int(estimate.recipients)}</dd></div>
                <div><dt>Provider</dt><dd>{prov?.label}, {hybrid ? "hybrid" : "live"} mode</dd></div>
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
            {!opts.phones && (
              <Notice kind="info">No phone is online. Open <b>/phone</b> on a phone or another tab; the campaign waits for one.</Notice>
            )}
            {unusable && <Notice>{!prov ? "Pick a provider." : !prov.caps.live.ready ? `${prov.label} is not set up for live calls.`
              : `${prov.label} is fine for the call, but IVR audio is made with ElevenLabs: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID.`}</Notice>}
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
          <button className="btn primary" disabled={!reviewed || !estimate || unusable} onClick={launch}>
            <Icon name="phone" />Launch
          </button>
        )}
      </div>
    </main>
  );
}
