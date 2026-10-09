"use client";
import { useRouter } from "next/navigation";
import { use, useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import {
  MAX_OPTIONS, MAX_QUESTIONS, MIN_OPTIONS, questionText, SCRIPT_FIELDS,
  type BuilderOptions, type Detail, type Mode, type ProviderKey, type Question, type RetryPolicy, type Script,
} from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs, useShell } from "@/components/Shell";
import { ErrorView, Notice, PageHead, Skeleton } from "@/components/ui";

const LABEL = { greeting: "Greeting", message: "Message", menu: "Keypad menu", voicemail: "Voicemail", goodbye: "Goodbye" };

interface Form {
  name: string; provider: ProviderKey; mode: Mode; prompt: string; retry: RetryPolicy; scripts: Record<string, Script>;
  added: Question[]; // new follow-up questions (existing ones cannot change: answers are saved against their options)
}

export default function EditCampaignPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { toast } = useShell();
  const { data: d, error, retry } = useData(() => api<Detail>(`/campaigns/${id}`), [id]);
  const { data: opts } = useData(() => api<BuilderOptions>("/builder/options"), []);
  const [f, setF] = useState<Form | null>(null);
  const [tab, setTab] = useState("");
  const [busy, setBusy] = useState(false);

  useCrumbs([["Campaigns", "/campaigns"], [d?.name ?? "Campaign", `/campaigns/${id}`], ["Edit"]]);
  const langName = (c: string) => d?.scripts.find(x => x.code === c)?.language ?? c;

  // A new question is translated into every language of the campaign once it has a label and options. Text that was
  // typed by hand in a language is left alone; only empty or machine-filled text is replaced.
  const auto = useRef<Record<string, string>>({});   // `${question id}:${language}` -> the text we last filled in
  const asked = useRef<Record<string, string>>({});  // question id -> the version we last translated
  const latest = useRef<Form | null>(null);
  latest.current = f;
  const [translating, setTranslating] = useState(false);
  const addedKey = JSON.stringify((f?.added ?? []).map(q => [q.id, q.label.trim(), q.options.map(o => o.trim()).filter(Boolean), Object.keys(f?.scripts ?? {})]));
  useEffect(() => {
    if (!f) return;
    const langs = Object.keys(f.scripts);
    const version = (q: Question) => JSON.stringify([q.label.trim(), q.options.map(o => o.trim()).filter(Boolean)]);
    const stale = f.added.filter(q => q.label.trim() && q.options.filter(o => o.trim()).length >= MIN_OPTIONS && asked.current[q.id] !== version(q));
    if (!stale.length) return;
    const timer = setTimeout(async () => {
      setTranslating(true);
      for (const q of stale) {
        const cq: Question = { ...q, label: q.label.trim(), options: q.options.map(o => o.trim()).filter(Boolean) };
        const v = version(q);
        asked.current[q.id] = v;
        let texts: Record<string, string> = {}, english: string[] = [];
        try {
          const r = await post<{ texts: Record<string, string>; english_for: string[] }>("/builder/translate-question",
            { label: cq.label, options: cq.options, languages: langs });
          texts = r.texts; english = r.english_for;
        } catch { english = langs.filter(l => l !== "en"); }
        const now = latest.current?.added.find(x => x.id === q.id);
        if (!now || version(now) !== v) continue;  // edited or removed while translating: a newer run takes over
        setF(prev => prev && { ...prev, scripts: Object.fromEntries(Object.entries(prev.scripts).map(([l, sc]) => {
          const cur = sc.questions?.[q.id] ?? "";
          if (cur.trim() && cur !== auto.current[`${q.id}:${l}`]) return [l, sc];
          const text = texts[l] || questionText(cq);
          auto.current[`${q.id}:${l}`] = text;
          return [l, { ...sc, questions: { ...sc.questions, [q.id]: text } }];
        })) });
        if (english.length) toast(`“${cq.label}”: no translation for ${english.map(l => langName(l)).join(", ")}; the English wording is used there. Edit it in that language's tab.`, "info");
      }
      setTranslating(false);
    }, 900);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addedKey]);

  useEffect(() => {
    if (!d || f) return;
    setF({
      name: d.name, provider: d.provider, mode: d.mode, prompt: d.system_prompt, retry: d.retry_policy, added: [],
      scripts: Object.fromEntries(d.scripts.map(s => [s.code, {
        ...Object.fromEntries(SCRIPT_FIELDS.map(k => [k, s[k]])), questions: s.questions ?? {}, doubts: s.doubts ?? "",
      } as Script])),
    });
    setTab(d.scripts[0]?.code ?? "");
  }, [d, f]);

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!d || !f || !opts) return <main className="view"><Skeleton /></main>;

  const calling = d.status === "running" || d.status === "preparing";
  const hybrid = f.mode === "hybrid";
  const DOUBTS = "Do you have any other questions? Please ask now, or you may hang up if you have none.";
  const set = (patch: Partial<Form>) => setF(p => p && { ...p, ...patch });
  const setScript = (l: string, patch: Partial<Script>) => setF(p => p && { ...p, scripts: { ...p.scripts, [l]: { ...p.scripts[l], ...patch } } });
  const voiceReady = opts.providers.some(p => p.caps.voice.ready);
  const cleanQ = (q: Question): Question => ({ ...q, label: q.label.trim(), options: q.options.map(o => o.trim()).filter(Boolean) });
  const qOk = (q: Question) => !!q.label.trim() && q.options.filter(o => o.trim()).length >= MIN_OPTIONS
    && Object.values(f.scripts).every(s => (s.questions?.[q.id] ?? "").trim());
  const valid = f.name.trim() && f.prompt.trim() && Object.values(f.scripts).every(s => SCRIPT_FIELDS.every(k => s[k].trim()) && (!hybrid || s.doubts?.trim())) &&
    (!hybrid || voiceReady) && f.added.every(qOk);
  // Switching to hybrid needs a closing question in every language: offer the standard English one to edit.
  const setMode = (mode: Mode) => setF(p => p && {
    ...p, mode, scripts: mode === "hybrid" ? Object.fromEntries(Object.entries(p.scripts).map(([l, sc]) => [l, { ...sc, doubts: sc.doubts || DOUBTS }])) : p.scripts,
  });

  const addQuestion = () => {
    const used = new Set([...d.questions, ...f.added].map(q => q.id));
    const id = ["q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8", "q9"].find(x => !used.has(x));
    if (!id) return;
    const q: Question = { id, label: "", options: ["", ""], only_if_confirmed: true };
    setF(p => p && { ...p, added: [...p.added, q], scripts: Object.fromEntries(Object.entries(p.scripts).map(([l, sc]) =>
      [l, { ...sc, questions: { ...sc.questions, [id]: "" } }])) });
  };
  const editQuestion = (id: string, patch: Partial<Question>) =>
    setF(p => p && { ...p, added: p.added.map(q => q.id === id ? { ...q, ...patch } : q) });  // the translation effect fills the texts
  const removeQuestion = (id: string) => setF(p => p && { ...p, added: p.added.filter(q => q.id !== id), scripts: Object.fromEntries(
    Object.entries(p.scripts).map(([l, sc]) => [l, { ...sc, questions: Object.fromEntries(Object.entries(sc.questions ?? {}).filter(([k]) => k !== id)) }])) });

  const save = async () => {
    setBusy(true);
    try {
      await post(`/campaigns/${id}`, {
        name: f.name.trim(), provider: f.provider, mode: f.mode, system_prompt: f.prompt, retry: f.retry, scripts: f.scripts,
        ...(f.added.length ? { questions: [...d.questions.map(({ id, label, options, only_if_confirmed }) => ({ id, label, options, only_if_confirmed })), ...f.added.map(cleanQ)] } : {}),
      }, "PATCH");
      toast("Campaign saved");
      router.push(`/campaigns/${id}`);
    } catch (e) { toast((e as Error).message, "error"); setBusy(false); }
  };

  const s = f.scripts[tab];
  return (
    <main className="view enter">
      <PageHead title={`Edit ${d.name}`} sub="Changes apply to calls placed from now on. Contacts, languages and the mode are fixed; you can add follow-up questions." />
      {calling && <div className="stack-gap"><Notice>Pause the campaign before saving changes.</Notice></div>}
      {hybrid && <div className="stack-gap"><Notice kind="info">Hybrid IVR audio is made with ElevenLabs: new or changed lines are synthesised before calling resumes (the campaign shows “Preparing”).{f.mode !== d.mode && " Check the closing question in every language."}</Notice></div>}

      <section className="card form">
        <div className="row-2">
          <div className="field"><label htmlFor="e-name">Campaign name</label>
            <input id="e-name" className="input" maxLength={80} value={f.name} onChange={e => set({ name: e.target.value })} /></div>
          <div className="field"><label htmlFor="e-prov">Provider</label>
            <select id="e-prov" className="select" value={f.provider} onChange={e => set({ provider: e.target.value as ProviderKey })}>
              {opts.providers.map(p => <option key={p.key} value={p.key} disabled={!p.caps.live.ready && !p.caps.voice.ready}>{p.label}</option>)}
            </select></div>
        </div>
        <div className="field"><span className="label">Mode</span>
          <div className="choices">
            <button className={`choice${hybrid ? " on" : ""}`} aria-pressed={hybrid} disabled={!voiceReady && d.mode !== "hybrid"} onClick={() => setMode("hybrid")}>
              <b>Hybrid{!voiceReady && <em>ElevenLabs not set up</em>}</b>
              <small>Pre-synthesised IVR with the keypad, then the agent for anyone with a question at the end.</small>
            </button>
            <button className={`choice${!hybrid ? " on" : ""}`} aria-pressed={!hybrid} onClick={() => setMode("live")}>
              <b>Live</b><small>The agent takes over the whole call.</small>
            </button>
          </div>
        </div>
        <div className="row-2">
          <div className="field"><label htmlFor="e-att">Attempts per person</label>
            <select id="e-att" className="select" value={f.retry.max_attempts} onChange={e => set({ retry: { ...f.retry, max_attempts: +e.target.value } })}>
              {[1, 2, 3, 4].map(n => <option key={n} value={n}>{n}</option>)}</select></div>
          <div className="field"><label htmlFor="e-gap">Hours between tries</label>
            <input id="e-gap" className="input" type="number" min={1} max={48} value={f.retry.gap_hours}
              onChange={e => set({ retry: { ...f.retry, gap_hours: Math.max(1, Math.min(48, +e.target.value || 1)) } })} /></div>
        </div>
      </section>

      <section className="card form">
        <div className="card-head"><h2>Agent system prompt</h2></div>
        <div className="field">
          <textarea className="input mono" rows={12} maxLength={6000} spellCheck={false} aria-label="Agent system prompt"
            value={f.prompt} onChange={e => set({ prompt: e.target.value })} />
          <span className="hint">{"{name}"} and {"{language}"} are filled in per person.</span>
        </div>
      </section>

      <section className="card form">
        <div className="card-head"><h2>Follow-up questions</h2></div>
        <p className="hint" style={{ padding: "0 16px" }}>
          Asked after the main 1, 2 or 3 answer; each option is a key the person presses. Existing questions cannot be changed because
          their answers are already saved against the options. Every question&apos;s spoken wording is in the language tabs below.
        </p>
        {d.questions.map(q => (
          <div className="field" key={q.id}><span className="label">{q.label}</span>
            <span className="hint">{q.options.map((o, i) => `${i + 1} = ${o}`).join(" · ")}{q.answered > 0 && ` · ${q.answered} answered`}</span></div>
        ))}
        {f.added.map(q => (
          <fieldset key={q.id} className="card" style={{ padding: 12, margin: "0 16px 12px" }}>
            <div className="row-2">
              <div className="field"><label htmlFor={`nq-${q.id}`}>New question</label>
                <input id={`nq-${q.id}`} className="input" maxLength={60} placeholder="e.g. T-shirt size" value={q.label}
                  onChange={e => editQuestion(q.id, { label: e.target.value })} /></div>
              <div className="field"><span className="label">Ask</span>
                <label className="check"><input type="checkbox" checked={q.only_if_confirmed}
                  onChange={e => editQuestion(q.id, { only_if_confirmed: e.target.checked })} /><span>Only people who confirmed</span></label></div>
            </div>
            <div className="field"><span className="label">Options (keys 1, 2, ...)</span>
              {q.options.map((o, i) => (
                <div key={i} className="toggles" style={{ marginBottom: 6 }}>
                  <span className="mono muted" style={{ width: 20 }}>{i + 1}</span>
                  <input className="input" aria-label={`Option ${i + 1}`} maxLength={40} value={o} style={{ flex: 1 }}
                    onChange={e => editQuestion(q.id, { options: q.options.map((x, j) => j === i ? e.target.value : x) })} />
                  {q.options.length > MIN_OPTIONS && (
                    <button className="btn sm ghost" type="button" aria-label={`Remove option ${i + 1}`}
                      onClick={() => editQuestion(q.id, { options: q.options.filter((_, j) => j !== i) })}><Icon name="x" size={12} /></button>)}
                </div>
              ))}
              {q.options.length < MAX_OPTIONS && <button className="btn sm" type="button" onClick={() => editQuestion(q.id, { options: [...q.options, ""] })}>
                <Icon name="plus" size={12} />Add option</button>}
            </div>
            <button className="btn sm ghost" type="button" onClick={() => removeQuestion(q.id)}><Icon name="trash" size={12} />Remove this new question</button>
          </fieldset>
        ))}
        <div style={{ padding: "0 16px 16px" }}>
          <button className="btn" type="button" disabled={d.questions.length + f.added.length >= MAX_QUESTIONS} onClick={addQuestion}>
            <Icon name="plus" size={14} />Add a question
          </button>
          {d.questions.length + f.added.length >= MAX_QUESTIONS && <span className="hint"> At most {MAX_QUESTIONS} questions.</span>}
          {!!f.added.length && Object.keys(f.scripts).length > 1 && (
            <div className="stack-gap"><Notice kind="info">Each new question is translated into every language automatically{translating ? " (translating…)" : ""}. Check the wording in each language tab below, and fix anything that sounds wrong.</Notice></div>)}
        </div>
      </section>

      <section className="card">
        <div className="tabs" role="tablist">
          {Object.keys(f.scripts).map(l => (
            <button key={l} role="tab" aria-selected={tab === l} className={tab === l ? "on" : ""} onClick={() => setTab(l)}>{langName(l)}</button>
          ))}
        </div>
        {s && (
          <div className="form">
            {SCRIPT_FIELDS.map(k => (
              <div className="field" key={k}><label htmlFor={`e-${k}`}>{LABEL[k]}</label>
                <textarea id={`e-${k}`} className="input" rows={k === "message" ? 3 : 2} lang={tab} value={s[k]}
                  onChange={e => setScript(tab, { [k]: e.target.value })} /></div>
            ))}
            {hybrid && (
              <div className="field"><label htmlFor="e-doubts">Closing question</label>
                <textarea id="e-doubts" className="input" rows={2} lang={tab} value={s.doubts ?? ""} onChange={e => setScript(tab, { doubts: e.target.value })} /></div>
            )}
            {[...d.questions, ...f.added].map((q, i) => (
              <div className="field" key={q.id}><label htmlFor={`e-${q.id}`}>Question {i + 1}: {q.label || "(new)"}</label>
                <textarea id={`e-${q.id}`} className="input" rows={2} lang={tab} value={s.questions?.[q.id] ?? ""}
                  onChange={e => setScript(tab, { questions: { ...s.questions, [q.id]: e.target.value } })} /></div>
            ))}
          </div>
        )}
      </section>

      <div className="card builder-foot" style={{ borderTop: 0 }}>
        <button className="btn" onClick={() => router.push(`/campaigns/${id}`)}>Cancel</button>
        <button className="btn primary" disabled={!valid || busy || calling} onClick={() => void save()}>
          {busy ? <span className="spinner" /> : <Icon name="check" />}Save changes
        </button>
      </div>
    </main>
  );
}
