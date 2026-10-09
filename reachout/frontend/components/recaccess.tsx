"use client";
import { useRef } from "react";
import { api, post } from "@/lib/api";
import { useShell } from "@/components/Shell";

/** Recordings are personal data: playback needs a PIN-unlocked session (15 minutes), and the server logs every play.
 *  `unlockThen(fn)` runs fn at once if unlocked, otherwise asks for the PIN first. */
export function useRecordingAccess() {
  const { toast, modal } = useShell();
  const pinRef = useRef<HTMLInputElement>(null);

  const unlockThen = async (fn: () => void) => {
    let access: { enabled: boolean; unlocked: boolean };
    try { access = await api("/auth/recordings"); } catch (e) { toast((e as Error).message, "error"); return; }
    if (!access.enabled) { toast("Recording playback is off. Set RECORDINGS_PIN on the server to turn it on.", "info"); return; }
    if (access.unlocked) { fn(); return; }
    modal({
      title: "Unlock recordings",
      body: (
        <>
          Call recordings are personal data. Enter the recordings PIN to listen for the next 15 minutes. Each play is logged.
          <input ref={pinRef} className="input" type="password" inputMode="numeric" autoComplete="off" autoFocus
            aria-label="Recordings PIN" placeholder="PIN" style={{ width: "100%", marginTop: 14 }} />
        </>
      ),
      confirmLabel: "Unlock and play",
      onConfirm: async () => {
        await post("/auth/recordings", { pin: pinRef.current?.value ?? "" });
        fn();
      },
    });
  };
  const lock = async () => {
    try { await post("/auth/recordings", undefined, "DELETE"); toast("Recordings locked"); } catch { /* cookie expires anyway */ }
  };
  return { unlockThen, lock };
}
