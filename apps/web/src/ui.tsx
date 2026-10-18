import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Bot, User } from "./api";

export type Ctx = {
  user: User;
  bots: Bot[];
  refreshBots: () => Promise<void>;
  toast: (msg: string, kind?: "ok" | "err") => void;
  events: EventBus;
  go: (hash: string) => void;
};
export const AppCtx = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(AppCtx);

type Listener = (e: { task_id: string; type: string; data: any; at: string }) => void;
export class EventBus {
  private ls = new Set<Listener>();
  on(l: Listener) { this.ls.add(l); return () => { this.ls.delete(l); }; }
  emit(e: Parameters<Listener>[0]) { this.ls.forEach((l) => l(e)); }
}
export function useEvents(l: Listener, deps: unknown[] = []) {
  const { events } = useApp();
  const ref = useRef(l);
  ref.current = l;
  useEffect(() => events.on((e) => ref.current(e)), [events, ...deps]);
}

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

export function Button({ kind = "default", className, ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: "primary" | "default" | "danger" | "ghost" }) {
  return (
    <button
      {...p}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
        kind === "primary" && "bg-teal-700 text-white hover:bg-teal-800",
        kind === "default" && "border border-line bg-panel hover:bg-hover",
        kind === "danger" && "bg-red-700 text-white hover:bg-red-800",
        kind === "ghost" && "hover:bg-hover",
        className,
      )}
    />
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="block text-xs text-muted">{hint}</span>}
    </label>
  );
}

export const inputCls = "w-full rounded-lg border border-line bg-panel px-3 py-2 text-sm";

export function Page({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-5xl p-4 md:p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">{title}</h1>
        <div className="flex flex-wrap gap-2">{actions}</div>
      </div>
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-dashed border-line p-8 text-center text-sm text-muted">{children}</div>;
}

export function Loading() {
  return <div role="status" className="p-6 text-sm text-muted">Loading…</div>;
}

export function ErrorBox({ error, retry }: { error: string; retry?: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
      {error} {retry && <button className="ml-2 underline" onClick={retry}>Retry</button>}
    </div>
  );
}

const STATUS_STYLE: Record<string, string> = {
  queued: "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100",
  planning: "bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-100",
  running: "bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-100",
  retry_wait: "bg-amber-100 text-amber-900 dark:bg-amber-900 dark:text-amber-100",
  waiting_for_user: "bg-amber-100 text-amber-900 dark:bg-amber-900 dark:text-amber-100",
  waiting_for_approval: "bg-violet-100 text-violet-900 dark:bg-violet-900 dark:text-violet-100",
  succeeded: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100",
  failed: "bg-red-100 text-red-900 dark:bg-red-900 dark:text-red-100",
  cancelled: "bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200",
};
export function Status({ s }: { s: string }) {
  return <span className={cx("rounded-full px-2 py-0.5 text-xs font-medium", STATUS_STYLE[s] ?? STATUS_STYLE.queued)}>{s.replace(/_/g, " ")}</span>;
}

// Safe rendering: text nodes only; markdown links to same-origin /api paths become anchors. No HTML injection.
export function RichText({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  const re = /\[([^\]]{1,200})\]\((\/api\/[A-Za-z0-9/_\-.?=&]+)\)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    parts.push(text.slice(last, m.index));
    parts.push(<a key={m.index} href={m[2]} className="text-teal-700 underline dark:text-teal-400" download>{m[1]}</a>);
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return <div className="whitespace-pre-wrap break-words">{parts}</div>;
}

export function Dialog({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} aria-label={title}
      className="m-auto w-[min(100%-2rem,36rem)] rounded-xl border border-line bg-panel p-0 text-fg backdrop:bg-black/40">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <h2 className="font-semibold">{title}</h2>
        <button className="rounded px-2 text-lg" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <div className="max-h-[75vh] overflow-auto p-4">{children}</div>
    </dialog>
  );
}

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const load = async () => {
    setLoading(true);
    try {
      setData(await fn());
      setError("");
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, deps); // eslint-disable-line
  return { data, error, loading, reload: load, setData };
}
