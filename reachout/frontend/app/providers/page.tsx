"use client";
import { useState } from "react";
import { api, post } from "@/lib/api";
import type { GeminiCheck, ProviderGroup, ProviderItem } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs } from "@/components/Shell";
import { ErrorView, PageHead, Skeleton } from "@/components/ui";

function Row({ p }: { p: ProviderItem }) {
  const [check, setCheck] = useState<GeminiCheck | "running" | null>(null);

  const run = async () => {
    setCheck("running");
    try { setCheck(await post<GeminiCheck>("/providers/gemini/check")); }
    catch (e) { setCheck({ ok: false, model: p.model ?? "", error: (e as Error).message }); }
  };

  return (
    <div className="prov-row">
      <div className="prov-main">
        <div className="prov-name">
          {p.label}
          {p.default && <span className="chip">Default</span>}
          {p.model && <span className="chip">{p.model}</span>}
        </div>
        <div className="prov-sub">{p.sends}</div>
        {!p.available && p.setup && <div className="prov-sub">To turn on: {p.setup}</div>}
        {check && check !== "running" && (
          <div className={`prov-check ${check.ok ? "ok" : "bad"}`}>
            {check.ok
              ? `Connected in ${(check.connect_ms! / 1000).toFixed(1)} s, first audio ${(check.first_audio_ms! / 1000).toFixed(1)} s later.`
              : check.error}
          </div>
        )}
      </div>
      <div className="prov-side">
        <span className="prov-region">{p.region}</span>
        <span className={`pill ${p.available ? "status-running" : ""}`}>
          <i className="dot" />{p.available ? "Ready" : "Not set up"}
        </span>
        {p.checkable && p.available && (
          <button className="btn" onClick={run} disabled={check === "running"}>
            {check === "running" ? <><span className="spinner" />Testing</> : <><Icon name="refresh" />Test connection</>}
          </button>
        )}
      </div>
    </div>
  );
}

export default function ProvidersPage() {
  useCrumbs([["Providers"]]);
  const { data, error, retry } = useData(() => api<{ groups: ProviderGroup[] }>("/providers"), []);

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!data) return <main className="view"><Skeleton /></main>;

  return (
    <main className="view enter">
      <PageHead title="Providers" sub="The voices and models behind your calls, and where each one sends data." />
      {data.groups.map(g => (
        <section className="card" key={g.key}>
          <div className="card-head"><h2>{g.title}</h2></div>
          <p className="prov-help">{g.help}</p>
          {g.items.map(p => <Row key={p.key} p={p} />)}
        </section>
      ))}
    </main>
  );
}
