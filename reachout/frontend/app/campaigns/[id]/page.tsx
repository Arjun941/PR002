"use client";
import { use, useState } from "react";
import { api } from "@/lib/api";
import { ago, CHANNEL, fmt, KIND, ORDER, OUT } from "@/lib/format";
import type { Detail, Handling } from "@/lib/types";
import { useData, usePoll } from "@/components/hooks";
import { Icon, type IconName } from "@/components/Icon";
import { useCrumbs, useShell } from "@/components/Shell";
import { Breakdown, ErrorView, OutcomePill, PageHead, Skeleton, Stat, StatusPill } from "@/components/ui";

const NON_RESPONDER = ["voicemail", "no_answer"];

function HandlingItem({ icon, label, x }: { icon: IconName; label: string; x: Handling }) {
  return (
    <div className="h-item"><Icon name={icon} size={18} />
      <div><div className="h-label">{label}</div><div className="h-main">{x.provider}</div><div className="h-note">{x.note}</div></div>
    </div>
  );
}

export default function CampaignDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { toast, modal } = useShell();
  const { data: d, setData, error, retry } = useData(() => api<Detail>(`/campaigns/${id}`), [id]);
  const [f, setF] = useState({ q: "", outcome: "all", language: "all", segment: "all", limit: 50 });

  useCrumbs([["Campaigns", "/campaigns"], [d?.name ?? "Campaign"]]);
  const refresh = async () => { try { setData(await api<Detail>(`/campaigns/${id}`)); } catch { /* keep last good view */ } };
  usePoll(refresh, 2500, !!d && (d.status === "running" || d.totals.retrying > 0));

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
        <dl className="facts">
          <div><dt>Recipients</dt><dd>{fmt.int(n)}</dd></div>
          <div><dt>Made up of</dt><dd>{fmt.int(s.counts.voicemail)} voicemail, {fmt.int(s.counts.no_answer)} no answer</dd></div>
          <div><dt>Estimated cost</dt><dd>{fmt.inr2(d.retry_estimate_inr)}</dd></div>
        </dl>
      </>
    ),
    confirmLabel: `Call ${fmt.int(n)} again`,
    onConfirm: async () => {
      const res = await api<{ queued: number }>(`/campaigns/${id}/retry`, { method: "POST" });
      toast(`Retrying ${fmt.int(res.queued)} recipients`);
      await refresh();
    },
  });

  return (
    <main className="view enter">
      <div className="page-head">
        <div>
          <h1>{d.name}</h1>
          <div className="meta">
            <StatusPill s={d.status} /><span className="chip">{KIND[d.kind] || d.kind}</span>
            {d.languages.map(l => <span key={l} className="chip">{l}</span>)}
            <span className="muted">Started {ago(d.started_at)}</span>
          </div>
        </div>
        <div className="actions">
          <button className="btn primary" disabled={n === 0 || busy} onClick={openRetry}
            title={n === 0 && !busy ? "No one is waiting for a retry" : ""}>
            {busy ? <span className="spinner" /> : <Icon name="refresh" />}
            {busy ? `Retrying ${fmt.int(s.retrying)}` : "Retry non-responders"}
            {!busy && n > 0 && <span className="count">{fmt.int(n)}</span>}
          </button>
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

      <section className="card handling">
        <div className="card-head"><h2>Where your data is processed</h2></div>
        <HandlingItem icon="mic" label="Voice" x={d.handling.audio} />
        <HandlingItem icon="type" label="Language" x={d.handling.text} />
        <HandlingItem icon="lock" label="Recordings" x={d.handling.recordings} />
      </section>

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
        {rows.length ? (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Name</th><th>Phone</th><th>Language</th><th>Segment</th><th>Outcome</th><th>Replied by</th><th className="num">Attempts</th></tr></thead>
                <tbody>
                  {shown.map(r => (
                    <tr key={r.id}>
                      <td>{r.name}</td><td className="mono muted">{r.phone}</td><td>{r.language}</td>
                      <td className="muted">{r.segment}</td><td><OutcomePill r={r} /></td>
                      <td className="muted">{r.channel ? CHANNEL[r.channel] : "–"}</td><td className="num">{r.attempts}</td>
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
