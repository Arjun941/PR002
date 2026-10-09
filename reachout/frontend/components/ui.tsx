"use client";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { ago, fmt, KIND, ORDER, OUT, STATUS, type FmtKind } from "@/lib/format";
import type { Counts, Day, Group, Recipient, Status, Summary } from "@/lib/types";
import { useGrown } from "./hooks";

export function PageHead({ title, sub, actions }: { title: ReactNode; sub: string; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div><h1>{title}</h1><p className="sub">{sub}</p></div>
      <div className="actions">{actions}</div>
    </div>
  );
}

function CountUp({ value, kind, animate }: { value: number; kind: FmtKind; animate: boolean }) {
  const [shown, setShown] = useState(animate ? 0 : value);
  useEffect(() => {
    if (!animate || matchMedia("(prefers-reduced-motion: reduce)").matches) { setShown(value); return; }
    const t0 = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / 800);
      setShown(value * (1 - Math.pow(1 - p, 3)));
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, animate]);
  return <>{fmt[kind](shown)}</>;
}

export function Stat({ label, value, kind, sub, animate = true }:
  { label: string; value: number; kind: FmtKind; sub: string; animate?: boolean }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value"><CountUp value={value} kind={kind} animate={animate} /></div>
      <div className="stat-sub">{sub}</div>
    </div>
  );
}

export function Stack({ counts, total }: { counts: Counts; total: number }) {
  const grown = useGrown();
  const label = ORDER.filter(k => counts[k]).map(k => `${counts[k]} ${OUT[k].toLowerCase()}`).join(", ");
  return (
    <div className="stack" role="img" aria-label={label}>
      {ORDER.filter(k => counts[k] > 0).map(k => (
        <span key={k} className="seg" title={`${OUT[k]}: ${counts[k]}`}
          style={{ background: `var(--c-${k})`, width: grown ? `${(counts[k] / (total || 1)) * 100}%` : 0 }} />
      ))}
    </div>
  );
}

export function Legend({ counts, withNumbers }: { counts: Counts; withNumbers?: boolean }) {
  return (
    <div className="legend">
      {ORDER.filter(k => counts[k] > 0 || !withNumbers).map(k => (
        <span key={k} className="lg"><i style={{ background: `var(--c-${k})` }} />{OUT[k]}{withNumbers && <> <b>{fmt.int(counts[k])}</b></>}</span>
      ))}
    </div>
  );
}

export const StatusPill = ({ s }: { s: Status }) =>
  <span className={`pill status-${s}`}><i className="dot" />{STATUS[s]}</span>;

export const OutcomePill = ({ r }: { r: Recipient }) => r.retrying
  ? <span className="pill retrying"><i className="dot" />Retrying</span>
  : <span className="pill"><i className="dot" style={{ background: `var(--c-${r.outcome})` }} />{OUT[r.outcome]}</span>;

export function Breakdown({ list }: { list: Group[] }) {
  return (
    <>
      <div className="brow-head"><span /><span>Outcomes</span><span>Confirmed</span></div>
      {list.map(g => (
        <div className="brow" key={g.key}>
          <div className="brow-label"><span>{g.key}</span><small>{fmt.int(g.total)} recipients</small></div>
          <Stack counts={g.counts} total={g.total} />
          <div className="brow-val" title="Confirmed, as a share of answered calls">{fmt.pct(g.confirm_rate)}</div>
        </div>
      ))}
    </>
  );
}

export function DailyChart({ daily }: { daily: Day[] }) {
  const grown = useGrown();
  const max = Math.max(...daily.map(d => d.calls), 1);
  return (
    <>
      <div className="daily">
        {daily.map(d => (
          <div className="day" key={d.label} title={`${d.label}: ${d.calls} calls, ${d.answered} answered`}>
            <div className="bar-area">
              <div className="bar" style={{ height: grown ? `${(d.calls / max) * 100}%` : 0 }}>
                <div className="bar-ans" style={{ height: `${d.calls ? (d.answered / d.calls) * 100 : 0}%` }} />
              </div>
            </div>
            <span className="day-label">{d.label}</span>
          </div>
        ))}
      </div>
      <div className="legend chart-legend">
        <span className="lg"><i style={{ background: "var(--accent)" }} />Answered</span>
        <span className="lg"><i style={{ background: "#262b32" }} />Not answered</span>
      </div>
    </>
  );
}

export function CampaignTable({ list, detailed }: { list: Summary[]; detailed: boolean }) {
  if (!list.length) return <div className="empty"><h2>No campaigns match</h2><p>Try a different search or status.</p></div>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr>
          <th>Campaign</th><th>Status</th><th>Languages</th><th className="w-outcomes">Outcomes</th>
          <th className="num">Answer rate</th><th className="num">Cost</th>{detailed && <th className="num">Started</th>}
        </tr></thead>
        <tbody>
          {list.map(c => (
            <tr key={c.id} className="clickable">
              <td>
                <Link href={`/campaigns/${c.id}`} className="cell-main" style={{ display: "block" }}>{c.name}</Link>
                <div className="cell-sub">{KIND[c.kind] || c.kind}</div>
              </td>
              <td><StatusPill s={c.status} /></td>
              <td className="muted">{c.languages.join(", ")}</td>
              <td><Stack counts={c.totals.counts} total={c.totals.recipients} /></td>
              <td className="num">{fmt.pct(c.totals.answer_rate)}</td>
              <td className="num">{fmt.inr(c.totals.cost_inr)}</td>
              {detailed && <td className="num muted">{ago(c.started_at)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Skeleton() {
  return (
    <>
      <div className="skel skel-head" />
      <div className="grid-4">{[0, 1, 2, 3].map(i => <div key={i} className="skel skel-card" />)}</div>
      <div className="skel skel-block" />
    </>
  );
}

export function ErrorView({ status, retry }: { status: number; retry: () => void }) {
  if (status === 404) return (
    <div className="empty"><h2>Campaign not found</h2><p>It may have been removed.</p>
      <p style={{ marginTop: 14 }}><Link className="btn" href="/campaigns">Back to campaigns</Link></p></div>
  );
  return (
    <div className="empty"><h2>Can&apos;t reach the server</h2>
      <p>Start the backend from the project folder, then try again.</p>
      <code>uvicorn backend.main:app --reload</code>
      <p><button className="btn" onClick={retry}>Try again</button></p></div>
  );
}
