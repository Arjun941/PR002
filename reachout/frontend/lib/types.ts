export type Outcome = "confirmed" | "rescheduled" | "declined" | "voicemail" | "no_answer" | "pending";
export type Counts = Record<Outcome, number>;
export type Status = "running" | "completed" | "paused";

export interface Totals {
  recipients: number; contacted: number; calls_placed: number; answered: number;
  answer_rate: number; confirm_rate: number; cost_inr: number; counts: Counts;
  retryable: number; retrying: number;
}
export interface Summary {
  id: string; name: string; kind: string; status: Status; languages: string[];
  segments: string[]; started_at: string; totals: Totals;
}
export interface Group { key: string; total: number; counts: Counts; answer_rate: number; confirm_rate: number }
export interface Day { label: string; calls: number; answered: number }
export interface Overview { totals: Totals; by_language: Group[]; daily: Day[]; campaigns: Summary[] }
export interface Handling { provider: string; note: string }
export interface Recipient {
  id: string; name: string; phone: string; language: string; segment: string;
  outcome: Outcome; channel: "keypad" | "speech" | "agent" | null; attempts: number; retrying: boolean;
}
export interface Detail extends Summary {
  by_language: Group[]; by_segment: Group[];
  handling: { audio: Handling; text: Handling; recordings: Handling };
  retry_estimate_inr: number; recipients: Recipient[];
}
