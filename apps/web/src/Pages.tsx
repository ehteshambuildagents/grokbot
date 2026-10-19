import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmtTime, type Bot, type Conversation, type Task } from "./api";
import { TaskDetail } from "./Chat";
import { McpServers, ToolPicker } from "./Mcp";
import { Button, Dialog, Empty, ErrorBox, Field, Loading, Page, Status, cx, inputCls, useApp, useEvents, useLoad } from "./ui";

const err = (toast: any) => (e: any) => toast(e.message ?? String(e), "err");

// ---------------------------------------------------------------- bot editor
export function BotEditor({ id }: { id?: string }) {
  const { refreshBots, toast, go } = useApp();
  const tools = useLoad(() => api<any[]>("/api/tools"));
  const models = useLoad(() => api<any>("/api/models"));
  const existing = useLoad(() => (id ? api<Bot>(`/api/bots/${id}`) : Promise.resolve(null)), [id]);
  const [edits, setF] = useState<any>(null);
  const [impact, setImpact] = useState<any>(null);
  const f = edits ?? existing.data ?? (!id && tools.data ? { name: "", avatar: "🤖", role: "", instructions: "", model: "",
    tools: tools.data.filter((t) => t.default).map((t) => t.name), policy_profile: "standard", memory_enabled: true } : null);
  if (existing.error) return <Page title="Bot"><ErrorBox error={existing.error} /></Page>;
  if (!f || !tools.data) return <Loading />;
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  const save = async () => {
    const body = { name: f.name, avatar: f.avatar, role: f.role, instructions: f.instructions, model: f.model, tools: f.tools, policy_profile: f.policy_profile, memory_enabled: f.memory_enabled };
    try {
      const b = id ? await api<Bot>(`/api/bots/${id}`, { method: "PATCH", body }) : await api<Bot>("/api/bots", { body });
      await refreshBots(); toast("Saved"); go(`#/bot/${b.id}`);
    } catch (e: any) { toast(e.message, "err"); }
  };
  const chosen = models.data?.models?.find((m: any) => m.name === (f.model || models.data?.default));
  return (
    <Page title={id ? `Edit ${f.name}` : "New bot"} actions={id && (
      <>
        <Button onClick={async () => { const b = await api<Bot>(`/api/bots/${id}/duplicate`, { method: "POST" }).catch(err(toast)); if (b) { await refreshBots(); go(`#/bot/${b.id}/edit`); } }}>Duplicate</Button>
        <Button onClick={async () => { await api(`/api/bots/${id}`, { method: "PATCH", body: { pinned: !f.pinned } }); set("pinned", !f.pinned); refreshBots(); }}>{f.pinned ? "Unpin" : "Pin"}</Button>
        <Button onClick={async () => { await api(`/api/bots/${id}`, { method: "PATCH", body: { hidden: !f.hidden } }); set("hidden", !f.hidden); refreshBots(); toast(f.hidden ? "Visible again" : "Hidden. Its routines keep running."); }}>{f.hidden ? "Unhide" : "Hide"}</Button>
        <a className="rounded-lg border border-line px-3 py-1.5 text-sm" href={`/api/bots/${id}/template`} download={`${f.name}.localcrew.json`}>Export template</a>
        <Button kind="danger" onClick={async () => setImpact(await api(`/api/bots/${id}/deletion-impact`))}>Delete…</Button>
      </>
    )}>
      <div className="grid gap-4 md:grid-cols-2">
        <Field label="Name"><input className={inputCls} value={f.name} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Avatar (emoji)"><input className={inputCls} value={f.avatar} maxLength={4} onChange={(e) => set("avatar", e.target.value)} /></Field>
        <Field label="Role"><input className={inputCls} value={f.role} onChange={(e) => set("role", e.target.value)} /></Field>
        <Field label="Model" hint={chosen ? `tools: ${chosen.tools ? "yes" : "no"} · vision: ${chosen.vision ? "yes" : "no"} · context ${chosen.context_length}` : models.data?.ollama?.ok === false ? "Ollama is not reachable" : ""}>
          <select className={inputCls} value={f.model} onChange={(e) => set("model", e.target.value)}>
            <option value="">Default ({models.data?.default ?? "…"})</option>
            {models.data?.models?.map((m: any) => <option key={m.name} value={m.name}>{m.name}{m.remote ? " (API: data leaves this machine)" : ""}</option>)}
          </select>
        </Field>
        <Field label="Policy profile" hint="standard: external writes & commands need approval · cautious: workspace writes too · read_only: no writes">
          <select className={inputCls} value={f.policy_profile} onChange={(e) => set("policy_profile", e.target.value)}>
            <option>standard</option><option>cautious</option><option>read_only</option>
          </select>
        </Field>
        <label className="flex items-center gap-2 pt-6 text-sm"><input type="checkbox" checked={f.memory_enabled} onChange={(e) => set("memory_enabled", e.target.checked)} /> Memory enabled</label>
      </div>
      <div className="mt-4"><Field label="Durable instructions"><textarea rows={5} className={inputCls} value={f.instructions} onChange={(e) => set("instructions", e.target.value)} /></Field></div>
      <ToolPicker tools={tools.data} value={f.tools} onChange={(v) => set("tools", v)} />
      <div className="mt-4 flex gap-2"><Button kind="primary" onClick={save} disabled={!f.name}>Save</Button>{id && <Button onClick={() => go(`#/bot/${id}`)}>Back to chat</Button>}</div>
      <Dialog open={!!impact} title={`Delete ${f.name}?`} onClose={() => setImpact(null)}>
        {impact && (
          <div className="space-y-2 text-sm">
            <ul className="list-disc pl-5">
              <li>{impact.routines_deleted} routine(s) will be deleted and stop running.</li>
              <li>{impact.active_tasks_cancelled} active task(s) will be cancelled; {impact.tasks_and_history_deleted} task record(s) deleted.</li>
              <li>{impact.bot_memories_deleted} private bot memories will be deleted.</li>
              <li>{impact.messages_kept_with_author_removed} conversation message(s) are kept, without the bot as author.</li>
              <li>{impact.artifacts_kept} deliverable(s) are kept. Shared workspace files and browser sign-ins are kept.</li>
            </ul>
            <Button kind="danger" onClick={async () => { await api(`/api/bots/${id}?confirm=true`, { method: "DELETE" }).catch(err(toast)); await refreshBots(); go("#/"); }}>Delete permanently</Button>
          </div>
        )}
      </Dialog>
    </Page>
  );
}

// ---------------------------------------------------------------- groups
export function Groups() {
  const { bots, go, toast } = useApp();
  const convs = useLoad(() => api<Conversation[]>("/api/conversations?kind=group"));
  const [sel, setSel] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  return (
    <Page title="Groups">
      <div className="mb-6 rounded-xl border border-line bg-panel p-4">
        <h2 className="mb-2 font-medium">New group</h2>
        <div className="mb-2 flex flex-wrap gap-2">
          {bots.map((b) => <label key={b.id} className="flex items-center gap-1 text-sm"><input type="checkbox" checked={sel.includes(b.id)} onChange={(e) => setSel(e.target.checked ? [...sel, b.id] : sel.filter((x) => x !== b.id))} />{b.avatar} {b.name}</label>)}
        </div>
        <div className="flex gap-2"><input className={inputCls} placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
          <Button kind="primary" disabled={sel.length < 2} onClick={async () => { const c = await api<Conversation>("/api/conversations", { body: { bot_ids: sel, title: title || "Group" } }).catch(err(toast)); if (c) go(`#/group/${c.id}`); }}>Create</Button></div>
      </div>
      {convs.loading ? <Loading /> : convs.data?.length ? (
        <ul className="space-y-2">{convs.data.map((c) => <li key={c.id}><button className="w-full rounded-lg border border-line bg-panel p-3 text-left hover:bg-hover" onClick={() => go(`#/group/${c.id}`)}>{c.title} <span className="text-xs text-muted">{c.bot_ids.length} bots</span></button></li>)}</ul>
      ) : <Empty>No groups yet. Select at least two bots.</Empty>}
    </Page>
  );
}

// ---------------------------------------------------------------- tasks
export function Tasks({ id }: { id?: string }) {
  const { user, bots, go } = useApp();
  const [filter, setFilter] = useState("");
  const list = useLoad(() => api<Task[]>(`/api/tasks${filter ? `?status=${filter}` : ""}`), [filter]);
  useEvents((e) => { if (e.type === "status") list.reload(); });
  const name = (bid: string) => bots.find((b) => b.id === bid)?.name ?? "deleted bot";
  return (
    <Page title="Tasks" actions={
      <select aria-label="Filter by status" className={inputCls} value={filter} onChange={(e) => setFilter(e.target.value)}>
        <option value="">All</option><option value="queued,planning,running,retry_wait">Active</option>
        <option value="waiting_for_user,waiting_for_approval">Waiting</option><option value="succeeded">Succeeded</option><option value="failed,cancelled">Failed / cancelled</option>
      </select>}>
      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          {list.error && <ErrorBox error={list.error} retry={list.reload} />}
          {list.data?.length === 0 && <Empty>No tasks.</Empty>}
          <ul className="space-y-1">{list.data?.map((t) => (
            <li key={t.id}><button onClick={() => go(`#/task/${t.id}`)} className={cx("w-full rounded-lg border bg-panel p-2 text-left text-sm hover:bg-hover", id === t.id ? "border-teal-700" : "border-line")}>
              <div className="flex items-center gap-2"><Status s={t.status} /><span className="truncate">{name(t.bot_id)}</span><span className="ml-auto text-xs text-muted">{fmtTime(t.created_at, user.timezone)}</span></div>
              <p className="truncate text-xs text-muted">{t.objective.split("Current request:\n").pop()}</p>
            </button></li>
          ))}</ul>
        </div>
        <div>{id ? <TaskDetail id={id} /> : <Empty>Select a task.</Empty>}</div>
      </div>
    </Page>
  );
}

// ---------------------------------------------------------------- approvals
export function Approvals() {
  const { user, toast } = useApp();
  const [status, setStatus] = useState("pending");
  const list = useLoad(() => api<any[]>(`/api/approvals?status=${status}`), [status]);
  useEvents((e) => { if (e.type === "approval_requested") list.reload(); });
  const decide = async (a: any, decision: string, scope = "once") => {
    try { await api(`/api/approvals/${a.id}/decide`, { body: { decision, scope, digest: a.digest } }); toast(decision === "approve" ? "Approved" : "Denied"); list.reload(); }
    catch (e: any) { toast(e.message, "err"); list.reload(); }
  };
  return (
    <Page title="Approvals" actions={<select aria-label="Status" className={inputCls} value={status} onChange={(e) => setStatus(e.target.value)}>{["pending", "approved", "denied", "expired", "consumed", "all"].map((s) => <option key={s}>{s}</option>)}</select>}>
      {list.loading && <Loading />}
      {list.data?.length === 0 && <Empty>Nothing waiting for approval.</Empty>}
      <div className="space-y-3">{list.data?.map((a) => (
        <article key={a.id} className="rounded-xl border border-line bg-panel p-4 text-sm">
          <div className="flex flex-wrap items-center gap-2"><Status s={a.status === "pending" ? "waiting_for_approval" : a.status === "approved" || a.status === "consumed" ? "succeeded" : "failed"} /><b>{a.operation}</b><span className="text-muted">→ {a.target}</span></div>
          <dl className="mt-2 grid grid-cols-[8rem_1fr] gap-x-2 gap-y-1">
            <dt className="text-muted">Acting identity</dt><dd>{a.acting_identity.bot_name} on behalf of you</dd>
            <dt className="text-muted">Expected effect</dt><dd>{a.expected_effect}</dd>
            <dt className="text-muted">Expires</dt><dd>{fmtTime(a.expires_at, user.timezone)}</dd>
            <dt className="text-muted">Scope</dt><dd>this exact action (digest {a.digest.slice(0, 12)}…)</dd>
          </dl>
          <details className="mt-2" open><summary className="cursor-pointer text-muted">Exact payload</summary><pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-hover p-2 text-xs">{JSON.stringify(a.payload, null, 2)}</pre></details>
          {a.status === "pending" && (
            <div className="mt-3 flex flex-wrap gap-2">
              <Button kind="primary" onClick={() => decide(a, "approve")}>Allow once</Button>
              <Button onClick={() => decide(a, "approve", "rule")} title="Creates a rule for this bot, tool and exact target">Always allow this exact target</Button>
              <Button kind="danger" onClick={() => decide(a, "deny")}>Deny</Button>
            </div>
          )}
        </article>
      ))}</div>
    </Page>
  );
}

// ---------------------------------------------------------------- computer
export function Computer() {
  const { toast } = useApp();
  const [status, setStatus] = useState<any>(null);
  const [shot, setShot] = useState<any>(null);
  const [error, setError] = useState("");
  const [url, setUrl] = useState("");
  const [typed, setTyped] = useState("");
  const [sensitive, setSensitive] = useState(false);
  const [recording, setRecording] = useState<any[] | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const refresh = useCallback(() => Promise.all([api("/api/computer/status"), api("/api/computer/screenshot")])
    .then(([st, sh]) => { setStatus(st); setShot(sh); setError(""); }, (e: any) => setError(e.message)), []);
  useEffect(() => { refresh(); const t = setInterval(refresh, 1500); return () => clearInterval(t); }, [refresh]);
  const human = status?.controller === "human";
  const act = async (body: any) => { try { await api("/api/computer/act", { body }); refresh(); } catch (e: any) { toast(e.message, "err"); } };
  return (
    <Page title="Computer" actions={<>
      {human ? <Button kind="primary" onClick={async () => { const r = await api<any>("/api/computer/release", { method: "POST" }).catch(err(toast)); if (r) toast(`Released. Resumed ${r.resumed_tasks.length} task(s).`); refresh(); }}>Release control</Button>
        : <Button kind="primary" onClick={async () => { await api("/api/computer/takeover", { method: "POST" }).catch(err(toast)); refresh(); }}>Take control</Button>}
      {human && (status?.recording ? <Button onClick={async () => { const r = await api<any>("/api/computer/record/stop", { method: "POST" }); setRecording(r.steps); }}>Stop recording</Button>
        : <Button onClick={() => api("/api/computer/record/start", { method: "POST" }).then(refresh)}>Record demonstration</Button>)}
      <Button kind="danger" onClick={async () => { if (confirm("Delete the browser profile (all sign-ins) for your bots?")) { await api("/api/computer/session", { method: "DELETE" }).catch(err(toast)); refresh(); } }}>Delete sign-ins</Button>
    </>}>
      <p className="mb-3 text-sm text-muted">Live view of the dedicated browser your bots share. While you hold control, automation is paused. Bots never type passwords; sign in here yourself. Typed text marked sensitive (and anything typed into password fields) is never logged or recorded.</p>
      {error && <ErrorBox error={error} retry={refresh} />}
      <p className="mb-2 text-sm">Controller: <b>{status?.controller ?? "…"}</b> {status?.holder && <span className="text-muted">({status.holder.slice(0, 8)})</span>} · {shot?.url}</p>
      {shot?.jpeg_b64 && (
        <img ref={imgRef} alt="Live browser view" src={`data:image/jpeg;base64,${shot.jpeg_b64}`}
          className={cx("w-full rounded-lg border border-line", human && "cursor-crosshair")}
          onClick={(e) => {
            if (!human || !imgRef.current) return;
            const r = imgRef.current.getBoundingClientRect();
            act({ kind: "click", x: ((e.clientX - r.left) / r.width) * 1280, y: ((e.clientY - r.top) / r.height) * 800 });
          }} />
      )}
      {human && (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); act({ kind: "goto", url }); }}><input className={inputCls} aria-label="URL" placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} /><Button type="submit">Go</Button></form>
          <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); act({ kind: "type", text: typed, sensitive }); setTyped(""); }}>
            <input className={inputCls + " flex-1"} type={sensitive ? "password" : "text"} aria-label="Type into the focused field" placeholder="Click a field, then type here" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
            <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={sensitive} onChange={(e) => setSensitive(e.target.checked)} />sensitive</label>
            <Button type="submit">Type</Button>
            {["Enter", "Tab", "Backspace", "Escape"].map((k) => <Button key={k} type="button" onClick={() => act({ kind: "key", key: k })}>{k}</Button>)}
          </form>
        </div>
      )}
      <Dialog open={!!recording} title="Recorded demonstration" onClose={() => setRecording(null)}>
        <pre className="max-h-64 overflow-auto text-xs">{JSON.stringify(recording, null, 1)}</pre>
        <p className="my-2 text-xs text-muted">Converting creates a draft skill with semantic locators. Review it and run one safe validation before relying on it; one recording does not guarantee it generalizes.</p>
        <Button kind="primary" onClick={async () => { await api("/api/skills/from-demonstration", { body: { name: "Recorded workflow", steps: recording } }).catch(err(toast)); setRecording(null); toast("Draft skill created"); }}>Create draft skill</Button>
      </Dialog>
    </Page>
  );
}

// ---------------------------------------------------------------- files & deliverables
export function Files() {
  const { user, toast } = useApp();
  const [path, setPath] = useState("");
  const list = useLoad(() => api<any[]>(`/api/files?path=${encodeURIComponent(path)}`), [path]);
  const arts = useLoad(() => api<any[]>("/api/artifacts"));
  const [view, setView] = useState<any>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <Page title="Files" actions={<>
      <input ref={fileRef} type="file" className="hidden" onChange={async (e) => { const f = e.target.files?.[0]; if (!f) return; const form = new FormData(); form.append("file", f); await api(`/api/files/upload?dir=${encodeURIComponent(path || "uploads")}`, { form }).catch(err(toast)); list.reload(); e.target.value = ""; }} />
      <Button onClick={() => fileRef.current?.click()}>Upload</Button></>}>
      <h2 className="mb-2 font-medium">Shared workspace <span className="text-sm text-muted">/{path}</span></h2>
      {path && <Button kind="ghost" onClick={() => setPath(path.split("/").slice(0, -1).join("/"))}>⬑ Up</Button>}
      {list.error && <ErrorBox error={list.error} />}
      {list.data?.length === 0 && <Empty>Empty folder.</Empty>}
      <ul className="mb-6 divide-y divide-line rounded-lg border border-line bg-panel">{list.data?.map((f) => (
        <li key={f.path} className="flex items-center gap-2 p-2 text-sm">
          <button className="flex-1 truncate text-left" onClick={async () => f.dir ? setPath(f.path) : setView(await api(`/api/files/content?path=${encodeURIComponent(f.path)}`).catch(err(toast)))}>{f.dir ? "📁" : "📄"} {f.name}</button>
          {!f.dir && <a className="text-xs underline" href={`/api/files/download?path=${encodeURIComponent(f.path)}`} download>download</a>}
          {!f.dir && <button className="text-xs text-red-700 underline" onClick={async () => { if (confirm(`Delete ${f.path}? A recoverable copy is kept in .versions`)) { await api(`/api/files?path=${encodeURIComponent(f.path)}`, { method: "DELETE" }).catch(err(toast)); list.reload(); } }}>delete</button>}
        </li>))}</ul>
      <h2 className="mb-2 font-medium">Deliverables</h2>
      {arts.data?.length === 0 && <Empty>No deliverables yet.</Empty>}
      <ul className="divide-y divide-line rounded-lg border border-line bg-panel">{arts.data?.map((a) => (
        <li key={a.id} className="flex flex-wrap items-center gap-2 p-2 text-sm"><a className="text-teal-700 underline dark:text-teal-400" href={a.url} download>{a.name}</a><span className="text-xs text-muted">v{a.version} · {a.size} B · {fmtTime(a.created_at, user.timezone)} · task {a.task_id?.slice(0, 8)}</span></li>))}</ul>
      <Dialog open={!!view} title={view?.path ?? ""} onClose={() => setView(null)}><pre className="whitespace-pre-wrap text-xs">{view?.content}</pre></Dialog>
    </Page>
  );
}

// ---------------------------------------------------------------- skills
export function Skills() {
  const { toast } = useApp();
  const list = useLoad(() => api<any[]>("/api/skills"));
  const [draft, setDraft] = useState("");
  return (
    <Page title="Skills" actions={<Button onClick={() => setDraft(JSON.stringify({ name: "New skill", spec: { purpose: "", trigger_conditions: [], inputs: [], prerequisites: [], permissions: [], steps: ["…"], validation: [], failure_handling: [], output_contract: "" } }, null, 2))}>New skill</Button>}>
      <p className="mb-3 text-sm text-muted">Skills are versioned procedures. Drafts (from a task, a recorded demonstration, or by hand) must be reviewed and approved before they can be selected in the composer.</p>
      {list.loading && <Loading />}
      {list.data?.length === 0 && <Empty>No skills yet. Save one from a succeeded task or a browser demonstration.</Empty>}
      <div className="space-y-3">{list.data?.map((s) => (
        <article key={s.id} className="rounded-xl border border-line bg-panel p-3 text-sm">
          <div className="flex items-center gap-2"><b>{s.name}</b><span className="text-xs text-muted">current v{s.current_version}</span>
            <Button kind="ghost" className="ml-auto" onClick={async () => { if (confirm("Delete skill?")) { await api(`/api/skills/${s.id}`, { method: "DELETE" }); list.reload(); } }}>Delete</Button></div>
          {s.versions.map((v: any) => (
            <details key={v.id} className="mt-2 rounded-lg border border-line p-2">
              <summary className="cursor-pointer">v{v.version} <Status s={v.status === "approved" ? "succeeded" : "queued"} /> {v.status} · from {v.source.kind}</summary>
              <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(v.spec, null, 2)}</pre>
              {v.status === "draft" && <Button kind="primary" onClick={async () => { await api(`/api/skills/${s.id}/versions/${v.version}/approve`, { method: "POST" }).catch(err(toast)); list.reload(); }}>Approve v{v.version}</Button>}
            </details>
          ))}
        </article>
      ))}</div>
      <Dialog open={!!draft} title="New skill (JSON)" onClose={() => setDraft("")}>
        <textarea className={inputCls + " font-mono"} rows={16} value={draft} onChange={(e) => setDraft(e.target.value)} />
        <Button kind="primary" className="mt-2" onClick={async () => { try { await api("/api/skills", { body: JSON.parse(draft) }); setDraft(""); list.reload(); } catch (e: any) { toast(e.message, "err"); } }}>Create draft</Button>
      </Dialog>
    </Page>
  );
}

// ---------------------------------------------------------------- routines
export function Routines() {
  const { user, bots, toast } = useApp();
  const list = useLoad(() => api<any[]>("/api/routines"));
  const [f, setF] = useState({ name: "", bot_id: "", prompt: "", trigger: "schedule", schedule_text: "every weekday at 9am", timezone: user.timezone, missed_policy: "run_once", deliverable: "" });
  const [preview, setPreview] = useState<any>(null);
  const [runs, setRuns] = useState<{ id: string; rows: any[] } | null>(null);
  const [secret, setSecret] = useState<any>(null);
  const set = (k: string, v: string) => { setF({ ...f, [k]: v }); if (k === "schedule_text" || k === "timezone") setPreview(null); };
  const doPreview = async () => setPreview(await api("/api/routines/parse", { body: { text: f.schedule_text, timezone: f.timezone } }).catch(err(toast)));
  const create = async () => {
    try {
      const r = await api<any>("/api/routines", { body: { ...f, bot_id: f.bot_id || bots[0]?.id, deliverable: f.deliverable || null, confirmed: !!preview } });
      if (r.webhook_secret_shown_once) setSecret(r);
      setPreview(null); list.reload(); toast("Routine created");
    } catch (e: any) { toast(e.message, "err"); }
  };
  return (
    <Page title="Routines">
      <p className="mb-3 text-sm text-muted">Routines run in the background worker, also when this page is closed — but only while this computer is awake and LocalCrew is running.</p>
      <div className="mb-6 grid gap-3 rounded-xl border border-line bg-panel p-4 md:grid-cols-2">
        <Field label="Name"><input className={inputCls} value={f.name} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Bot"><select className={inputCls} value={f.bot_id} onChange={(e) => set("bot_id", e.target.value)}>{bots.map((b) => <option key={b.id} value={b.id}>{b.avatar} {b.name}</option>)}</select></Field>
        <div className="md:col-span-2"><Field label="What should the bot do?"><textarea className={inputCls} rows={2} value={f.prompt} onChange={(e) => set("prompt", e.target.value)} /></Field></div>
        <Field label="Trigger"><select className={inputCls} value={f.trigger} onChange={(e) => set("trigger", e.target.value)}><option value="schedule">Schedule</option><option value="webhook">Webhook</option></select></Field>
        {f.trigger === "schedule" && <>
          <Field label="When" hint="e.g. every weekday at 9am · every 15 minutes · every monday at 18:30 · cron"><input className={inputCls} value={f.schedule_text} onChange={(e) => set("schedule_text", e.target.value)} /></Field>
          <Field label="Timezone (IANA)"><input className={inputCls} value={f.timezone} onChange={(e) => set("timezone", e.target.value)} /></Field>
          <Field label="If runs were missed (computer asleep)"><select className={inputCls} value={f.missed_policy} onChange={(e) => set("missed_policy", e.target.value)}><option value="run_once">Run once to catch up</option><option value="skip">Skip</option></select></Field>
        </>}
        <Field label="Deliverable file (optional)"><input className={inputCls} placeholder="digest.md" value={f.deliverable} onChange={(e) => set("deliverable", e.target.value)} /></Field>
        <div className="md:col-span-2 flex flex-wrap items-center gap-2">
          {f.trigger === "schedule" && <Button onClick={doPreview}>Preview schedule</Button>}
          {preview && <span className="text-sm">cron <code>{preview.cron}</code> · next: {preview.next_runs_local.join(" · ")}</span>}
          <Button kind="primary" disabled={!f.name || !f.prompt || (f.trigger === "schedule" && !preview)} onClick={create}>{f.trigger === "schedule" ? "Confirm & create" : "Create"}</Button>
        </div>
      </div>
      {list.data?.length === 0 && <Empty>No routines.</Empty>}
      <ul className="space-y-2">{list.data?.map((r) => (
        <li key={r.id} className="rounded-lg border border-line bg-panel p-3 text-sm">
          <div className="flex flex-wrap items-center gap-2"><b>{r.name}</b><span className="text-muted">{r.trigger === "schedule" ? `${r.schedule_text} (${r.timezone})` : `webhook ${r.webhook_url}`}</span>
            {!r.enabled && <Status s="cancelled" />}<span className="ml-auto text-xs text-muted">{r.next_run_at && `next ${fmtTime(r.next_run_at, user.timezone)}`}</span></div>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button onClick={async () => { const x = await api<any>(`/api/routines/${r.id}/run-now`, { method: "POST" }).catch(err(toast)); if (x) toast(`Run ${x.status}`); }}>Run now</Button>
            <Button onClick={async () => { await api(`/api/routines/${r.id}`, { method: "PATCH", body: { enabled: !r.enabled } }); list.reload(); }}>{r.enabled ? "Pause" : "Enable"}</Button>
            <Button onClick={async () => setRuns({ id: r.id, rows: await api(`/api/routines/${r.id}/runs`) })}>History</Button>
            <Button kind="danger" onClick={async () => { if (confirm("Delete routine?")) { await api(`/api/routines/${r.id}`, { method: "DELETE" }); list.reload(); } }}>Delete</Button>
          </div>
        </li>))}</ul>
      <Dialog open={!!runs} title="Run history" onClose={() => setRuns(null)}>
        {runs?.rows.length === 0 ? <Empty>No runs yet.</Empty> : <ul className="space-y-1 text-sm">{runs?.rows.map((x) => <li key={x.id}><Status s={x.status === "started" ? "running" : x.status.startsWith("skipped") ? "cancelled" : x.status} /> {x.trigger} · {fmtTime(x.created_at, user.timezone)} {x.status.startsWith("skipped") && `(${x.status})`}</li>)}</ul>}
      </Dialog>
      <Dialog open={!!secret} title="Webhook secret (shown once)" onClose={() => setSecret(null)}>
        <p className="text-sm">POST to <code>{secret?.webhook_url}</code> with headers <code>X-LocalCrew-Timestamp</code> (unix seconds), <code>X-LocalCrew-Delivery</code> (unique id) and <code>X-LocalCrew-Signature</code> = hex HMAC-SHA256(secret, "timestamp.body").</p>
        <pre className="mt-2 break-all rounded bg-hover p-2 text-xs">{secret?.webhook_secret_shown_once}</pre>
      </Dialog>
    </Page>
  );
}

// ---------------------------------------------------------------- connections
export function Connections() {
  const { toast } = useApp();
  const cat = useLoad(() => api<any>("/api/connectors"));
  const list = useLoad(() => api<any[]>("/api/connections"));
  const [f, setF] = useState({ connector: "fixture_tracker", name: "Fixture tracker", base_url: "http://127.0.0.1:8899", secret: "" });
  return (
    <Page title="Connections">
      <McpServers />
      <h2 className="mb-2 font-medium">Other connectors</h2>
      <div className="mb-6 grid gap-3 rounded-xl border border-line bg-panel p-4 md:grid-cols-2">
        <Field label="Connector"><select className={inputCls} value={f.connector} onChange={(e) => setF({ ...f, connector: e.target.value })}>{cat.data?.available.filter((c: any) => c.key !== "mcp").map((c: any) => <option key={c.key} value={c.key}>{c.title}</option>)}</select></Field>
        <Field label="Name"><input className={inputCls} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        {f.connector === "fixture_tracker" && <>
          <Field label="Base URL"><input className={inputCls} value={f.base_url} onChange={(e) => setF({ ...f, base_url: e.target.value })} /></Field>
          <Field label="API token" hint="Encrypted at rest; never shown to bots."><input type="password" autoComplete="off" className={inputCls} value={f.secret} onChange={(e) => setF({ ...f, secret: e.target.value })} /></Field>
        </>}
        <div><Button kind="primary" onClick={async () => { await api("/api/connections", { body: { connector: f.connector, name: f.name, config: f.connector === "fixture_tracker" ? { base_url: f.base_url } : {}, secret: f.secret || null } }).catch(err(toast)); setF({ ...f, secret: "" }); list.reload(); }}>Connect</Button></div>
      </div>
      <ul className="mb-6 space-y-2">{list.data?.filter((c) => c.connector !== "mcp").map((c) => (
        <li key={c.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel p-3 text-sm">
          <b>{c.name}</b><span className="text-muted">{c.connector}</span><Status s={c.status === "active" ? "succeeded" : "cancelled"} /><span className="text-xs text-muted">scopes: {c.scopes.join(", ")}</span>
          <Button className="ml-auto" onClick={async () => { const h = await api<any>(`/api/connections/${c.id}/health`).catch(err(toast)); if (h) toast(h.ok ? "Healthy" : `Unhealthy: ${h.error ?? h.status}`, h.ok ? "ok" : "err"); }}>Check health</Button>
          {c.status === "active" && <Button kind="danger" onClick={async () => { await api(`/api/connections/${c.id}/revoke`, { method: "POST" }); list.reload(); }}>Revoke</Button>}
        </li>))}</ul>
      <h2 className="mb-2 font-medium">Available connectors</h2>
      <div className="grid gap-2 md:grid-cols-2">
        {cat.data?.available.map((c: any) => <div key={c.key} className="rounded-lg border border-line bg-panel p-3 text-sm"><b>{c.title}</b><p className="text-xs text-muted">{c.requirements}</p><ul className="mt-1 text-xs">{c.capabilities.map((x: any) => <li key={x.name}><code>{x.name}</code> · {x.effect}</li>)}</ul></div>)}
        {cat.data?.optional.map((c: any) => <div key={c.key} className="rounded-lg border border-dashed border-line p-3 text-sm text-muted"><b>{c.key}</b> — {c.status}<p className="text-xs">{c.requirements}</p></div>)}
      </div>
    </Page>
  );
}

// ---------------------------------------------------------------- memory
export function Memory() {
  const { bots, toast } = useApp();
  const [q, setQ] = useState("");
  const [botId, setBotId] = useState("");
  const list = useLoad(() => api<any[]>(`/api/memories?${new URLSearchParams({ ...(q && { q }), ...(botId && { bot_id: botId }) })}`), [q, botId]);
  const [nw, setNw] = useState({ content: "", scope: "user", bot_id: "" });
  const [edit, setEdit] = useState<any>(null);
  return (
    <Page title="Memory">
      <p className="mb-3 text-sm text-muted">Bots retrieve these by keyword. Bot-scoped memories stay private to that bot; "all my bots" memories are shared across your bots. Credentials are rejected.</p>
      <div className="mb-4 flex flex-wrap gap-2">
        <input className={inputCls + " max-w-xs"} aria-label="Search memory" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className={inputCls + " w-auto"} aria-label="Filter by bot" value={botId} onChange={(e) => setBotId(e.target.value)}><option value="">All</option>{bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}</select>
      </div>
      <form className="mb-4 flex flex-wrap gap-2" onSubmit={async (e) => { e.preventDefault(); await api("/api/memories", { body: { content: nw.content, scope: nw.scope, bot_id: nw.bot_id || null } }).catch(err(toast)); setNw({ ...nw, content: "" }); list.reload(); }}>
        <input className={inputCls + " flex-1"} aria-label="New memory" placeholder="Add a fact…" value={nw.content} onChange={(e) => setNw({ ...nw, content: e.target.value })} />
        <select className={inputCls + " w-auto"} aria-label="Scope" value={nw.scope} onChange={(e) => setNw({ ...nw, scope: e.target.value })}><option value="user">All my bots</option><option value="bot">One bot</option><option value="private_note">Private note (never given to bots)</option></select>
        {nw.scope === "bot" && <select className={inputCls + " w-auto"} aria-label="Bot" value={nw.bot_id} onChange={(e) => setNw({ ...nw, bot_id: e.target.value })}><option value="">Choose…</option>{bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}</select>}
        <Button kind="primary" type="submit" disabled={!nw.content}>Add</Button>
      </form>
      {list.data?.length === 0 && <Empty>No memories.</Empty>}
      <ul className="space-y-2">{list.data?.map((m) => (
        <li key={m.id} className={cx("rounded-lg border border-line bg-panel p-3 text-sm", !m.enabled && "opacity-60")}>
          <p>{m.content}</p>
          <p className="mt-1 text-xs text-muted">{m.scope}{m.bot_id && ` · ${bots.find((b) => b.id === m.bot_id)?.name ?? "bot"}`} · source {m.source.kind} · confidence {m.confidence} · rev {m.revision}{m.expires_at && ` · expires ${m.expires_at}`}</p>
          <div className="mt-2 flex gap-2">
            <Button onClick={() => setEdit(m)}>Correct</Button>
            <Button onClick={async () => { await api(`/api/memories/${m.id}`, { method: "PATCH", body: { enabled: !m.enabled } }); list.reload(); }}>{m.enabled ? "Disable" : "Enable"}</Button>
            <Button kind="danger" onClick={async () => { await api(`/api/memories/${m.id}`, { method: "DELETE" }); list.reload(); }}>Delete</Button>
          </div>
          {m.history.length > 0 && <details className="mt-1 text-xs"><summary className="cursor-pointer text-muted">History</summary>{m.history.map((h: any) => <p key={h.revision}>r{h.revision}: {h.content}</p>)}</details>}
        </li>))}</ul>
      <Dialog open={!!edit} title="Correct memory" onClose={() => setEdit(null)}>
        <textarea className={inputCls} rows={4} value={edit?.content ?? ""} onChange={(e) => setEdit({ ...edit, content: e.target.value })} />
        <Button kind="primary" className="mt-2" onClick={async () => { await api(`/api/memories/${edit.id}`, { method: "PATCH", body: { content: edit.content } }).catch(err(toast)); setEdit(null); list.reload(); }}>Save revision</Button>
      </Dialog>
    </Page>
  );
}

// ---------------------------------------------------------------- settings
export function Settings() {
  const { user, toast } = useApp();
  const [tz, setTz] = useState(user.timezone);
  const [name, setName] = useState(user.display_name);
  const rules = useLoad(() => api<any[]>("/api/policy-rules"));
  const models = useLoad(() => api<any>("/api/models"));
  const zones = (Intl as any).supportedValuesOf?.("timeZone") as string[] | undefined;
  return (
    <Page title="Settings">
      <section className="mb-6 grid gap-3 rounded-xl border border-line bg-panel p-4 md:grid-cols-2">
        <Field label="Display name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Timezone" hint="Times are stored in UTC and shown in this zone.">
          {zones ? <select className={inputCls} value={tz} onChange={(e) => setTz(e.target.value)}>{zones.map((z) => <option key={z}>{z}</option>)}</select> : <input className={inputCls} value={tz} onChange={(e) => setTz(e.target.value)} />}
        </Field>
        <div><Button kind="primary" onClick={async () => { await api("/api/me", { method: "PATCH", body: { timezone: tz, display_name: name } }).catch(err(toast)); toast("Saved — reload to apply everywhere"); }}>Save</Button></div>
      </section>
      <section className="mb-6 rounded-xl border border-line bg-panel p-4 text-sm">
        <h2 className="mb-2 font-medium">Models & privacy</h2>
        <p>Local models run through Ollama. Default model: <code>{models.data?.default}</code>.</p>
        <p className={models.data?.remote_providers?.enabled ? "mt-1 rounded bg-amber-50 p-2 text-amber-900 dark:bg-amber-950 dark:text-amber-100" : "text-muted"}>
          Model API ({models.data?.remote_providers?.kind}): {models.data?.remote_providers?.enabled ? `enabled (${models.data.remote_providers.host})` : "disabled"} · {models.data?.remote_providers?.data_leaving_machine}
        </p>
        <ul className="mt-2 list-disc pl-5">{models.data?.models?.map((m: any) => <li key={m.name}><code>{m.name}</code> — tools {m.tools ? "✓" : "✗"}, vision {m.vision ? "✓" : "✗"}, context {m.context_length}</li>)}</ul>
      </section>
      <section className="rounded-xl border border-line bg-panel p-4 text-sm">
        <h2 className="mb-2 font-medium">Policy rules</h2>
        <p className="mb-2 text-muted">Deny always wins. Organization rules cannot be relaxed by personal rules or bots.</p>
        {rules.data?.length === 0 && <Empty>No custom rules; bot policy profiles apply.</Empty>}
        <ul className="space-y-1">{rules.data?.map((r) => <li key={r.id} className="flex items-center gap-2"><code>{r.level}</code> {r.decision} <code>{r.tool}</code> → <code>{r.target}</code>{r.bot_id && " (one bot)"}<Button kind="ghost" className="ml-auto" onClick={async () => { await api(`/api/policy-rules/${r.id}`, { method: "DELETE" }).catch(err(toast)); rules.reload(); }}>Remove</Button></li>)}</ul>
      </section>
    </Page>
  );
}

// ---------------------------------------------------------------- diagnostics
export function Diagnostics() {
  const { user } = useApp();
  const d = useLoad(() => api<any>("/api/diagnostics"));
  const audit = useLoad(() => api<any[]>("/api/audit?limit=50"));
  const row = (label: string, ok: boolean, extra: string) => <li className="flex gap-2"><span>{ok ? "✅" : "❌"}</span><b>{label}</b><span className="text-muted">{extra}</span></li>;
  return (
    <Page title="Diagnostics" actions={<Button onClick={() => { d.reload(); audit.reload(); }}>Refresh</Button>}>
      {d.error && <ErrorBox error={d.error} />}
      {d.data && (
        <ul className="mb-6 space-y-1 rounded-xl border border-line bg-panel p-4 text-sm">
          {row("Database", d.data.database.ok, "PostgreSQL")}
          {row("Ollama", d.data.ollama.ok, d.data.ollama.ok ? d.data.ollama.models.join(", ") : d.data.ollama.error)}
          {row("Computer service", !!d.data.computer.ok, d.data.computer.ok ? `${d.data.computer.browsers_open} browser(s) open` : d.data.computer.error)}
          {row("Worker", d.data.workers.length > 0, d.data.workers.length ? d.data.workers.map((w: any) => w.id).join(", ") : "no worker heartbeat — tasks and routines will not run")}
          <li className="pt-2 text-muted">{d.data.host_note}</li>
          <li className="text-muted">Tasks: {Object.entries(d.data.tasks_by_status).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"}</li>
        </ul>
      )}
      <h2 className="mb-2 font-medium">Audit log</h2>
      <ul className="divide-y divide-line rounded-lg border border-line bg-panel text-xs">{audit.data?.map((a) => <li key={a.id} className="p-2"><span className="text-muted">{fmtTime(a.at, user.timezone)}</span> <b>{a.action}</b> {a.object_type} {a.object_id.slice(0, 8)}</li>)}</ul>
    </Page>
  );
}
