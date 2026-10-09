"use client";
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { useShell } from "@/components/Shell";

/** A small play button for one synthesised phrase. `load` fetches the audio (a WAV); it is fetched on click and played once. */
export function Listen({ load, disabled, title = "Listen" }: { load: () => Promise<Response>; disabled?: boolean; title?: string }) {
  const { toast } = useShell();
  const [state, setState] = useState<"idle" | "loading" | "playing">("idle");
  const audio = useRef<HTMLAudioElement | null>(null);
  useEffect(() => () => { audio.current?.pause(); }, []);

  const click = async () => {
    if (state === "playing") { audio.current?.pause(); setState("idle"); return; }
    setState("loading");
    try {
      const res = await load();
      if (!res.ok) throw new Error(((await res.json().catch(() => null)) as { detail?: string } | null)?.detail ?? `HTTP ${res.status}`);
      const url = URL.createObjectURL(await res.blob());
      const a = new Audio(url);
      audio.current = a;
      a.onended = a.onerror = () => { URL.revokeObjectURL(url); setState("idle"); };
      await a.play();
      setState("playing");
    } catch (e) { toast((e as Error).message, "error"); setState("idle"); }
  };

  return (
    <button type="button" className="btn sm" disabled={disabled || state === "loading"} onClick={() => void click()} title={title}>
      {state === "loading" ? <span className="spinner" /> : <Icon name={state === "playing" ? "stop" : "play"} size={12} />}
      {state === "playing" ? "Stop" : "Listen"}
    </button>
  );
}
