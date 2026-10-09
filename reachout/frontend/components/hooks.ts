"use client";
import { useEffect, useRef, useState } from "react";

/** False on first paint, true two frames later, so CSS width/height transitions run. */
export function useGrown(): boolean {
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { setOn(true); return; }
    let b = 0;
    const a = requestAnimationFrame(() => { b = requestAnimationFrame(() => setOn(true)); });
    return () => { cancelAnimationFrame(a); cancelAnimationFrame(b); };
  }, []);
  return on;
}

/** Calls fn every `ms` while `active`; always uses the latest fn. */
export function usePoll(fn: () => void | Promise<void>, ms: number, active: boolean) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => { void ref.current(); }, ms);
    return () => clearInterval(t);
  }, [active, ms]);
}

/** Fetch with loading/error state. `setData` lets callers refresh in place without a skeleton. */
export function useData<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    loadRef.current().then(d => live && setData(d)).catch(e => live && setError(e.status ?? 0));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);
  return { data, setData, error, retry: () => setNonce(n => n + 1) };
}
