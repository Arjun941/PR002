"use client";
import { useState } from "react";
import { api, post } from "@/lib/api";
import { fmt } from "@/lib/format";
import type { Summary } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs, useShell } from "@/components/Shell";
import { CampaignTable, ErrorView, PageHead, Skeleton } from "@/components/ui";

const STATUSES = [["all", "All"], ["running", "Running"], ["paused", "Paused"], ["completed", "Completed"]] as const;

export default function CampaignsPage() {
  useCrumbs([["Campaigns"]]);
  const { data, error, retry } = useData(() => api<{ campaigns: Summary[] }>("/campaigns"), []);
  const { toast, modal } = useShell();
  const [q, setQ] = useState("");
  const [st, setSt] = useState("all");

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!data) return <main className="view"><Skeleton /></main>;

  const openDelete = (c: Summary) => modal({
    title: "Delete this campaign?",
    danger: true,
    body: (
      <>
        <b style={{ color: "var(--text)" }}>{c.name}</b> will be removed for good: its contacts, outcomes, answers and call log.
        This cannot be undone.
        <dl className="facts">
          <div><dt>Recipients</dt><dd>{fmt.int(c.totals.recipients)}</dd></div>
          <div><dt>Calls placed</dt><dd>{fmt.int(c.totals.calls_placed)}</dd></div>
        </dl>
        <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>Call recordings stay with Exotel until their own retention removes them.</p>
      </>
    ),
    confirmLabel: "Delete campaign",
    onConfirm: async () => {
      await post(`/campaigns/${c.id}`, undefined, "DELETE");
      toast(`Deleted “${c.name}”`);
      retry();
    },
  });

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
      <div className="card"><CampaignTable list={rows} detailed onDelete={openDelete} /></div>
    </main>
  );
}
