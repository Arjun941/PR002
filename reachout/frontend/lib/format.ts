import type { Outcome } from "./types";

export const ORDER: Outcome[] = ["confirmed", "rescheduled", "declined", "voicemail", "no_answer", "pending"];
export const OUT: Record<Outcome, string> = {
  confirmed: "Confirmed", rescheduled: "Reschedule", declined: "Declined",
  voicemail: "Voicemail", no_answer: "No answer", pending: "Queued",
};
export const KIND: Record<string, string> = {
  seminar: "Seminar", clinic: "Clinic reminder", school: "School notice", payment: "Payment reminder",
};
export const CHANNEL: Record<string, string> = { keypad: "Keypad", speech: "Speech", agent: "Voice agent" };
export const STATUS: Record<string, string> = { preparing: "Preparing voice", running: "Running", completed: "Completed", paused: "Paused" };

const nf = new Intl.NumberFormat("en-IN");
export const fmt = {
  int: (v: number) => nf.format(Math.round(v)),
  pct: (v: number) => `${Math.round(v * 1000) / 10}%`,
};
export type FmtKind = keyof typeof fmt;

export const ago = (iso: string) => {
  const d = Math.floor((Date.now() - new Date(iso).getTime()) / 864e5);
  return d <= 0 ? "today" : d === 1 ? "yesterday" : `${d} days ago`;
};
