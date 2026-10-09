export type Outcome = "confirmed" | "rescheduled" | "declined" | "voicemail" | "no_answer" | "pending";
export type Counts = Record<Outcome, number>;
export type Status = "preparing" | "running" | "completed" | "paused";

export interface Totals {
  recipients: number; contacted: number; calls_placed: number; answered: number;
  answer_rate: number; confirm_rate: number; cost_inr: number; counts: Counts;
  retryable: number; retrying: number;
}
export interface Summary {
  id: string; name: string; kind: string; status: Status; languages: string[];
  segments: string[]; started_at: string; simulated: boolean; totals: Totals;
}
export interface Group { key: string; total: number; counts: Counts; answer_rate: number; confirm_rate: number }
export interface Day { label: string; calls: number; answered: number }
export interface Overview { totals: Totals; by_language: Group[]; daily: Day[]; campaigns: Summary[] }
export interface Handling { provider: string; note: string }
export interface Recipient {
  id: string; name: string; phone: string; language: string; segment: string;
  outcome: Outcome; channel: "keypad" | "speech" | "agent" | null; attempts: number; retrying: boolean;
  has_recording: boolean; answers: Record<string, string>;
  last_call?: LastCall | null;
}
export interface UnseenCall { call_id: string; campaign_id: string; recipient: string; at: string }
export interface QA { question: string; answer: string }
export interface LastCall { call_id: string; qa: QA[]; summary: string; talk_seconds: number | null; ended_at: string; analysis: string }
export interface CallRecord {
  id: string; campaign_id: string | null; campaign_name: string | null; org: string; recipient_id: string | null;
  recipient_name: string | null; phone: string | null; language: string | null; provider: string | null; mode: string | null;
  rang_at: string; answered_at: string | null; ended_at: string; ring_seconds: number | null; talk_seconds: number | null;
  outcome: Outcome | null; channel: string | null; attempt: number | null; answers: Record<string, string>;
  recording: { saved: boolean; note: string; seconds: number };
  analysis: { status: "pending" | "done" | "failed" | "off" | "none"; note?: string; by?: string };
  final_heard: string | null; summary: string; has_recording: boolean; qa_count?: number;
  qa?: QA[]; transcript?: { speaker: "agent" | "person"; text: string }[]; keys_pressed?: string[]; event_title?: string | null;
}
export interface RetryPolicy { max_attempts: number; gap_hours: number }
export const SCRIPT_FIELDS = ["greeting", "message", "menu", "voicemail", "goodbye"] as const;
export type Script = Record<(typeof SCRIPT_FIELDS)[number], string> & {
  questions?: Record<string, string>;
  doubts?: string;  // closing "any other questions?" when the assistant is on
};
/** A follow-up keypad question after the main 1/2/3 answer; option n is key n. */
export interface Question { id: string; label: string; options: string[]; only_if_confirmed: boolean }
export interface QuestionResult extends Question {
  answered: number; results: { key: string; label: string; count: number }[];
}
export const MAX_QUESTIONS = 4, MIN_OPTIONS = 2, MAX_OPTIONS = 6;
/** English fallback for a question's spoken text (same wording as the backend's). */
export const questionText = (q: Question) =>
  `${q.label}. Press ${q.options.map((o, i) => `${i + 1} for ${o}`).join(", ")}.`;
export interface Detail extends Summary {
  by_language: Group[]; by_segment: Group[];
  handling: { audio: Handling; text: Handling; recordings: Handling };
  retry_estimate_inr: number; retry_policy: RetryPolicy; note: string | null;
  provider: ProviderKey; mode: Mode; voice: string; system_prompt: string;
  ivr: Record<string, Record<string, boolean>>; synthesising: boolean;
  scripts: (Script & { language: string; code: string })[];
  questions: QuestionResult[];
  recipients: Recipient[];
}

// Campaign builder
export type Kind = string; // a preset key (seminar, clinic, school, payment) or a custom label
export type ProviderKey = "elevenlabs" | "gemini";
export type Mode = "live" | "hybrid";
export interface Cap { supported: boolean; ready: boolean; missing: string[] }
export interface BuilderProvider {
  key: ProviderKey; label: string; region: string; sends: string; caps: { draft: Cap; voice: Cap; live: Cap };
}
export interface ProviderItem {
  key: ProviderKey; label: string; region: string; sends: string; default: boolean; available: boolean; model: string | null;
  caps: (Cap & { key: string; label: string })[];
}
export interface ProvidersInfo {
  providers: ProviderItem[]; default: ProviderKey;
  webphone: { connected: number; path: string; token_required: boolean };
}
export interface ProviderCheck { ok: boolean; model?: string; connect_ms?: number; first_audio_ms?: number; error?: string }
export interface BuilderOptions {
  languages: { code: string; name: string }[];
  kinds: { value: Kind; label: string }[];
  providers: BuilderProvider[]; default_provider: ProviderKey;
  phones: number;
}
export interface ChatGPTStatus { connected: boolean; email: string | null; redirect_uri: string }
export const PREFILL_KEY = "reachout.prefill";
export interface AssistantReply {
  reply: string; ready: boolean; event: EventDetails | null; languages: string[]; language_names: string[];
  missing: { key: string; label: string }[]; suggested: ("kind" | "title" | "details")[]; provider: string; provider_key: string; warnings: string[];
}
/** Handed from the assistant to the builder through sessionStorage. */
export interface Prefill {
  event: EventDetails; languages: string[]; provider?: ProviderKey; draft?: Draft; warnings?: string[];
}
export interface EventDetails { org: string; kind: Kind; title: string; date: string; time: string; venue: string; details: string }
export interface Draft {
  name: string; scripts: Record<string, Script & { placeholder: boolean }>; questions: Question[]; retry: RetryPolicy;
  notes: string; provider: string; system_prompt: string;
}
export interface ContactsCheck {
  count: number; by_language: Record<string, number>; segments: Record<string, number>;
  errors: { line: number; error: string }[]; error_count: number;
  preview: { name: string; phone: string; language: string; segment: string }[];
}
export interface Estimate {
  lines: { label: string; detail: string; inr: number }[];
  total_inr: number; per_recipient_inr: number; recipients: number;
  expected_calls: number; expected_answered: number; all_agent_inr: number;
  assumptions: { pickup: number; max_attempts: number; call_seconds: number; escalation_rate: number };
  handling: Detail["handling"];
}
