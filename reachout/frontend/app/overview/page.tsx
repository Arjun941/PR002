"use client";
import Link from "next/link";
import { api } from "@/lib/api";
import { fmt } from "@/lib/format";
import type { Overview } from "@/lib/types";
import { useData } from "@/components/hooks";
import { AssistantChat } from "@/components/AssistantChat";
import { useCrumbs } from "@/components/Shell";
import { Breakdown, CampaignTable, DailyChart, ErrorView, Legend, PageHead, Skeleton, Stat } from "@/components/ui";

export default function OverviewPage() {
  useCrumbs([["Overview"]]);
  const { data: d, error, retry } = useData(() => api<Overview>("/overview"), []);

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!d) return <main className="view"><Skeleton /></main>;

  if (!d.campaigns.length) return (
    <main className="view enter">
      <PageHead title="Overview" sub="How your calling campaigns are performing." />
      <AssistantChat />
      <div className="card empty">
        <h2>No campaigns yet</h2>
        <p>Paste your event details, pick languages and review the drafted scripts before anyone is called.</p>
        <p style={{ marginTop: 16 }}><Link className="btn primary" href="/campaigns/new" style={{ textDecoration: "none" }}>Create a campaign</Link></p>
      </div>
    </main>
  );

  const s = d.totals;
  return (
    <main className="view enter">
      <PageHead title="Overview" sub="How your calling campaigns are performing." />
      <AssistantChat />
      <section className="grid-4">
        <Stat label="Calls placed" value={s.calls_placed} kind="int" sub={`to ${fmt.int(s.contacted)} of ${fmt.int(s.recipients)} recipients`} />
        <Stat label="Answer rate" value={s.answer_rate} kind="pct" sub={`${fmt.int(s.answered)} calls answered`} />
        <Stat label="Confirmed" value={s.counts.confirmed} kind="int" sub={`${fmt.pct(s.confirm_rate)} of answered calls`} />
        <Stat label="Estimated cost" value={s.cost_inr} kind="inr" sub={`${fmt.inr2(s.cost_inr / Math.max(s.calls_placed, 1))} per call`} />
      </section>
      <section className="grid-2">
        <div className="card"><div className="card-head"><h2>Calls per day</h2></div><DailyChart daily={d.daily} /></div>
        <div className="card"><div className="card-head"><h2>By language</h2></div><Breakdown list={d.by_language} /></div>
      </section>
      <section className="card">
        <div className="card-head"><h2>Campaigns</h2><Link href="/campaigns">View all</Link></div>
        <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--line)" }}><Legend counts={s.counts} withNumbers /></div>
        <CampaignTable list={d.campaigns} detailed={false} />
      </section>
    </main>
  );
}
