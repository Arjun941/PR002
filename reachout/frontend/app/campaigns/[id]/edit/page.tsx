"use client";
import { useRouter } from "next/navigation";
import { use, useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { OUT } from "@/lib/format";
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
  questions: Question[]; // every follow-up question: existing ones can change too (saved answers follow their option's text)
}

export default function EditCampaignPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { toast, modal } = useShell();
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
  const addedKey = JSON.stringify((f?.questions ?? []).map(q => [q.id, q.label.trim(), q.options.map(o => o.trim()).filter(Boolean), Object.keys(f?.scripts ?? {})]));
  useEffect(() => {
    if (!f) return;
    const langs = Object.keys(f.scripts);
    const version = (q: Question) => JSON.stringify([q.label.trim(), q.options.map(o => o.trim()).filter(Boolean)]);
    const stale = f.questions.filter(q => q.label.trim() && q.options.filter(o => o.trim()).length >= MIN_OPTIONS && asked.current[q.id] !== version(q));
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
        const now = latest.current?.questions.find(x => x.id === q.id);
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
    const base = d.questions.map(({ id, label, options, only_if_confirmed }): Question => ({ id, label, options, only_if_confirmed }));
    // Existing questions count as already translated with their saved wording: only a real change retranslates, and the
    // saved wording is replaced only if it has not been edited by hand since.
    for (const q of base) {
      asked.current[q.id] = JSON.stringify([q.label.trim(), q.options.map(o => o.trim()).filter(Boolean)]);
      for (const sc of d.scripts) auto.current[`${q.id}:${sc.code}`] = sc.questions?.[q.id] ?? "";
    }
    setF({
      name: d.name, provider: d.provider, mode: d.mode, prompt: d.system_prompt, retry: d.retry_policy, questions: base,
      scripts: Object.fromEntries(d.scripts.map(s => [s.code, {
        ...Object.fromEntries(SCRIPT_FIELDS.map(k => [k, s[k]])), questions: s.questions ?? {}, doubts: s.doubts ?? "",
      } as Script])),
    });
    setTab(d.scripts[0]?.code ?? "");
  }, [d, f]);

  // After the languages change (saved straight away), bring the new scripts in and drop the removed ones.
  const [langSel, setLangSel] = useState<string[] | null>(null);
  useEffect(() => {
    if (!d || !f) return;
    const codes = d.scripts.map(x => x.code), have = Object.keys(f.scripts);
    if (codes.length === have.length && codes.every(c => have.includes(c))) return;
    setF(p => p && { ...p, scripts: Object.fromEntries(d.scripts.map(x => [x.code, p.scripts[x.code] ?? {
      ...Object.fromEntries(SCRIPT_FIELDS.map(k => [k, x[k]])), questions: x.questions ?? {}, doubts: x.doubts ?? "" } as Script])) });
    setLangSel(null);
    setTab(t => codes.includes(t) ? t : codes[0]);
  }, [d, f]);
  const [csv, setCsv] = useState("");
  const [find, setFind] = useState("");
  const [shown, setShown] = useState(25);
  const [contactBusy, setContactBusy] = useState("");

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!d || !f || !opts) return <main className="view"><Skeleton /></main>;

  const calling = d.status === "running" || d.status === "preparing";
  const hybrid = f.mode === "hybrid";
  const DOUBTS = "Do you have any other questions? Please ask now, or you may hang up if you have none.";
  const set = (patch: Partial<Form>) => setF(p => p && { ...p, ...patch });
  const setScript = (l: string, patch: Partial<Script>) => setF(p => p && { ...p, scripts: { ...p.scripts, [l]: { ...p.scripts[l], ...patch } } });
  const voiceReady = opts.providers.some(p => p.caps.voice.ready);
  const cleanQ = (q: Question): Question => ({ ...q, label: q.label.trim(), options: q.options.map(o => o.trim()).filter(Boolean) });
  const isNew = (qid: string) => !d.questions.some(x => x.id === qid);
  const changed = (q: Question) => {
    const o = d.questions.find(x => x.id === q.id);
    return !o || JSON.stringify([o.label, o.options, o.only_if_confirmed]) !== JSON.stringify([q.label.trim(), q.options.map(x => x.trim()).filter(Boolean), q.only_if_confirmed])
      || Object.entries(f.scripts).some(([l, sc]) => (sc.questions?.[q.id] ?? "") !== (d.scripts.find(x => x.code === l)?.questions?.[q.id] ?? ""));
  };
  const qOk = (q: Question) => !!q.label.trim() && q.options.filter(o => o.trim()).length >= MIN_OPTIONS
    && Object.values(f.scripts).every(s => (s.questions?.[q.id] ?? "").trim());
  const valid = f.name.trim() && f.prompt.trim() && Object.values(f.scripts).every(s => SCRIPT_FIELDS.every(k => s[k].trim()) && (!hybrid || s.doubts?.trim())) &&
    (!hybrid || voiceReady) && f.questions.every(qOk);
  // Switching to hybrid needs a closing question in every language: offer the standard English one to edit.
  const setMode = (mode: Mode) => setF(p => p && {
    ...p, mode, scripts: mode === "hybrid" ? Object.fromEntries(Object.entries(p.scripts).map(([l, sc]) => [l, { ...sc, doubts: sc.doubts || DOUBTS }])) : p.scripts,
  });

  const addQuestion = () => {
    const used = new Set(f.questions.map(q => q.id));
    const id = [...Array(used.size + 1)].map((_, n) => `q${n + 1}`).find(x => !used.has(x));
    if (!id) return;
    const q: Question = { id, label: "", options: ["", ""], only_if_confirmed: true };
    setF(p => p && { ...p, questions: [...p.questions, q], scripts: Object.fromEntries(Object.entries(p.scripts).map(([l, sc]) =>
      [l, { ...sc, questions: { ...sc.questions, [id]: "" } }])) });
  };
  const editQuestion = (qid: string, patch: Partial<Question>) =>
    setF(p => p && { ...p, questions: p.questions.map(q => q.id === qid ? { ...q, ...patch } : q) });  // the translation effect updates the wording
  const dropQuestion = (qid: string) => setF(p => p && { ...p, questions: p.questions.filter(q => q.id !== qid), scripts: Object.fromEntries(
    Object.entries(p.scripts).map(([l, sc]) => [l, { ...sc, questions: Object.fromEntries(Object.entries(sc.questions ?? {}).filter(([k]) => k !== qid)) }])) });
  const removeQuestion = (q: Question) => {
    const old = d.questions.find(x => x.id === q.id);
    if (!old) { dropQuestion(q.id); return; }
    modal({
      title: "Remove this question?", danger: true,
      body: <>“{old.label}” will no longer be asked{old.answered > 0 ? `, and its ${old.answered} saved answer${old.answered > 1 ? "s" : ""} will stop showing on the dashboard` : ""}. This is saved when you save the campaign.</>,
      confirmLabel: "Remove question", onConfirm: async () => { dropQuestion(q.id); },
    });
  };

  // Saves the follow-up questions and their wording right away (the rest of the form is saved with "Save changes").
  const [savingQ, setSavingQ] = useState("");
  const saveQuestion = async (q: Question) => {
    setSavingQ(q.id);
    try {
      // Everything else stays as saved: only the questions and their spoken wording go.
      const scripts = Object.fromEntries(d.scripts.map(sc => [sc.code, {
        ...Object.fromEntries(SCRIPT_FIELDS.map(k => [k, sc[k]])), doubts: sc.doubts ?? "",
        questions: Object.fromEntries(f.questions.map(x => [x.id, f.scripts[sc.code]?.questions?.[x.id] ?? ""])),
      }]));
      const r = await post<{ dropped_answers?: number }>(`/campaigns/${id}`, {
        questions: f.questions.map(cleanQ), scripts }, "PATCH");
      toast(r.dropped_answers ? `Question saved; ${r.dropped_answers} saved answer(s) pointed at removed options and were cleared` : "Question saved");
      retry();
    } catch (e) { toast((e as Error).message, "error"); }
    finally { setSavingQ(""); }
  };

  // Languages and contacts are saved straight away, like a question's Save button.
  const codes = d.scripts.map(x => x.code);
  const sel = langSel ?? codes;
  const nameToCode = (n: string) => opts.languages.find(l => l.name === n)?.code ?? n;
  const inUse = (code: string) => d.recipients.filter(r => nameToCode(r.language) === code).length;
  const blocked = codes.filter(c => !sel.includes(c) && inUse(c) > 0);
  const langsChanged = sel.length !== codes.length || sel.some(c => !codes.includes(c));
  const applyLanguages = async () => {
    setContactBusy("languages");
    try {
      const r = await post<{ warnings?: string[] }>(`/campaigns/${id}`, { languages: sel }, "PATCH");
      toast(sel.some(c => !codes.includes(c)) ? "Languages saved: the scripts were translated into the new ones. Check each language tab." : "Languages saved");
      (r.warnings ?? []).filter(w => w.startsWith("No usable")).forEach(w => toast(w, "info"));
      retry();
    } catch (e) { toast((e as Error).message, "error"); }
    finally { setContactBusy(""); }
  };
  const changeContacts = async (body: { add_csv?: string; remove?: string[]; update?: { id: string; name?: string; language?: string; segment?: string }[] }, ok = "Contacts saved") => {
    setContactBusy("contacts");
    try {
      const r = await post<{ added: number; skipped: { error: string }[]; total: number }>(`/campaigns/${id}/contacts`, body);
      toast(body.add_csv ? `${r.added} contact${r.added === 1 ? "" : "s"} added${r.skipped.length ? `, ${r.skipped.length} skipped (${r.skipped[0].error})` : ""}` : ok);
      if (body.add_csv) setCsv("");
      retry();
    } catch (e) { toast((e as Error).message, "error"); }
    finally { setContactBusy(""); }
  };
  const removeContact = (r: { id: string; name: string }) => modal({
    title: "Remove this contact?", danger: true,
    body: <>{r.name} will be removed from the campaign, with their call history and any recording of their calls. This cannot be undone.</>,
    confirmLabel: "Remove contact", onConfirm: () => changeContacts({ remove: [r.id] }, "Contact removed"),
  });
  const needle = find.trim().toLowerCase();
  const people = d.recipients.filter(r => !needle || r.name.toLowerCase().includes(needle) || r.segment.toLowerCase().includes(needle));

  const save = async () => {
    setBusy(true);
    try {
      await post(`/campaigns/${id}`, {
        name: f.name.trim(), provider: f.provider, mode: f.mode, system_prompt: f.prompt, retry: f.retry, scripts: f.scripts,
        questions: f.questions.map(cleanQ),
      }, "PATCH");
      toast("Campaign saved");
      router.push(`/campaigns/${id}`);
    } catch (e) { toast((e as Error).message, "error"); setBusy(false); }
  };

  const s = f.scripts[tab];
  return (
    <main className="view enter">
      <PageHead title={`Edit ${d.name}`} sub="Changes apply to calls placed from now on. Everything can change: languages, contacts and follow-up questions are saved as you go; the rest with Save changes." />
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
        <div className="card-head"><h2>Languages</h2>
          <button className="btn sm primary" disabled={!langsChanged || !sel.length || blocked.length > 0 || contactBusy !== "" || calling} onClick={() => void applyLanguages()}>
            {contactBusy === "languages" ? <><span className="spinner" />Translating</> : <><Icon name="check" size={12} />Apply languages</>}
          </button></div>
        <div className="field">
          <div className="toggles" role="group" aria-label="Languages">
            {opts.languages.map(l => (
              <button key={l.code} type="button" className={`toggle${sel.includes(l.code) ? " on" : ""}`} aria-pressed={sel.includes(l.code)}
                onClick={() => setLangSel(sel.includes(l.code) ? sel.filter(x => x !== l.code) : [...sel, l.code])}>{l.name}</button>
            ))}
          </div>
          <span className="hint">
            {sel.some(c => !codes.includes(c)) && "A new language gets the whole script translated automatically, including the questions: check it in its tab. "}
            {blocked.map(c => `${inUse(c)} contact${inUse(c) > 1 ? "s" : ""} still hear ${opts.languages.find(l => l.code === c)?.name}: change their language below first. `)}
            Saved straight away when you press Apply.
          </span>
        </div>
      </section>

      <section className="card form">
        <div className="card-head"><h2>Contacts ({d.recipients.length})</h2></div>
        <div className="field">
          <label htmlFor="add-csv">Add contacts</label>
          <textarea id="add-csv" className="input mono" rows={4} spellCheck={false} value={csv} onChange={e => setCsv(e.target.value)}
            placeholder={"name, phone, language, segment\nAsha Kulkarni, 98765 43210, hi, Class 5"} />
          <div className="toggles">
            <button className="btn sm primary" disabled={!csv.trim() || contactBusy !== "" || calling} onClick={() => void changeContacts({ add_csv: csv })}>
              {contactBusy === "contacts" ? <span className="spinner" /> : <Icon name="plus" size={12} />}Add contacts</button>
            <label className="btn sm"><Icon name="upload" size={12} />Upload CSV
              <input type="file" accept=".csv,text/csv,text/plain" hidden onChange={async e => { const f0 = e.target.files?.[0]; if (f0) setCsv(await f0.text()); e.target.value = ""; }} /></label>
          </div>
          <span className="hint">Name and phone; language ({codes.join(", ")}) and segment are optional. Numbers already in the campaign are skipped.</span>
        </div>
        <div className="field">
          <input className="input" placeholder="Search by name or segment" aria-label="Search contacts" value={find} onChange={e => { setFind(e.target.value); setShown(25); }} />
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Name</th><th>Phone</th><th>Language</th><th>Segment</th><th>Status</th><th><span className="sr-only">Remove</span></th></tr></thead>
            <tbody>
              {people.slice(0, shown).map(r => (
                <tr key={r.id}>
                  <td><input key={r.name} className="input" aria-label={`Name of ${r.name}`} defaultValue={r.name} maxLength={60} disabled={calling}
                    onBlur={e => { const v = e.target.value.trim(); if (v && v !== r.name) void changeContacts({ update: [{ id: r.id, name: v }] }); }} /></td>
                  <td className="mono muted">{r.phone}</td>
                  <td><select className="select" aria-label={`Language of ${r.name}`} value={nameToCode(r.language)} disabled={calling || contactBusy !== ""}
                    onChange={e => void changeContacts({ update: [{ id: r.id, language: e.target.value }] })}>
                    {codes.map(c => <option key={c} value={c}>{opts.languages.find(l => l.code === c)?.name ?? c}</option>)}</select></td>
                  <td><input key={r.segment} className="input" aria-label={`Segment of ${r.name}`} defaultValue={r.segment} maxLength={40} disabled={calling}
                    onBlur={e => { const v = e.target.value.trim(); if (v && v !== r.segment) void changeContacts({ update: [{ id: r.id, segment: v }] }); }} /></td>
                  <td className="muted">{OUT[r.outcome]}</td>
                  <td className="num"><button className="btn sm ghost" aria-label={`Remove ${r.name}`} disabled={calling || contactBusy !== ""} onClick={() => removeContact(r)}><Icon name="trash" size={12} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="table-foot"><span>Showing {Math.min(shown, people.length)} of {people.length}</span>
          {people.length > shown && <button className="btn" onClick={() => setShown(n => n + 25)}>Show 25 more</button>}</div>
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
          Asked after the main 1, 2 or 3 answer; each option is a key the person presses. Changing a question&apos;s wording or options
          updates its spoken text in every language (check each language tab below). Answers people already gave stay matched to their
          option by its text; an answer whose option you delete is cleared.
        </p>
        {f.questions.map((q, n) => {
          const old = d.questions.find(x => x.id === q.id);
          return (
            <fieldset key={q.id} className="card" style={{ padding: 12, margin: "0 16px 12px" }}>
              <div className="row-2">
                <div className="field"><label htmlFor={`nq-${q.id}`}>{old ? `Question ${n + 1}` : "New question"}</label>
                  <input id={`nq-${q.id}`} className="input" maxLength={60} placeholder="e.g. T-shirt size" value={q.label}
                    onChange={e => editQuestion(q.id, { label: e.target.value })} /></div>
                <div className="field"><span className="label">Ask</span>
                  <label className="check"><input type="checkbox" checked={q.only_if_confirmed}
                    onChange={e => editQuestion(q.id, { only_if_confirmed: e.target.checked })} /><span>Only people who confirmed</span></label></div>
              </div>
              <div className="field"><span className="label">Options (keys 1, 2, ...){old && old.answered > 0 && ` · ${old.answered} answered`}</span>
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
              <div className="toggles">
                <button className="btn sm primary" type="button" disabled={!qOk(q) || !changed(q) || savingQ !== "" || calling || translating}
                  onClick={() => void saveQuestion(q)} title={calling ? "Pause the campaign first" : !changed(q) ? "Nothing to save" : ""}>
                  {savingQ === q.id ? <span className="spinner" /> : <Icon name="check" size={12} />}Save question</button>
                <button className="btn sm ghost" type="button" onClick={() => removeQuestion(q)}><Icon name="trash" size={12} />Remove question</button>
              </div>
            </fieldset>
          );
        })}
        <div style={{ padding: "0 16px 16px" }}>
          <button className="btn" type="button" disabled={f.questions.length >= MAX_QUESTIONS} onClick={addQuestion}>
            <Icon name="plus" size={14} />Add a question
          </button>
          {f.questions.length >= MAX_QUESTIONS && <span className="hint"> At most {MAX_QUESTIONS} questions.</span>}
          {Object.keys(f.scripts).length > 1 && (
            <div className="stack-gap"><Notice kind="info">Changed or new questions are translated into every language automatically{translating ? " (translating…)" : ""}. Check the wording in each language tab below, and fix anything that sounds wrong.</Notice></div>)}
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
            {f.questions.map((q, i) => (
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
