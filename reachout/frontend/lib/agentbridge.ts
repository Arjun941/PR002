import type { AgentEdits, ChatTurn } from "@/lib/types";
import { CHAT_KEY } from "@/lib/types";

/** What the campaign agent (the chat in the dock) can do to the builder form, which lives in the page's own state. */
export interface BuilderBridge {
  /** The form as it is now, for the agent's context (no contact details). */
  snapshot(): unknown;
  /** Applies the agent's edits and actions; returns the names of what was changed. */
  apply(edits: AgentEdits, actions: string[]): string[];
}

let current: BuilderBridge | null = null;
export const builderBridge = () => current;
/** The builder page registers itself while it is mounted. Returns the cleanup. */
export const registerBuilder = (b: BuilderBridge) => { current = b; return () => { if (current === b) current = null; }; };

/** The builder's agent chat lives in sessionStorage until the campaign is created, then moves onto the campaign. */
export const loadChat = (): ChatTurn[] => {
  try {
    const v = JSON.parse(sessionStorage.getItem(CHAT_KEY) || "[]");
    return Array.isArray(v) ? v.filter(t => t && (t.role === "user" || t.role === "assistant") && typeof t.content === "string") : [];
  } catch { return []; }
};
export const saveChat = (turns: ChatTurn[]) => { try { sessionStorage.setItem(CHAT_KEY, JSON.stringify(turns.slice(-200))); } catch { /* private mode */ } };
export const clearChat = () => { try { sessionStorage.removeItem(CHAT_KEY); } catch { /* ignore */ } };
