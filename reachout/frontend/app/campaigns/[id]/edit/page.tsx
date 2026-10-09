"use client";
import { useRouter } from "next/navigation";
import { use, useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { SCRIPT_FIELDS, type BuilderOptions, type Detail, type Mode, type ProviderKey, type RetryPolicy, type Script } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs, useShell } from "@/components/Shell";
import { ErrorView, Notice, PageHead, Skeleton } from "@/components/ui";

const LABEL = { greeting: "Greeting", message: "Message", menu: "Keypad menu", voicemail: "Voicemail", goodbye: "Goodbye" };

interface Form {
  name: string; provider: ProviderKey; mode: Mode; prompt: string; retry: RetryPolicy; scripts: Record<string, Script>;
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

  useEffect(() => {
    if (!d || f) return;
    setF({
      name: d.name, provider: d.provider, mode: d.mode, prompt: d.system_prompt, retry: d.retry_policy,
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
  const langName = (c: string) => d.scripts.find(s => s.code === c)?.language ?? c;
  const set = (patch: Partial<Form>) => setF(p => p && { ...p, ...patch });
  const setScript = (l: string, patch: Partial<Script>) => setF(p => p && { ...p, scripts: { ...p.scripts, [l]: { ...p.scripts[l], ...patch } } });
  const voiceReady = opts.providers.some(p => p.caps.voice.ready);
  const valid = f.name.trim() && f.prompt.trim() && Object.values(f.scripts).every(s => SCRIPT_FIELDS.every(k => s[k].trim()) && (!hybrid || s.doubts?.trim())) &&
    (!hybrid || voiceReady);
  // Switching to hybrid needs a closing question in every language: offer the standard English one to edit.
  const setMode = (mode: Mode) => setF(p => p && {
    ...p, mode, scripts: mode === "hybrid" ? Object.fromEntries(Object.entries(p.scripts).map(([l, sc]) => [l, { ...sc, doubts: sc.doubts || DOUBTS }])) : p.scripts,
  });

  const save = async () => {
    setBusy(true);
    try {
      await post(`/campaigns/${id}`, {
        name: f.name.trim(), provider: f.provider, mode: f.mode, system_prompt: f.prompt, retry: f.retry, scripts: f.scripts,
      }, "PATCH");
      toast("Campaign saved");
      router.push(`/campaigns/${id}`);
    } catch (e) { toast((e as Error).message, "error"); setBusy(false); }
  };

  const s = f.scripts[tab];
  return (
    <main className="view enter">
      <PageHead title={`Edit ${d.name}`} sub="Changes apply to calls placed from now on. Contacts, languages, questions and the mode are fixed." />
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
            {d.questions.map((q, i) => (
              <div className="field" key={q.id}><label htmlFor={`e-${q.id}`}>Question {i + 1}: {q.label}</label>
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
