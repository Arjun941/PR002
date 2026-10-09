"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  createContext, Fragment, useCallback, useContext, useEffect, useRef, useState, type ReactNode,
} from "react";
import { AssistantChat } from "./AssistantChat";
import { Icon, type IconName } from "./Icon";

type Crumb = [label: string, href?: string];
type ToastKind = "ok" | "error" | "info";
interface ModalSpec { title: string; body: ReactNode; confirmLabel: string; onConfirm: () => Promise<void>; danger?: boolean }

interface Ctx {
  setCrumbs: (c: Crumb[]) => void;
  toast: (msg: string, kind?: ToastKind) => void;
  modal: (m: ModalSpec) => void;
}
const ShellCtx = createContext<Ctx>({ setCrumbs() {}, toast() {}, modal() {} });
export const useShell = () => useContext(ShellCtx);

/** Sets the topbar breadcrumbs for the lifetime of the calling page. */
export function useCrumbs(crumbs: Crumb[]) {
  const { setCrumbs } = useShell();
  const key = JSON.stringify(crumbs);
  useEffect(() => { setCrumbs(JSON.parse(key)); }, [key, setCrumbs]);
}

const NAV: { href: string; label: string; icon: IconName }[] = [
  { href: "/overview", label: "Overview", icon: "grid" },
  { href: "/campaigns", label: "Campaigns", icon: "phone" },
  { href: "/history", label: "History", icon: "clock" },
  { href: "/providers", label: "Providers", icon: "cpu" },
];

function ModalView({ spec, onClose }: { spec: ModalSpec; onClose: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const okRef = useRef<HTMLButtonElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const { toast } = useShell();
  const closing = useRef(false);

  const close = useCallback(() => {
    if (closing.current) return;
    closing.current = true;
    setOpen(false);
    setTimeout(onClose, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180);
  }, [onClose]);

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    requestAnimationFrame(() => setOpen(true));
    // An autoFocus input in the body keeps focus; otherwise focus the confirm button.
    if (!boxRef.current?.contains(document.activeElement)) okRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, [close]);

  const confirm = async () => {
    setBusy(true);
    try { await spec.onConfirm(); close(); }
    catch (e) { setBusy(false); toast((e as Error).message || "Something went wrong", "error"); }
  };

  return (
    <div className={`overlay${open ? " open" : ""}`} onMouseDown={e => { if (e.target === e.currentTarget) close(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="m-title" ref={boxRef}
        onKeyDown={e => { if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT" && !busy) void confirm(); }}>
        <h2 id="m-title">{spec.title}</h2>
        <div className="modal-body">{spec.body}</div>
        <div className="modal-actions">
          <button className="btn" onClick={close}>Cancel</button>
          <button className={spec.danger ? "btn danger solid" : "btn primary"} ref={okRef} disabled={busy} onClick={confirm}>
            {busy ? <><span className="spinner" />Working</> : spec.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
  const [toasts, setToasts] = useState<{ id: number; msg: string; kind: ToastKind; show: boolean }[]>([]);
  const [modalSpec, setModalSpec] = useState<ModalSpec | null>(null);
  const seq = useRef(0);

  const toast = useCallback((msg: string, kind: ToastKind = "ok") => {
    const id = ++seq.current;
    setToasts(t => [...t, { id, msg, kind, show: false }]);
    requestAnimationFrame(() => setToasts(t => t.map(x => x.id === id ? { ...x, show: true } : x)));
    setTimeout(() => setToasts(t => t.map(x => x.id === id ? { ...x, show: false } : x)), 3200);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 3450);
  }, []);
  const modal = useCallback((m: ModalSpec) => setModalSpec(m), []);
  const ctx = useRef<Ctx>({ setCrumbs, toast, modal }).current;

  return (
    <ShellCtx.Provider value={ctx}>
      <div className="app">
        <aside className="sidebar">
          <Link className="brand" href="/overview" aria-label="Reachout home">
            <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="3" y="9" width="3.5" height="6" rx="1.75" />
              <rect x="10.25" y="4" width="3.5" height="16" rx="1.75" />
              <rect x="17.5" y="8" width="3.5" height="8" rx="1.75" />
            </svg>
            Reachout
          </Link>
          <nav className="nav" aria-label="Main">
            {NAV.map(n => (
              <Link key={n.href} href={n.href} className={pathname.startsWith(n.href) ? "active" : undefined}>
                <Icon name={n.icon} />{n.label}
              </Link>
            ))}
          </nav>
          <div className="side-foot">
            <div className="avatar">DI</div>
            <div><div className="foot-title">Demo institution</div><div className="foot-sub">Self-hosted</div></div>
          </div>
        </aside>

        <div className="main">
          <header className="topbar">
            <div className="crumbs">
              {crumbs.map(([label, href], i) => (
                <Fragment key={i}>
                  {i > 0 && <Icon name="chevron" size={14} />}
                  {i === crumbs.length - 1 || !href ? <span className="cur">{label}</span> : <Link href={href}>{label}</Link>}
                </Fragment>
              ))}
            </div>
            {pathname !== "/campaigns/new" && (
              <Link className="btn primary" href="/campaigns/new" style={{ textDecoration: "none" }}>
                <Icon name="plus" />New campaign
              </Link>
            )}
          </header>
          {children}
        </div>
      </div>
      <AssistantChat />

      <div id="toasts" role="status" aria-live="polite">
        {toasts.map(t => (
          <div key={t.id} className={`toast ${t.kind}${t.show ? " show" : ""}`}>
            <Icon name={t.kind === "error" ? "x" : t.kind === "info" ? "info" : "check"} /><span>{t.msg}</span>
          </div>
        ))}
      </div>
      {modalSpec && <ModalView spec={modalSpec} onClose={() => setModalSpec(null)} />}
    </ShellCtx.Provider>
  );
}
