// Thin API client: same-origin cookies + CSRF header on mutations.
let csrf = "";
export const setCsrf = (t: string) => (csrf = t);

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; form?: FormData } = {}): Promise<T> {
  const method = opts.method ?? (opts.body || opts.form ? "POST" : "GET");
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: opts.form ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
  });
  const text = await r.text();
  const data = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : null;
  if (!r.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : d?.message ?? (Array.isArray(d) ? d.map((x: any) => x.msg).join("; ") : JSON.stringify(d ?? data));
    throw new ApiError(r.status, msg || r.statusText);
  }
  return data as T;
}

export type User = { id: string; email: string; display_name: string; timezone: string; is_admin: boolean };
export type Bot = {
  id: string; name: string; avatar: string; role: string; instructions: string; model: string; tools: string[];
  policy_profile: string; memory_enabled: boolean; pinned: boolean; hidden: boolean; version: number; team_id: string | null;
  model_options: Record<string, number>;
};
export type Message = { id: string; content: string; author_user_id: string | null; author_bot_id: string | null; task_id: string | null; created_at: string; attachments: any[] };
export type Task = {
  id: string; bot_id: string; status: string; phase: string; progress: string; blocker: any; error: string; objective: string;
  conversation_id: string | null; created_at: string; finished_at: string | null; tool_calls_used: number; tool_budget: number;
  attempts: number; parent_task_id: string | null; handoff: any;
};
export type Conversation = { id: string; kind: string; title: string; bot_ids: string[] };

export const ACTIVE = ["queued", "planning", "running", "retry_wait"];
export const WAITING = ["waiting_for_user", "waiting_for_approval"];

// Persisted event stream with automatic replay (EventSource resends Last-Event-ID on reconnect).
export function subscribe(onEvent: (e: { task_id: string; type: string; data: any; at: string }) => void, after = 0) {
  const es = new EventSource(`/api/events?after=${after}`, { withCredentials: true });
  const handler = (ev: MessageEvent) => onEvent(JSON.parse(ev.data));
  for (const t of ["status", "step", "progress", "delta", "approval_requested", "handoff", "handoff_completed",
    "notification", "routine", "redirect", "recovered", "cancel_requested", "usage"]) es.addEventListener(t, handler);
  return () => es.close();
}

export function fmtTime(iso: string | null | undefined, tz: string) {
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat(undefined, { timeZone: tz, dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  } catch {
    return iso;
  }
}
