import { useCallback, useEffect, useMemo, useState } from "react";
import { api, setCsrf, subscribe, type Bot, type Conversation, type User } from "./api";
import { Conversation as Chat } from "./Chat";
import { Approvals, BotEditor, Computer, Connections, Diagnostics, Files, Groups, Memory, Routines, Settings, Skills, Tasks } from "./Pages";
import { AppCtx, Button, ErrorBox, EventBus, Field, Loading, cx, inputCls, useLoad } from "./ui";

const AREAS: [string, string][] = [
  ["#/tasks", "Tasks"], ["#/groups", "Groups"], ["#/approvals", "Approvals"], ["#/computer", "Computer"], ["#/files", "Files"],
  ["#/skills", "Skills"], ["#/routines", "Routines"], ["#/connections", "Connections"], ["#/memory", "Memory"],
  ["#/settings", "Settings"], ["#/diagnostics", "Diagnostics"],
];

function useHash() {
  const [h, setH] = useState(location.hash || "#/");
  useEffect(() => { const f = () => setH(location.hash || "#/"); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h;
}

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [needsSetup, setNeedsSetup] = useState(false);
  useEffect(() => {
    api<any>("/api/me").then((r) => { setCsrf(r.csrf); setUser(r.user); })
      .catch(async () => { setUser(null); setNeedsSetup((await api<any>("/api/setup")).needs_setup); });
  }, []);
  if (user === undefined) return <Loading />;
  if (!user) return <Auth setup={needsSetup} onDone={(u, c) => { setCsrf(c); setUser(u); }} />;
  return <Shell user={user} onLogout={() => setUser(null)} />;
}

function Auth({ setup, onDone }: { setup: boolean; onDone: (u: User, csrf: string) => void }) {
  const [f, setF] = useState({ email: "", password: "", display_name: "", timezone: "Asia/Karachi" });
  const [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const r = await api<any>(setup ? "/api/setup" : "/api/auth/login", { body: setup ? f : { email: f.email, password: f.password } });
      onDone(r.user, r.csrf);
    } catch (err: any) { setError(err.message); }
  };
  return (
    <main className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-3 rounded-2xl border border-line bg-panel p-6">
        <h1 className="text-2xl font-semibold">LocalCrew</h1>
        <p className="text-sm text-muted">{setup ? "Create the first account on this machine. Everything stays local." : "Sign in to your AI teammates."}</p>
        {error && <ErrorBox error={error} />}
        {setup && <Field label="Your name"><input className={inputCls} required value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} /></Field>}
        <Field label="Email"><input className={inputCls} type="email" autoComplete="username" required value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
        <Field label="Password" hint={setup ? "At least 10 characters." : undefined}><input className={inputCls} type="password" autoComplete={setup ? "new-password" : "current-password"} required minLength={setup ? 10 : 1} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
        {setup && <Field label="Timezone"><input className={inputCls} value={f.timezone} onChange={(e) => setF({ ...f, timezone: e.target.value })} /></Field>}
        <Button kind="primary" type="submit" className="w-full">{setup ? "Create account" : "Sign in"}</Button>
      </form>
    </main>
  );
}

function Shell({ user, onLogout }: { user: User; onLogout: () => void }) {
  const hash = useHash();
  const [bots, setBots] = useState<Bot[]>([]);
  const [toasts, setToasts] = useState<{ id: number; msg: string; kind: string }[]>([]);
  const [navOpen, setNavOpen] = useState(false);
  const [pending, setPending] = useState(0);
  const events = useMemo(() => new EventBus(), []);
  const groups = useLoad(() => api<Conversation[]>("/api/conversations?kind=group"), [hash.startsWith("#/group")]);
  const refreshBots = useCallback(async () => setBots(await api<Bot[]>("/api/bots")), []);
  const toast = useCallback((msg: string, kind: "ok" | "err" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);
  const go = useCallback((h: string) => { location.hash = h; setNavOpen(false); }, []);
  const loadPending = () => api<any[]>("/api/approvals").then((a) => setPending(a.length)).catch(() => {});
  useEffect(() => { refreshBots(); loadPending(); }, []);
  useEffect(() => subscribe((e) => {
    events.emit(e);
    if (e.type === "approval_requested" || (e.type === "status" && e.data.approval)) loadPending();
    if (e.type === "notification") toast(e.data.kind === "routine_failed" ? `Routine failed: ${e.data.error}` : "Notification", "err");
  }, -1), []);

  const [, route, id, sub] = hash.split("/");
  let main: React.ReactNode;
  if (route === "bot" && id && sub === "edit") main = <BotEditor id={id} />;
  else if (route === "bot" && id) main = <BotChat id={id} bots={bots} />;
  else if (route === "group" && id) main = <GroupChat id={id} bots={bots} />;
  else if (route === "new-bot") main = <BotEditor />;
  else if (route === "tasks" || route === "task") main = <Tasks id={route === "task" ? id : undefined} />;
  else if (route === "groups") main = <Groups />;
  else if (route === "approvals") main = <Approvals />;
  else if (route === "computer") main = <Computer />;
  else if (route === "files") main = <Files />;
  else if (route === "skills") main = <Skills />;
  else if (route === "routines") main = <Routines />;
  else if (route === "connections") main = <Connections />;
  else if (route === "memory") main = <Memory />;
  else if (route === "settings") main = <Settings />;
  else if (route === "diagnostics") main = <Diagnostics />;
  else main = <Home bots={bots} />;

  return (
    <AppCtx.Provider value={{ user, bots, refreshBots, toast, events, go }}>
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:right-2 focus:top-2 focus:z-50 focus:rounded focus:bg-panel focus:p-2">Skip to content</a>
      <div className="flex h-full">
        <nav aria-label="Primary" className={cx("fixed inset-y-0 left-0 z-40 w-72 shrink-0 overflow-y-auto border-r border-line bg-panel p-3 transition-transform md:static md:translate-x-0", navOpen ? "translate-x-0" : "-translate-x-full")}>
          <div className="mb-3 flex items-center justify-between">
            <button className="text-lg font-semibold" onClick={() => go("#/")}>LocalCrew</button>
            <button className="md:hidden" aria-label="Close menu" onClick={() => setNavOpen(false)}>×</button>
          </div>
          <div className="mb-1 flex items-center justify-between text-xs font-medium uppercase text-muted"><span>Bots</span><button aria-label="New bot" className="rounded px-1 text-base" onClick={() => go("#/new-bot")}>＋</button></div>
          <ul className="mb-3 space-y-0.5">
            {bots.map((b) => (
              <li key={b.id} className="group flex items-center">
                <a href={`#/bot/${b.id}`} onClick={() => setNavOpen(false)} className={cx("flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-hover", hash.startsWith(`#/bot/${b.id}`) && "bg-hover font-medium")}>
                  <span aria-hidden>{b.avatar}</span><span className="truncate">{b.name}</span>{b.pinned && <span className="text-xs text-muted" aria-label="pinned">★</span>}
                </a>
                <a href={`#/bot/${b.id}/edit`} className="rounded px-1 text-xs text-muted opacity-0 group-hover:opacity-100 focus:opacity-100" aria-label={`Edit ${b.name}`}>edit</a>
              </li>
            ))}
          </ul>
          {!!groups.data?.length && <>
            <div className="mb-1 text-xs font-medium uppercase text-muted">Groups</div>
            <ul className="mb-3 space-y-0.5">{groups.data.map((g) => <li key={g.id}><a href={`#/group/${g.id}`} onClick={() => setNavOpen(false)} className={cx("block truncate rounded-lg px-2 py-1.5 text-sm hover:bg-hover", hash === `#/group/${g.id}` && "bg-hover font-medium")}>👥 {g.title}</a></li>)}</ul>
          </>}
          <ul className="space-y-0.5 border-t border-line pt-2">
            {AREAS.map(([h, label]) => (
              <li key={h}><a href={h} onClick={() => setNavOpen(false)} className={cx("flex items-center justify-between rounded-lg px-2 py-1.5 text-sm hover:bg-hover", hash.startsWith(h) && "bg-hover font-medium")}>
                {label}{label === "Approvals" && pending > 0 && <span className="rounded-full bg-violet-700 px-1.5 text-xs text-white">{pending}</span>}
              </a></li>
            ))}
          </ul>
          <div className="mt-4 border-t border-line pt-2 text-xs text-muted">
            {user.display_name} · {user.timezone}
            <button className="ml-2 underline" onClick={async () => { await api("/api/auth/logout", { method: "POST" }); onLogout(); }}>Sign out</button>
          </div>
        </nav>
        {navOpen && <div className="fixed inset-0 z-30 bg-black/30 md:hidden" onClick={() => setNavOpen(false)} />}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center gap-2 border-b border-line bg-panel px-3 py-2 md:hidden">
            <button aria-label="Open menu" className="rounded-lg border border-line px-2 py-1" onClick={() => setNavOpen(true)}>☰</button>
            <span className="font-semibold">LocalCrew</span>
            {pending > 0 && <a href="#/approvals" className="ml-auto rounded-full bg-violet-700 px-2 text-xs text-white">{pending} approval{pending > 1 ? "s" : ""}</a>}
          </header>
          <main id="main" className="min-h-0 flex-1 overflow-y-auto">{main}</main>
        </div>
      </div>
      <div className="fixed bottom-3 right-3 z-50 space-y-2" aria-live="assertive">
        {toasts.map((t) => <div key={t.id} role="status" className={cx("rounded-lg px-3 py-2 text-sm text-white shadow", t.kind === "err" ? "bg-red-700" : "bg-slate-800")}>{t.msg}</div>)}
      </div>
    </AppCtx.Provider>
  );
}

function BotChat({ id, bots }: { id: string; bots: Bot[] }) {
  const conv = useLoad(() => api<Conversation>(`/api/bots/${id}/conversation`), [id]);
  const bot = bots.find((b) => b.id === id);
  if (conv.error) return <div className="p-4"><ErrorBox error={conv.error} /></div>;
  if (!conv.data || !bot) return <Loading />;
  return <Chat convId={conv.data.id} bots={[bot]} title={`${bot.avatar} ${bot.name}${bot.role ? " — " + bot.role : ""}`} />;
}

function GroupChat({ id, bots }: { id: string; bots: Bot[] }) {
  const convs = useLoad(() => api<Conversation[]>("/api/conversations?kind=group"), [id]);
  const c = convs.data?.find((x) => x.id === id);
  if (convs.loading) return <Loading />;
  if (!c) return <div className="p-4"><ErrorBox error="Group not found" /></div>;
  return <Chat convId={c.id} bots={bots.filter((b) => c.bot_ids.includes(b.id))} title={`👥 ${c.title}`} />;
}

function Home({ bots }: { bots: Bot[] }) {
  return (
    <div className="mx-auto max-w-3xl p-6">
      <h1 className="mb-2 text-2xl font-semibold">Your crew</h1>
      <p className="mb-4 text-sm text-muted">Persistent AI teammates running on this computer with a local model. Tasks continue when you close this page, but stop while the computer sleeps or is off.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        {bots.map((b) => (
          <a key={b.id} href={`#/bot/${b.id}`} className="rounded-xl border border-line bg-panel p-4 hover:bg-hover">
            <div className="text-2xl" aria-hidden>{b.avatar}</div>
            <div className="font-medium">{b.name}</div>
            <div className="text-sm text-muted">{b.role}</div>
          </a>
        ))}
        <a href="#/new-bot" className="flex items-center justify-center rounded-xl border border-dashed border-line p-4 text-sm text-muted hover:bg-hover">＋ New bot</a>
      </div>
    </div>
  );
}
