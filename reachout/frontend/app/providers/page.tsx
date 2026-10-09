"use client";
import { useState } from "react";
import { api, post } from "@/lib/api";
import type { ProviderCheck, ProviderItem, ProvidersInfo } from "@/lib/types";
import { useData } from "@/components/hooks";
import { Icon } from "@/components/Icon";
import { useCrumbs, useShell } from "@/components/Shell";
import { ErrorView, PageHead, Skeleton } from "@/components/ui";

function Row({ p, onDefault }: { p: ProviderItem; onDefault: () => void }) {
  const [check, setCheck] = useState<ProviderCheck | "running" | null>(null);
  const live = p.caps.find(c => c.key === "live");

  const run = async () => {
    setCheck("running");
    try { setCheck(await post<ProviderCheck>(`/providers/${p.key}/check`)); }
    catch (e) { setCheck({ ok: false, error: (e as Error).message }); }
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
        <div className="prov-sub">
          {p.caps.map(c => (
            <span key={c.key} style={{ marginRight: 12, opacity: c.supported ? 1 : 0.5 }}>
              {c.ready ? "✓" : c.supported ? "✗" : "–"} {c.label}
              {c.supported && !c.ready && c.missing.length > 0 && ` (set ${c.missing.join(", ")})`}
            </span>
          ))}
        </div>
        {check && check !== "running" && (
          <div className={`prov-check ${check.ok ? "ok" : "bad"}`}>
            {check.ok
              ? check.connect_ms !== undefined
                ? `Connected in ${(check.connect_ms / 1000).toFixed(1)} s, first audio ${(check.first_audio_ms! / 1000).toFixed(1)} s later.`
                : "Connected."
              : check.error}
          </div>
        )}
      </div>
      <div className="prov-side">
        <span className="prov-region">{p.region}</span>
        <span className={`pill ${p.available ? "status-running" : ""}`}>
          <i className="dot" />{p.available ? "Ready" : "Not set up"}
        </span>
        {live?.ready && (
          <button className="btn" onClick={run} disabled={check === "running"}>
            {check === "running" ? <><span className="spinner" />Testing</> : <><Icon name="refresh" />Test connection</>}
          </button>
        )}
        {p.available && !p.default && <button className="btn" onClick={onDefault}>Make default</button>}
      </div>
    </div>
  );
}

export default function ProvidersPage() {
  useCrumbs([["Providers"]]);
  const { toast } = useShell();
  const { data, error, retry } = useData(() => api<ProvidersInfo>("/providers"), []);

  if (error !== null) return <main className="view"><ErrorView status={error} retry={retry} /></main>;
  if (!data) return <main className="view"><Skeleton /></main>;

  const makeDefault = async (key: string) => {
    try { await post("/providers/default", { provider: key }, "PUT"); toast("Default provider saved"); retry(); }
    catch (e) { toast((e as Error).message, "error"); }
  };

  return (
    <main className="view enter">
      <PageHead title="Providers" sub="Each campaign picks one provider. It writes the scripts where it can, and holds the call." />
      <section className="card">
        <div className="card-head"><h2>Voice and language providers</h2></div>
        <p className="prov-help">The default is only what a new campaign starts on. Keys live in .env on the server.</p>
        {data.providers.map(p => <Row key={p.key} p={p} onDefault={() => void makeDefault(p.key)} />)}
      </section>
      <section className="card">
        <div className="card-head"><h2>Web phone</h2></div>
        <p className="prov-help">A browser page that rings when a campaign calls; it is how campaigns place calls.
          Open <b>{data.webphone.path}</b> on the backend (port 8000, or its tunnel URL) on a phone or in another tab and keep it open.
          {data.webphone.token_required ? " It needs the PHONE_TOKEN: open it as /phone#token=…." : " Set PHONE_TOKEN in .env before exposing it to the internet."}</p>
        <div className="prov-row">
          <div className="prov-main"><div className="prov-name">Phones online
            <span className="chip">{data.webphone.connected}</span></div></div>
          <div className="prov-side"><a className="btn" href={`http://${window.location.hostname}:8000${data.webphone.path}`} target="_blank" rel="noreferrer">Open web phone</a></div>
        </div>
      </section>
    </main>
  );
}
