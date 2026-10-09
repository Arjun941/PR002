"use client";
import { MAX_OPTIONS, MAX_QUESTIONS, MIN_OPTIONS, type Question } from "@/lib/types";
import { Icon } from "./Icon";

/** The call's questions: the fixed main answer, then the follow-ups the AI chose for this event (editable). */
export function QuestionsEditor({ questions, onChange, escalation, payment }: {
  questions: Question[]; onChange: (next: Question[]) => void; escalation: boolean; payment: boolean;
}) {
  const set = (i: number, patch: Partial<Question>) => onChange(questions.map((q, j) => j === i ? { ...q, ...patch } : q));
  const setOption = (i: number, k: number, v: string) => set(i, { options: questions[i].options.map((o, j) => j === k ? v : o) });
  const add = () => {
    const used = new Set(questions.map(q => q.id));
    const id = [...Array(used.size + 1)].map((_, n) => `q${n + 1}`).find(x => !used.has(x))!;
    onChange([...questions, { id, label: "", options: ["", ""], only_if_confirmed: true }]);
  };
  const main = payment ? ["Will pay by then", "Already paid", "Needs more time"] : ["Confirm", "Can't make it", "Reschedule"];

  return (
    <section className="card">
      <div className="card-head"><h2>Questions on the call</h2>
        <span className="muted" style={{ fontSize: 13 }}>Each answer is one key press</span>
      </div>
      <div className="qlist">
        <div className="q main">
          <div className="q-head"><b>Main answer</b><span className="chip">Asked to everyone</span></div>
          <div className="q-opts">
            {main.map((o, k) => <span key={o} className="q-opt fixed"><kbd>{k + 1}</kbd>{o}</span>)}
          </div>
        </div>
        {questions.map((q, i) => (
          <div className="q" key={q.id}>
            <div className="q-head">
              <input className="input" aria-label={`Question ${i + 1}`} placeholder="What do you want to ask? e.g. Food preference"
                maxLength={60} value={q.label} onChange={e => set(i, { label: e.target.value })} />
              <label className="check small">
                <input type="checkbox" checked={q.only_if_confirmed} onChange={e => set(i, { only_if_confirmed: e.target.checked })} />
                <span>Only people who confirm</span>
              </label>
              <button className="btn sm ghost" aria-label={`Remove question ${i + 1}`} onClick={() => onChange(questions.filter((_, j) => j !== i))}>
                <Icon name="x" size={14} />
              </button>
            </div>
            <div className="q-opts">
              {q.options.map((o, k) => (
                <span key={k} className="q-opt">
                  <kbd>{k + 1}</kbd>
                  <input className="input" aria-label={`Option ${k + 1}`} maxLength={40} value={o} placeholder="Option"
                    onChange={e => setOption(i, k, e.target.value)} />
                  {q.options.length > MIN_OPTIONS && (
                    <button className="q-x" aria-label={`Remove option ${k + 1}`}
                      onClick={() => set(i, { options: q.options.filter((_, j) => j !== k) })}><Icon name="x" size={12} /></button>
                  )}
                </span>
              ))}
              {q.options.length < MAX_OPTIONS && (
                <button className="btn sm ghost" onClick={() => set(i, { options: [...q.options, ""] })}><Icon name="plus" size={12} />Option</button>
              )}
            </div>
          </div>
        ))}
        {escalation && (
          <div className="q main">
            <div className="q-head"><b>At the end: any other questions?</b><span className="chip">Asked to everyone who answered</span></div>
            <span className="muted" style={{ fontSize: 13 }}>If the caller starts asking something, the voice assistant answers. If they stay quiet, the call says goodbye.</span>
          </div>
        )}
      </div>
      <div className="table-foot">
        <span>{questions.length ? "Changed a question or its options? Update its spoken text in each language below."
          : `No follow-up questions: after the main answer the call ${escalation ? "asks for any other questions" : "ends"}.`}</span>
        {questions.length < MAX_QUESTIONS && <button className="btn sm" onClick={add}><Icon name="plus" size={12} />Add question</button>}
      </div>
    </section>
  );
}
