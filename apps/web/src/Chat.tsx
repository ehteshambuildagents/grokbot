import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ACTIVE, WAITING, api, fmtTime, type Bot, type Message, type Task } from "./api";
import { Button, ErrorBox, Loading, RichText, Status, cx, inputCls, useApp, useEvents, useLoad } from "./ui";

export function Conversation({ convId, bots, title }: { convId: string; bots: Bot[]; title: string }) {
  const { user, toast } = useApp();
  const msgs = useLoad(() => api<Message[]>(`/api/conversations/${convId}/messages`), [convId]);
  const [tasks, setTasks] = useState<Record<string, Task>>({});
  const [deltas, setDeltas] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [showActivity, setShowActivity] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const botById = useMemo(() => Object.fromEntries(bots.map((b) => [b.id, b])), [bots]);

  const loadTasks = useCallback(() => api<Task[]>(`/api/tasks?limit=50`)
    .then((list) => setTasks(Object.fromEntries(list.filter((t) => t.conversation_id === convId).map((t) => [t.id, t])))), [convId]);
  useEffect(() => { loadTasks(); }, [loadTasks]);
  useEffect(() => { endRef.current?.scrollIntoView({ block: "end" }); }, [msgs.data?.length, deltas]);

  useEvents((e) => {
    if (!e.task_id) return;
    if (e.type === "delta") setDeltas((d) => ({ ...d, [e.task_id]: (d[e.task_id] ?? "") + e.data.text }));
    if (e.type === "status" || e.type === "progress" || e.type === "approval_requested" || e.type === "handoff") {
      loadTasks();
      if (e.type === "status" && ["succeeded", "failed", "cancelled"].includes(e.data.status)) {
        setDeltas((d) => { const n = { ...d }; delete n[e.task_id]; return n; });
        msgs.reload();
      }
    }
  });

  const live = Object.values(tasks).filter((t) => ACTIVE.includes(t.status) || WAITING.includes(t.status));

  return (
    <div className="flex h-full min-h-0">
      <section className="flex min-w-0 flex-1 flex-col" aria-label="Conversation">
        <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-2">
          <div className="min-w-0">
            <h1 className="truncate font-semibold">{title}</h1>
            <p className="truncate text-xs text-muted">{bots.map((b) => `${b.avatar} ${b.name}`).join(" · ")}</p>
          </div>
          <Button kind="ghost" aria-pressed={showActivity} onClick={() => setShowActivity((v) => !v)}>Activity</Button>
        </header>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
          {msgs.loading && !msgs.data && <Loading />}
          {msgs.error && <ErrorBox error={msgs.error} retry={msgs.reload} />}
          {msgs.data?.length === 0 && (
            <p className="text-center text-sm text-muted">No messages yet. Tasks keep running in the background even if you close this page.</p>
          )}
          {msgs.data?.map((m) => {
            const bot = m.author_bot_id ? botById[m.author_bot_id] : null;
            return (
              <article key={m.id} className={cx("max-w-[52rem] rounded-xl px-3 py-2 text-sm", m.author_user_id ? "ml-auto bg-teal-700 text-white" : "border border-line bg-panel")}>
                <div className={cx("mb-1 flex gap-2 text-xs", m.author_user_id ? "text-teal-100" : "text-muted")}>
                  <span>{bot ? `${bot.avatar} ${bot.name}` : m.author_user_id ? "You" : "Bot"}</span>
                  <time>{fmtTime(m.created_at, user.timezone)}</time>
                  {m.task_id && !m.author_user_id && (
                    <button className="underline" onClick={() => { setSelected(m.task_id); setShowActivity(true); }}>evidence</button>
                  )}
                </div>
                <RichText text={m.content} />
              </article>
            );
          })}
          {live.map((t) => (
            <TaskCard key={t.id} t={t} bot={botById[t.bot_id]} delta={deltas[t.id]} onOpen={() => { setSelected(t.id); setShowActivity(true); }} onChange={loadTasks} />
          ))}
          <div ref={endRef} />
        </div>
        <Composer convId={convId} bots={bots} onSent={(ids) => { msgs.reload(); loadTasks(); if (ids[0]) setSelected(ids[0]); }} toast={toast} />
      </section>
      {showActivity && (
        <aside className="fixed inset-0 z-30 flex flex-col bg-panel lg:static lg:z-auto lg:w-[26rem] lg:border-l lg:border-line" aria-label="Activity">
          <div className="flex items-center justify-between border-b border-line px-4 py-2">
            <h2 className="font-semibold">Activity</h2>
            <button className="rounded px-2 text-lg" aria-label="Close activity" onClick={() => setShowActivity(false)}>×</button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            <div className="mb-3 flex flex-wrap gap-1">
              {Object.values(tasks).slice(0, 12).map((t) => (
                <button key={t.id} onClick={() => setSelected(t.id)} className={cx("rounded-md border px-2 py-1 text-xs", selected === t.id ? "border-teal-700" : "border-line")}>
                  {botById[t.bot_id]?.avatar} {t.id.slice(0, 6)} <Status s={t.status} />
                </button>
              ))}
            </div>
            {selected ? <TaskDetail id={selected} /> : <p className="text-sm text-muted">Select a task to see its plan, tool calls, evidence and artifacts.</p>}
          </div>
        </aside>
      )}
    </div>
  );
}

function TaskCard({ t, bot, delta, onOpen, onChange }: { t: Task; bot?: Bot; delta?: string; onOpen: () => void; onChange: () => void }) {
  const { toast, go } = useApp();
  const [redirect, setRedirect] = useState("");
  const act = async (path: string, body?: unknown) => {
    try { await api(`/api/tasks/${t.id}/${path}`, { method: "POST", body: body ?? {} }); onChange(); }
    catch (e: any) { toast(e.message, "err"); }
  };
  return (
    <div className="rounded-xl border border-line bg-panel p-3 text-sm" aria-label={`Task ${t.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span>{bot?.avatar} {bot?.name ?? "Bot"}</span>
        <Status s={t.status} />
        <span className="text-xs text-muted">{t.progress}</span>
        <span className="ml-auto text-xs text-muted">tools {t.tool_calls_used}/{t.tool_budget}</span>
      </div>
      {delta && <p className="mt-2 whitespace-pre-wrap text-muted">{delta.slice(-600)}</p>}
      {t.status === "waiting_for_approval" && (
        <p className="mt-2">Needs your approval. <button className="underline" onClick={() => go("#/approvals")}>Review</button></p>
      )}
      {t.status === "waiting_for_user" && <Blocker t={t} act={act} />}
      {t.status === "retry_wait" && <p className="mt-2 text-amber-700 dark:text-amber-300">{t.error}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        <Button onClick={onOpen}>Details</Button>
        <Button kind="danger" onClick={() => act("cancel")}>Cancel</Button>
        <form className="flex min-w-[12rem] flex-1 gap-1" onSubmit={(e) => { e.preventDefault(); if (redirect.trim()) { act("redirect", { text: redirect }); setRedirect(""); } }}>
          <input className={inputCls} aria-label="Redirect this task" placeholder="Redirect: new instruction…" value={redirect} onChange={(e) => setRedirect(e.target.value)} />
          <Button type="submit">Send</Button>
        </form>
      </div>
    </div>
  );
}

function Blocker({ t, act }: { t: Task; act: (p: string, b?: unknown) => void }) {
  const { go } = useApp();
  const k = t.blocker?.kind;
  return (
    <div className="mt-2 rounded-lg bg-amber-50 p-2 text-amber-900 dark:bg-amber-950 dark:text-amber-100">
      <p className="font-medium">Blocked: {String(k).replace(/_/g, " ")}</p>
      <p className="text-xs">{t.blocker?.detail}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {["human_required", "session_expired", "control_revoked"].includes(k) && <Button onClick={() => go("#/computer")}>Open Computer</Button>}
        {String(k).startsWith("ambiguous") ? (
          <>
            <Button onClick={() => act("resume", { resolution: "assume_done" })}>It happened</Button>
            <Button onClick={() => act("resume", { resolution: "retry" })}>Retry once</Button>
            <Button onClick={() => act("resume", { resolution: "skip" })}>Skip</Button>
          </>
        ) : <Button onClick={() => act("resume")}>Resume</Button>}
      </div>
    </div>
  );
}

export function TaskDetail({ id }: { id: string }) {
  const { user } = useApp();
  const d = useLoad(() => api<any>(`/api/tasks/${id}`), [id]);
  useEvents((e) => { if (e.task_id === id && e.type !== "delta") d.reload(); });
  if (d.error) return <ErrorBox error={d.error} />;
  if (!d.data) return <Loading />;
  const t = d.data;
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2"><Status s={t.status} /><span className="text-xs text-muted">phase {t.phase} · attempts {t.attempts} · {fmtTime(t.created_at, user.timezone)}</span></div>
      {t.error && <ErrorBox error={t.error} />}
      {t.handoff?.objective && (
        <div className="rounded-lg border border-line p-2 text-xs">
          <p className="font-medium">Handoff from {t.handoff.from_bot} → {t.handoff.owner_bot}</p>
          <p>Objective: {t.handoff.objective}</p><p>Deliverable: {t.handoff.deliverable || "answer"}</p>
          <p>Done when: {t.handoff.completion_criteria}</p><p>Budget {t.handoff.budget} · deadline {fmtTime(t.handoff.deadline, user.timezone)} · permissions {t.handoff.permissions}</p>
        </div>
      )}
      {t.plan?.summary && <div><h3 className="font-medium">Plan</h3><p>{t.plan.summary}</p><ul className="list-disc pl-5">{t.plan.steps?.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul></div>}
      <div>
        <h3 className="font-medium">Timeline</h3>
        <ol className="space-y-1 border-l border-line pl-3">
          {t.steps.map((s: any) => <li key={s.idx + s.at}><span className="text-xs text-muted">{s.kind}</span> {s.summary}</li>)}
        </ol>
      </div>
      {t.tool_calls.length > 0 && (
        <div>
          <h3 className="font-medium">Tool evidence</h3>
          {t.tool_calls.map((c: any) => (
            <details key={c.id} className="rounded-lg border border-line p-2">
              <summary className="cursor-pointer">#{c.seq} {c.tool} <Status s={c.status === "succeeded" ? "succeeded" : c.status === "failed" || c.status === "denied" ? "failed" : "queued"} /></summary>
              <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify({ args: c.args, result: c.result }, null, 1)}</pre>
            </details>
          ))}
        </div>
      )}
      {t.artifacts.length > 0 && (
        <div><h3 className="font-medium">Deliverables</h3>
          <ul>{t.artifacts.map((a: any) => <li key={a.id}><a className="text-teal-700 underline dark:text-teal-400" href={a.url} download>{a.name}</a> <span className="text-xs text-muted">v{a.version} · {a.size} B</span></li>)}</ul>
        </div>
      )}
      {t.children.length > 0 && <div><h3 className="font-medium">Delegated</h3>{t.children.map((c: any) => <p key={c.id}>{c.id.slice(0, 8)} <Status s={c.status} /></p>)}</div>}
      {t.status === "succeeded" && <SkillFromTask id={id} />}
    </div>
  );
}

function SkillFromTask({ id }: { id: string }) {
  const { toast, go } = useApp();
  return <Button onClick={async () => { try { await api(`/api/skills/from-task/${id}`, { method: "POST" }); toast("Draft skill created for review"); go("#/skills"); } catch (e: any) { toast(e.message, "err"); } }}>Save as skill (draft)</Button>;
}

function Composer({ convId, bots, onSent, toast }: { convId: string; bots: Bot[]; onSent: (ids: string[]) => void; toast: (m: string, k?: "ok" | "err") => void }) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<string[]>([]);
  const [skill, setSkill] = useState("");
  const [deliverable, setDeliverable] = useState("");
  const [busy, setBusy] = useState(false);
  const [opts, setOpts] = useState(false);
  const [skills, setSkills] = useState<any[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => { api<any[]>("/api/skills").then((s) => setSkills(s.filter((x) => x.versions.some((v: any) => v.status === "approved")))).catch(() => {}); }, []);

  const send = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      const r = await api<any>(`/api/conversations/${convId}/messages`, {
        body: { content: text, attachments, skill_id: skill || null, deliverable: deliverable || null },
      });
      setText(""); setAttachments([]); setDeliverable("");
      onSent(r.task_ids);
    } catch (e: any) { toast(e.message, "err"); }
    setBusy(false);
  };
  const upload = async (f: File) => {
    const form = new FormData();
    form.append("file", f);
    try { const r = await api<any>("/api/files/upload?dir=uploads", { form }); setAttachments((a) => [...a, r.path]); }
    catch (e: any) { toast(e.message, "err"); }
  };
  return (
    <form className="border-t border-line bg-panel p-3" onSubmit={(e) => { e.preventDefault(); send(); }}>
      {bots.length > 1 && (
        <div className="mb-2 flex flex-wrap gap-1 text-xs" aria-label="Mention a bot">
          {bots.map((b) => <button type="button" key={b.id} className="rounded-full border border-line px-2 py-0.5" onClick={() => setText((t) => `${t}@${b.name} `)}>@{b.name}</button>)}
        </div>
      )}
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1 text-xs">{attachments.map((a) => (
          <span key={a} className="rounded-full bg-hover px-2 py-0.5">📎 {a} <button type="button" aria-label={`Remove ${a}`} onClick={() => setAttachments((x) => x.filter((y) => y !== a))}>×</button></span>
        ))}</div>
      )}
      <textarea
        aria-label="Message" rows={2} value={text} onChange={(e) => setText(e.target.value)} className={inputCls + " resize-y"}
        placeholder={bots.length > 1 ? "Message the group — @mention a bot to assign it" : `Message ${bots[0]?.name ?? ""}… (Enter to send, Shift+Enter for newline)`}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
      />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input ref={fileRef} type="file" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }} />
        <Button type="button" onClick={() => fileRef.current?.click()}>Attach</Button>
        <Button type="button" kind="ghost" className="sm:hidden" aria-expanded={opts} onClick={() => setOpts((v) => !v)}>Options</Button>
        <div className={cx("w-full flex-wrap gap-2 sm:flex sm:w-auto", opts ? "flex" : "hidden")}>
        <select aria-label="Skill" className={inputCls + " w-auto"} value={skill} onChange={(e) => setSkill(e.target.value)}>
          <option value="">No skill</option>
          {skills.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <input aria-label="Deliverable file name" className={inputCls + " w-44"} placeholder="deliverable e.g. report.md" value={deliverable} onChange={(e) => setDeliverable(e.target.value)} />
        </div>
        <Button kind="primary" type="submit" disabled={busy || !text.trim()} className="ml-auto">{busy ? "Sending…" : "Send"}</Button>
      </div>
    </form>
  );
}
