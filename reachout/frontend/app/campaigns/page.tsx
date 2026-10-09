"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import type { Summary } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs } from "@/components/Shell";
import { CampaignTable, ErrorView, PageHead, Skeleton } from "@/components/ui";

const STATUSES = [["all", "All"], ["running", "Running"], ["paused", "Paused"], ["completed", "Completed"]] as const;

export default function CampaignsPage() {
  useCrumbs([["Campaigns"]]);
  const { data, error, retry } = useData(() => api<{ campaigns: Summary[] }>("/campaigns"), []);
  const [q, setQ] = useState("");
  const [st, setSt] = useState("all");

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!data) return <main className="view"><Skeleton /></main>;

  const needle = q.trim().toLowerCase();
  const rows = data.campaigns.filter(c => (st === "all" || c.status === st) && c.name.toLowerCase().includes(needle));
  return (
    <main className="view enter">
      <PageHead title="Campaigns" sub="Every invitation, reminder and notice you have sent." />
      <div className="toolbar">
        <div className="search">
          <Icon name="search" />
          <input className="input" placeholder="Search campaigns" aria-label="Search campaigns" value={q} onChange={e => setQ(e.target.value)} />
        </div>
        <div className="seg-ctl">
          {STATUSES.map(([v, l]) => <button key={v} className={st === v ? "on" : ""} onClick={() => setSt(v)}>{l}</button>)}
        </div>
      </div>
      <div className="card"><CampaignTable list={rows} detailed /></div>
    </main>
  );
}
