"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { CHANNEL, OUT } from "@/lib/format";
import type { CallRecord, UnseenCall } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useRecordingAccess } from "@/components/recaccess";
import { useCrumbs } from "@/components/Shell";
import { Notice, PageHead, Skeleton } from "@/components/ui";

const when = (iso: string | null) => iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "medium" }) : "–";
const clock = (iso: string | null) => iso ? new Date(iso).toLocaleTimeString("en-IN", { timeStyle: "medium" }) : "–";
const dur = (s: number | null) => s == null ? "–" : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
const ANALYSIS: Record<string, string> = {
  pending: "Working out what was said…", failed: "Could not work out what was said", off: "Not analysed", none: "Not analysed",
};

function Outcome({ o }: { o: CallRecord["outcome"] }) {
  return o ? <span className="pill"><i className="dot" style={{ background: `var(--c-${o})` }} />{OUT[o]}</span> : <span className="muted">–</span>;
}

function Detail({ id, onClose }: { id: string; onClose: () => void }) {
  const { data: c, error } = useData(() => api<CallRecord>(`/history/${encodeURIComponent(id)}`), [id]);
  const { unlockThen, lock } = useRecordingAccess();
  const [playing, setPlaying] = useState(false);
  if (error !== null) return <section className="card"><div className="empty"><h2>Call not found</h2></div></section>;
  if (!c) return <Skeleton />;
  return (
    <section className="card history-detail" aria-label="Call details">
      <div className="card-head">
        <h2>{c.recipient_name ?? "Unknown"} · {c.campaign_name ?? "–"}</h2>
        <button className="btn sm ghost" aria-label="Close details" onClick={onClose}><Icon name="x" size={14} /></button>
      </div>
      <div className="history-grid">
        <dl className="facts">
          <div><dt>Called for</dt><dd>{c.org}</dd></div>
          <div><dt>Phone</dt><dd className="mono">{c.phone ?? "–"}</dd></div>
          <div><dt>Language</dt><dd>{c.language ?? "–"}</dd></div>
          <div><dt>Handled by</dt><dd>{c.provider ?? "–"}{c.mode && ` · ${c.mode === "hybrid" ? "keypad + agent" : "live agent"}`}</dd></div>
          <div><dt>Attempt</dt><dd>{c.attempt ?? "–"}</dd></div>
          {c.kind === "notice" ? <div><dt>Type</dt><dd>{c.notice_kind === "update" ? "Update" : "Reminder"} (a message, not a survey)</dd></div> : null}
          <div><dt>Final answer</dt><dd><Outcome o={c.outcome} />{c.channel && <span className="muted"> by {CHANNEL[c.channel] ?? c.channel}</span>}</dd></div>
          {c.final_heard && c.final_heard !== "unclear" && c.final_heard !== c.outcome &&
            <div><dt>Heard in the recording</dt><dd>{c.final_heard}</dd></div>}
        </dl>
        <dl className="facts">
          <div><dt>Rang</dt><dd>{when(c.rang_at)}</dd></div>
          <div><dt>Answered</dt><dd>{c.answered_at ? `${clock(c.answered_at)} (after ${dur(c.ring_seconds)})` : "Not answered"}</dd></div>
          <div><dt>Ended</dt><dd>{clock(c.ended_at)}</dd></div>
          <div><dt>Talk time</dt><dd>{dur(c.talk_seconds)}</dd></div>
          {!!c.keys_pressed?.length && <div><dt>Keys pressed</dt><dd className="mono">{c.keys_pressed.join(" ")}</dd></div>}
          {Object.keys(c.answers).length > 0 && <div><dt>Keypad answers</dt><dd>{Object.values(c.answers).join(" · ")}</dd></div>}
        </dl>
      </div>

      <div className="history-block">
        <h3><Icon name="lock" size={14} />Recording</h3>
        {c.has_recording ? (playing ? (
          <div className="history-player">
            <audio src={`/api/history/${encodeURIComponent(c.id)}/audio`} controls autoPlay onError={() => setPlaying(false)} />
            <button className="btn sm" onClick={() => { setPlaying(false); void lock(); }}>Lock</button>
          </div>
        ) : (
          <button className="btn" onClick={() => void unlockThen(() => setPlaying(true))}><Icon name="play" size={14} />Play recording ({dur(c.recording.seconds)})</button>
        )) : <p className="muted">{c.recording.note || "No recording"}</p>}
      </div>

      <div className="history-block">
        <h3>What was asked and answered</h3>
        {c.analysis.status === "done" ? (
          <>
            {!!c.answers_corrected?.length && (
              <div className="stack-gap"><Notice kind="info">
                Corrected from what the person said: {c.answers_corrected.map(x => `${x.question}: the agent saved “${x.saved ?? "nothing"}”, the person said “${x.heard}”`).join("; ")}.
              </Notice></div>
            )}
            {c.summary && <p>{c.summary}</p>}
            {c.qa?.length ? (
              <table className="table qa-table">
                <thead><tr><th>Question</th><th>Answer</th></tr></thead>
                <tbody>{c.qa.map((q, i) => <tr key={i}><td>{q.question}</td><td>{q.answer || <span className="muted">No answer</span>}</td></tr>)}</tbody>
              </table>
            ) : <p className="muted">No questions were asked.</p>}
            <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>Worked out from the recording by {c.analysis.by ?? "Gemini"}. Check anything important against the recording.</p>
          </>
        ) : <p className="muted">{ANALYSIS[c.analysis.status] ?? c.analysis.status}{c.analysis.note && ` (${c.analysis.note})`}</p>}
      </div>

      {!!c.transcript?.length && (
        <details className="history-block">
          <summary><b>Transcript</b> <span className="muted">({c.transcript.length} turns)</span></summary>
          <div className="chat-log transcript">
            {c.transcript.map((t, i) => <p key={i} className={`bubble ${t.speaker === "person" ? "user" : "assistant"}`}>{t.text}</p>)}
          </div>
        </details>
      )}
    </section>
  );
}

export default function HistoryPage() {
  useCrumbs([["History"]]);
  const [campaign, setCampaign] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  // A link from a campaign page (?call=<id>) opens that call.
  useEffect(() => { const id = new URLSearchParams(window.location.search).get("call"); if (id) setOpen(id); }, []);
  const { data, error, retry } = useData(
    () => api<{ calls: CallRecord[]; campaigns: { id: string; name: string }[]; unseen: UnseenCall[] }>(`/history${campaign ? `?campaign=${encodeURIComponent(campaign)}` : ""}`),
    [campaign]);

  return (
    <main className="view enter">
      <PageHead title="History" sub="Every call the phone rang: when, how long, what was said, and the recording."
        actions={<button className="btn" onClick={retry}><Icon name="refresh" size={14} />Refresh</button>} />
      <div className="toolbar">
        <select className="select" aria-label="Campaign" value={campaign} onChange={e => { setCampaign(e.target.value); setOpen(null); }}>
          <option value="">All campaigns</option>
          {data?.campaigns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </div>
      {!!data?.unseen.length && (
        <div className="stack-gap"><Notice>
          <b>{data.unseen.length} call{data.unseen.length === 1 ? " was" : "s were"} placed by another Reachout server</b> using the same database
          (latest: {data.unseen[0].recipient}, {when(data.unseen[0].at)}). That server does not record calls, so they are not here.
          On the phone page, open its settings and set <b>Server</b> to this backend&apos;s address (the same address that serves this
          dashboard&apos;s API, or a tunnel to it), then reconnect. Stop or update the other server too: both ring the phone for the same campaigns.
        </Notice></div>
      )}
      {open && <Detail id={open} onClose={() => setOpen(null)} />}
      {error !== null ? <Notice>Could not load the call history.</Notice> : !data ? <Skeleton /> : data.calls.length === 0 ? (
        <div className="card empty"><h2>No calls yet</h2><p>Calls appear here when they end. Start a campaign and keep <Link href="/providers">the phone page</Link> open.</p></div>
      ) : (
        <section className="card">
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>When</th><th>Person</th><th>Campaign</th><th>Final answer</th><th className="num">Rang for</th>
                <th className="num">Talk time</th><th>Answers</th><th><span className="sr-only">Recording</span></th></tr></thead>
              <tbody>
                {data.calls.map(c => (
                  <tr key={c.id} className={`clickable${open === c.id ? " on" : ""}`} onClick={() => setOpen(c.id)}>
                    <td className="mono muted">{when(c.rang_at)}</td>
                    <td>{c.recipient_name ?? "–"}<div className="cell-sub mono">{c.phone ?? ""}</div></td>
                    <td>{c.campaign_name ?? "–"}<div className="cell-sub">{c.org}{c.provider && ` · ${c.provider}`}</div></td>
                    <td>{c.kind === "notice" ? <span className="chip">{c.notice_kind === "update" ? "Update" : "Reminder"}</span> : <Outcome o={c.outcome} />}</td>
                    <td className="num">{dur(c.ring_seconds)}</td>
                    <td className="num">{c.answered_at ? dur(c.talk_seconds) : <span className="muted">Not answered</span>}</td>
                    <td className="muted">{c.analysis.status === "pending" ? "Working…" : c.qa_count ? `${c.qa_count} answered` : "–"}</td>
                    <td className="num">{c.has_recording ? <span title="Recorded"><Icon name="play" size={12} /></span>
                      : <span className="muted" title={c.recording.note}>–</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="table-foot"><span>{data.calls.length} call{data.calls.length === 1 ? "" : "s"}. Select a row for the details.</span></div>
        </section>
      )}
    </main>
  );
}
