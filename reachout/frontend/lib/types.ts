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
  scripts: (Script & { language: string })[];
  questions: QuestionResult[];
  recipients: Recipient[];
}

// Campaign builder
export type Kind = "seminar" | "clinic" | "school" | "payment";
export type TextProvider = "template" | "chatgpt" | "ollama" | "sarvam";
export type VoiceProvider = "piper" | "sarvam" | "elevenlabs";
export interface Provider { key: string; label: string; region: string; sends: string; available?: boolean; live?: boolean }
export interface AgentProvider { key: string; label: string; region: string; sends: string; setup: string; available: boolean }
export interface ProviderItem {
  key: string; label: string; region: string; sends: string; available: boolean; setup: string;
  default?: boolean; model?: string | null; checkable?: boolean; live?: boolean;
}
export interface ProviderGroup { key: string; title: string; help: string; items: ProviderItem[] }
export interface GeminiCheck { ok: boolean; model: string; connect_ms?: number; first_audio_ms?: number; error?: string }
export interface ChatGPTStatus { connected: boolean; email: string | null; redirect_uri: string }
export interface BuilderOptions {
  languages: { code: string; name: string }[];
  kinds: { value: Kind; label: string }[];
  text_providers: Provider[]; voice_providers: Provider[]; chatgpt: ChatGPTStatus;
  escalation: { available: boolean; label: string; sends: string };
  agent_providers: AgentProvider[]; default_agent: string;
  launch_mode: "live" | "simulated" | "unavailable";
  call_window: string;
}
export const PREFILL_KEY = "reachout.prefill";
export interface AssistantReply {
  reply: string; ready: boolean; event: EventDetails | null; languages: string[]; language_names: string[];
  missing: { key: string; label: string }[]; suggested: ("title" | "details")[]; provider: string; provider_key: TextProvider; warnings: string[];
}
/** Handed from the assistant to the builder through sessionStorage. */
export interface Prefill {
  event: EventDetails; languages: string[]; text_provider?: TextProvider; draft?: Draft; warnings?: string[];
}
export interface EventDetails { org: string; kind: Kind; title: string; date: string; time: string; venue: string; details: string }
export interface Draft {
  name: string; scripts: Record<string, Script & { placeholder: boolean }>; questions: Question[]; retry: RetryPolicy;
  notes: string; provider: TextProvider;
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
