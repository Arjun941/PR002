"use client";
import { useRouter } from "next/navigation";
import { use, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { ago, CHANNEL, fmt, KIND, ORDER, OUT } from "@/lib/format";
import { SCRIPT_FIELDS, type Detail, type Recipient } from "@/lib/types";
import { useData, usePoll } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs, useShell } from "@/components/Shell";
import { Breakdown, ErrorView, HandlingCard, Notice, OutcomePill, Skeleton, Stat, StatusPill } from "@/components/ui";

const NON_RESPONDER = ["voicemail", "no_answer"];
const FIELD_LABEL = { greeting: "Greeting", message: "Message", menu: "Keypad menu", voicemail: "Voicemail", goodbye: "Goodbye" };

function Scripts({ d }: { d: Detail }) {
  const [tab, setTab] = useState(0);
  const s = d.scripts[tab];
  return (
    <section className="card">
      <div className="card-head"><h2>What recipients hear</h2>
        <span className="muted" style={{ fontSize: 13 }}>Up to {d.retry_policy.max_attempts} attempts, {d.retry_policy.gap_hours} h apart</span>
      </div>
      <div className="tabs" role="tablist">
        {d.scripts.map((x, i) => (
          <button key={x.language} role="tab" aria-selected={tab === i} className={tab === i ? "on" : ""} onClick={() => setTab(i)}>{x.language}</button>
        ))}
      </div>
      <div className="script"><dl>
        {SCRIPT_FIELDS.map(f => <div key={f}><dt>{FIELD_LABEL[f]}</dt><dd>{s[f]}</dd></div>)}
        {d.questions.map((q, i) => <div key={q.id}><dt>Question {i + 1}</dt><dd>{s.questions?.[q.id]}</dd></div>)}
        {s.doubts && <div><dt>Closing question</dt><dd>{s.doubts}</dd></div>}
      </dl></div>
    </section>
  );
}

/** Follow-up answers: how many people pressed each option. */
function Answers({ d }: { d: Detail }) {
  return (
    <section className="card">
      <div className="card-head"><h2>Answers to follow-up questions</h2></div>
      <div className="answers">
        {d.questions.map(q => (
          <div className="answer" key={q.id}>
            <div className="answer-head"><b>{q.label}</b>
              <span className="muted">{fmt.int(q.answered)} answered{q.only_if_confirmed ? " · asked to people who confirmed" : ""}</span>
            </div>
            {q.results.map(o => (
              <div className="arow" key={o.key}>
                <span className="alabel"><kbd>{o.key}</kbd>{o.label}</span>
                <span className="abar"><i style={{ width: `${q.answered ? (o.count / q.answered) * 100 : 0}%` }} /></span>
                <span className="aval">{fmt.int(o.count)}<small>{q.answered ? fmt.pct(o.count / q.answered) : "–"}</small></span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}

export default function CampaignDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { toast, modal } = useShell();
  const router = useRouter();
  const { data: d, setData, error, retry } = useData(() => api<Detail>(`/campaigns/${id}`), [id]);
  const [f, setF] = useState({ q: "", outcome: "all", language: "all", segment: "all", limit: 50 });
  const [playing, setPlaying] = useState<Recipient | null>(null);
  const pinRef = useRef<HTMLInputElement>(null);

  useCrumbs([["Campaigns", "/campaigns"], [d?.name ?? "Campaign"]]);
  const refresh = async () => { try { setData(await api<Detail>(`/campaigns/${id}`)); } catch { /* keep last good view */ } };
  usePoll(refresh, 2500, !!d && (d.status === "running" || d.status === "preparing" || d.totals.retrying > 0));

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!d) return <main className="view"><Skeleton /></main>;

  const setFilter = (patch: Partial<typeof f>) => setF(prev => ({ ...prev, limit: 50, ...patch }));
  const needle = f.q.trim().toLowerCase();
  const rows = d.recipients.filter(r =>
    (f.outcome === "all" || (f.outcome === "nonresp" ? NON_RESPONDER.includes(r.outcome) : r.outcome === f.outcome)) &&
    (f.language === "all" || r.language === f.language) &&
    (f.segment === "all" || r.segment === f.segment) &&
    (!needle || r.name.toLowerCase().includes(needle)));
  const shown = rows.slice(0, f.limit);

  const s = d.totals, n = s.retryable, busy = s.retrying > 0;

  const openRetry = () => modal({
    title: "Retry non-responders",
    body: (
      <>
        Recipients who reached voicemail or did not pick up will be called again with the same script, in their own language.
        {d.status === "paused" && " The campaign is paused, so calls start when you resume it."}
        <dl className="facts">
          <div><dt>Recipients</dt><dd>{fmt.int(n)}</dd></div>
          <div><dt>Made up of</dt><dd>{fmt.int(s.counts.voicemail)} voicemail, {fmt.int(s.counts.no_answer)} no answer</dd></div>
          <div><dt>Estimated cost</dt><dd>{fmt.inr2(d.retry_estimate_inr)}</dd></div>
        </dl>
      </>
    ),
    confirmLabel: `Call ${fmt.int(n)} again`,
    onConfirm: async () => {
      const res = await post<{ queued: number }>(`/campaigns/${id}/retry`);
      toast(`Retrying ${fmt.int(res.queued)} recipients`);
      await refresh();
    },
  });

  const calling = d.status === "running" || d.status === "preparing";
  const openDelete = () => modal({
    title: "Delete this campaign?",
    danger: true,
    body: (
      <>
        <b style={{ color: "var(--text)" }}>{d.name}</b> will be removed for good: its contacts, outcomes, answers and call log.
        This cannot be undone.
        <dl className="facts">
          <div><dt>Recipients</dt><dd>{fmt.int(d.totals.recipients)}</dd></div>
          <div><dt>Calls placed</dt><dd>{fmt.int(d.totals.calls_placed)}</dd></div>
        </dl>
        <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>Call recordings stay with Exotel until their own retention removes them.</p>
      </>
    ),
    confirmLabel: "Delete campaign",
    onConfirm: async () => {
      await post(`/campaigns/${id}`, undefined, "DELETE");
      toast(`Deleted “${d.name}”`);
      router.push("/campaigns");
    },
  });

  const setStatus = async (status: "running" | "paused") => {
    try {
      await post(`/campaigns/${id}/status`, { status });
      toast(status === "paused" ? "Paused. Calls already ringing will finish." : "Resumed");
      await refresh();
    } catch (e) { toast((e as Error).message, "error"); }
  };

  // Recordings are personal data: playback needs a PIN-unlocked session, and the server logs every play.
  const play = async (r: Recipient) => {
    let access: { enabled: boolean; unlocked: boolean };
    try { access = await api("/auth/recordings"); } catch (e) { toast((e as Error).message, "error"); return; }
    if (!access.enabled) { toast("Recording playback is off. Set RECORDINGS_PIN on the server to turn it on.", "info"); return; }
    if (access.unlocked) { setPlaying(r); return; }
    modal({
      title: "Unlock recordings",
      body: (
        <>
          Call recordings are personal data. Enter the recordings PIN to listen for the next 15 minutes. Each play is logged.
          <input ref={pinRef} className="input" type="password" inputMode="numeric" autoComplete="off" autoFocus
            aria-label="Recordings PIN" placeholder="PIN" style={{ width: "100%", marginTop: 14 }} />
        </>
      ),
      confirmLabel: "Unlock and play",
      onConfirm: async () => {
        await post("/auth/recordings", { pin: pinRef.current?.value ?? "" });
        setPlaying(r);
      },
    });
  };
  const lock = async () => {
    setPlaying(null);
    try { await post("/auth/recordings", undefined, "DELETE"); toast("Recordings locked"); } catch { /* cookie expires anyway */ }
  };

  return (
    <main className="view enter">
      <div className="page-head">
        <div>
          <h1>{d.name}</h1>
          <div className="meta">
            <StatusPill s={d.status} /><span className="chip">{KIND[d.kind] || d.kind}</span>
            {d.simulated && <span className="chip" title="Demo mode: no real calls were placed">Simulated</span>}
            {d.languages.map(l => <span key={l} className="chip">{l}</span>)}
            <span className="muted">Started {ago(d.started_at)}</span>
          </div>
        </div>
        <div className="actions" style={{ display: "flex", gap: 8 }}>
          {calling && <button className="btn" onClick={() => void setStatus("paused")}><Icon name="pause" />Pause</button>}
          {d.status === "paused" && <button className="btn" onClick={() => void setStatus("running")}><Icon name="play" />Resume</button>}
          <button className="btn primary" disabled={n === 0 || busy} onClick={openRetry}
            title={n === 0 && !busy ? "No one is waiting for a retry" : ""}>
            {busy ? <span className="spinner" /> : <Icon name="refresh" />}
            {busy ? `Retrying ${fmt.int(s.retrying)}` : "Retry non-responders"}
            {!busy && n > 0 && <span className="count">{fmt.int(n)}</span>}
          </button>
          <button className="btn danger" disabled={calling} onClick={openDelete} aria-label="Delete campaign"
            title={calling ? "Pause the campaign before deleting it" : "Delete campaign"}><Icon name="trash" /></button>
        </div>
      </div>

      <section className="grid-4">
        <Stat label="Calls placed" value={s.calls_placed} kind="int" animate={false} sub={`to ${fmt.int(s.contacted)} of ${fmt.int(s.recipients)} recipients`} />
        <Stat label="Answer rate" value={s.answer_rate} kind="pct" animate={false} sub={`${fmt.int(s.answered)} calls answered`} />
        <Stat label="Confirmed" value={s.counts.confirmed} kind="int" animate={false} sub={`${fmt.pct(s.confirm_rate)} of answered calls`} />
        <Stat label="Estimated cost" value={s.cost_inr} kind="inr" animate={false} sub={`${fmt.inr2(s.cost_inr / Math.max(s.calls_placed, 1))} per call`} />
      </section>

      <section className="grid-2 even">
        <div className="card"><div className="card-head"><h2>Outcomes by language</h2></div><Breakdown list={d.by_language} /></div>
        <div className="card"><div className="card-head"><h2>Outcomes by audience segment</h2></div><Breakdown list={d.by_segment} /></div>
      </section>

      {d.note && <div className="stack-gap"><Notice kind={d.status === "paused" ? "warn" : "info"}>{d.note}</Notice></div>}
      <HandlingCard h={d.handling} />
      {d.questions.length > 0 && <Answers d={d} />}
      {d.scripts.length > 0 && <Scripts d={d} />}

      <section className="card">
        <div className="card-head"><h2>Recipients</h2>
          <div className="filters">
            <div className="search"><Icon name="search" />
              <input className="input" placeholder="Search by name" aria-label="Search recipients" value={f.q} onChange={e => setFilter({ q: e.target.value })} />
            </div>
            <select className="select" aria-label="Filter by outcome" value={f.outcome} onChange={e => setFilter({ outcome: e.target.value })}>
              <option value="all">All outcomes</option><option value="nonresp">Non-responders</option>
              {ORDER.map(k => <option key={k} value={k}>{OUT[k]}</option>)}
            </select>
            <select className="select" aria-label="Filter by language" value={f.language} onChange={e => setFilter({ language: e.target.value })}>
              <option value="all">All languages</option>{d.languages.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
            <select className="select" aria-label="Filter by segment" value={f.segment} onChange={e => setFilter({ segment: e.target.value })}>
              <option value="all">All segments</option>{d.segments.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
          </div>
        </div>
        {playing && (
          <div className="player">
            <span className="who"><Icon name="lock" size={14} /> Recording · <b>{playing.name}</b>{d.simulated && <span className="muted"> (demo tone)</span>}</span>
            <audio key={playing.id} src={`/api/recordings/${encodeURIComponent(playing.id)}`} controls autoPlay
              onError={() => { setPlaying(null); toast("Could not play the recording. The session may have expired; unlock again.", "error"); }} />
            <button className="btn sm" onClick={() => void lock()}>Lock</button>
            <button className="btn sm ghost" aria-label="Close player" onClick={() => setPlaying(null)}><Icon name="x" size={14} /></button>
          </div>
        )}
        {rows.length ? (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Name</th><th>Phone</th><th>Language</th><th>Segment</th><th>Outcome</th><th>Replied by</th><th className="num">Attempts</th>{d.questions.length > 0 && <th>Answers</th>}<th><span className="sr-only">Recording</span></th></tr></thead>
                <tbody>
                  {shown.map(r => (
                    <tr key={r.id}>
                      <td>{r.name}</td><td className="mono muted">{r.phone}</td><td>{r.language}</td>
                      <td className="muted">{r.segment}</td><td><OutcomePill r={r} /></td>
                      <td className="muted">{r.channel ? CHANNEL[r.channel] : "–"}</td><td className="num">{r.attempts}</td>
                      {d.questions.length > 0 && <td className="muted" title={d.questions.filter(q => r.answers[q.id]).map(q => `${q.label}: ${r.answers[q.id]}`).join(", ")}>
                        {d.questions.map(q => r.answers[q.id]).filter(Boolean).join(" · ") || "–"}</td>}
                      <td className="num">{r.has_recording && (
                        <button className="btn sm ghost" onClick={() => void play(r)} aria-label={`Play recording for ${r.name}`}>
                          <Icon name="play" size={12} />Play
                        </button>
                      )}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="table-foot"><span>Showing {fmt.int(shown.length)} of {fmt.int(rows.length)}</span>
              {rows.length > shown.length && <button className="btn" onClick={() => setF(p => ({ ...p, limit: p.limit + 50 }))}>Show 50 more</button>}
            </div>
          </>
        ) : <div className="empty"><h2>No recipients match</h2><p>Try clearing a filter.</p></div>}
      </section>
    </main>
  );
}
