import { useState } from "react";
import { api } from "./api";
import { Button, Empty, Field, inputCls, useApp, useLoad } from "./ui";

type Conn = { id: string; connector: string; name: string; status: string; has_secret: boolean; config: any };
type ToolInfo = { name: string; description: string; effect: string; default: boolean; mcp_server?: string; mcp_connection_id?: string };

const EFFECT_HINT: Record<string, string> = {
  read: "runs without approval",
  external_write: "needs approval by default",
  destructive: "needs approval by default",
};

// Add, inspect, re-discover and revoke any number of MCP servers.
export function McpServers() {
  const { toast } = useApp();
  const list = useLoad(() => api<Conn[]>("/api/connections"));
  const blank = { name: "", transport: "stdio", command: "", args: "", url: "", env: "", secret: "", secretKind: "env", secretName: "" };
  const [f, setF] = useState(blank);
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: string) => setF({ ...f, [k]: v });
  const servers = (list.data ?? []).filter((c) => c.connector === "mcp" && c.status !== "revoked");

  const add = async () => {
    const env = Object.fromEntries(f.env.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const i = l.indexOf("=");
      return [l.slice(0, i).trim(), l.slice(i + 1).trim()];
    }));
    const secret_as = f.secretKind === "env" ? { kind: "env", name: f.secretName || "API_TOKEN" }
      : { kind: "header", name: f.secretName || "Authorization", prefix: f.secretName ? "" : "Bearer " };
    setBusy(true);
    try {
      const c = await api<Conn>("/api/connections", {
        body: {
          connector: "mcp", name: f.name, secret: f.secret || null,
          config: { transport: f.transport, command: f.command, args: f.args.split("\n").map((a) => a.trim()).filter(Boolean), url: f.url, env, secret_as },
        },
      });
      toast(c.status === "active" ? `Connected ${c.name}: ${c.config.tools.length} tools` : `Added, but the server failed: ${c.config.last_error}`, c.status === "active" ? "ok" : "err");
      setF(blank);
      list.reload();
    } catch (e: any) { toast(e.message, "err"); }
    setBusy(false);
  };

  return (
    <section className="mb-8">
      <h2 className="mb-1 font-medium">MCP servers</h2>
      <p className="mb-3 text-sm text-muted">Connect any number of Model Context Protocol servers. Their tools become available to bots you enable them for,
        under the same policy and approvals as built-in tools (read-only tools run directly; others need approval unless you add a rule).
        Stdio servers run inside the computer service with sandbox limits; secrets are injected as an env var or header and never shown to models.</p>
      <div className="mb-4 grid gap-3 rounded-xl border border-line bg-panel p-4 md:grid-cols-2">
        <Field label="Name" hint="Used in tool names: mcp__<name>__<tool>"><input className={inputCls} value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="github" /></Field>
        <Field label="Transport"><select className={inputCls} value={f.transport} onChange={(e) => set("transport", e.target.value)}><option value="stdio">stdio (command)</option><option value="http">Streamable HTTP (URL)</option></select></Field>
        {f.transport === "stdio" ? <>
          <Field label="Command" hint="e.g. npx, uvx, python"><input className={inputCls} value={f.command} onChange={(e) => set("command", e.target.value)} placeholder="npx" /></Field>
          <Field label="Arguments" hint="one per line (paths with spaces are fine)"><textarea className={inputCls} rows={2} value={f.args} onChange={(e) => set("args", e.target.value)} placeholder={"-y\n@modelcontextprotocol/server-github"} /></Field>
          <div className="md:col-span-2"><Field label="Environment (non-secret)" hint="one KEY=value per line"><textarea className={inputCls} rows={2} value={f.env} onChange={(e) => set("env", e.target.value)} /></Field></div>
        </> : <div className="md:col-span-2"><Field label="URL" hint="https (plain http only for localhost)"><input className={inputCls} value={f.url} onChange={(e) => set("url", e.target.value)} placeholder="https://example.com/mcp" /></Field></div>}
        <Field label="Secret (optional)" hint="Encrypted at rest"><input type="password" autoComplete="off" className={inputCls} value={f.secret} onChange={(e) => set("secret", e.target.value)} /></Field>
        <Field label="Inject secret as">
          <div className="flex gap-2">
            <select className={inputCls + " w-32"} value={f.secretKind} onChange={(e) => set("secretKind", e.target.value)}><option value="env">env var</option><option value="header">HTTP header</option></select>
            <input className={inputCls} value={f.secretName} onChange={(e) => set("secretName", e.target.value)} placeholder={f.secretKind === "env" ? "GITHUB_TOKEN" : "Authorization (Bearer)"} />
          </div>
        </Field>
        <div><Button kind="primary" disabled={busy || !f.name || (f.transport === "stdio" ? !f.command : !f.url)} onClick={add}>{busy ? "Connecting…" : "Connect & discover tools"}</Button></div>
      </div>
      {servers.length === 0 ? <Empty>No MCP servers yet.</Empty> : (
        <ul className="space-y-2">{servers.map((c) => (
          <li key={c.id} className="rounded-lg border border-line bg-panel p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <b>{c.name}</b><span className="text-xs text-muted">{c.config.transport === "stdio" ? `${c.config.command} ${(c.config.args || []).join(" ")}` : c.config.url}</span>
              <span className={c.status === "active" ? "rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100" : "rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-900 dark:bg-red-900 dark:text-red-100"}>{c.status === "active" ? "connected" : "error"}</span>
              <span className="text-xs text-muted">{(c.config.tools || []).length} tools{c.has_secret ? " · secret set" : ""}</span>
              <div className="ml-auto flex gap-2">
                <Button onClick={async () => { await api(`/api/connections/${c.id}/discover`, { method: "POST" }).catch((e) => toast(e.message, "err")); list.reload(); }}>Re-discover</Button>
                <Button kind="danger" onClick={async () => { await api(`/api/connections/${c.id}/revoke`, { method: "POST" }); list.reload(); }}>Remove</Button>
              </div>
            </div>
            {c.config.last_error && <p className="mt-1 text-xs text-red-700 dark:text-red-300">{c.config.last_error}</p>}
            {(c.config.tools || []).length > 0 && (
              <details className="mt-2"><summary className="cursor-pointer text-xs text-muted">Tools</summary>
                <ul className="mt-1 space-y-0.5 text-xs">{c.config.tools.map((t: any) => (
                  <li key={t.name}><code>{t.exposed_name}</code> · {t.read_only ? "read" : t.destructive ? "destructive" : "external write"} · {t.description}</li>
                ))}</ul>
              </details>
            )}
          </li>))}</ul>
      )}
    </section>
  );
}

// Tool selection for a bot: built-in tools plus MCP tools grouped by server, with "all tools of this server".
export function ToolPicker({ tools, value, onChange }: { tools: ToolInfo[]; value: string[]; onChange: (v: string[]) => void }) {
  const builtin = tools.filter((t) => !t.mcp_server);
  const servers = new Map<string, { id: string; tools: ToolInfo[] }>();
  for (const t of tools.filter((x) => x.mcp_server)) {
    const s = servers.get(t.mcp_server!) ?? { id: t.mcp_connection_id!, tools: [] };
    s.tools.push(t);
    servers.set(t.mcp_server!, s);
  }
  const toggle = (name: string, on: boolean) => onChange(on ? [...value, name] : value.filter((x) => x !== name));
  return (
    <fieldset className="mt-4"><legend className="mb-2 text-sm font-medium">Tools</legend>
      <div className="grid gap-1 sm:grid-cols-2">
        {builtin.map((t) => (
          <label key={t.name} className="flex items-start gap-2 text-sm">
            <input type="checkbox" checked={value.includes(t.name)} onChange={(e) => toggle(t.name, e.target.checked)} />
            <span><code>{t.name}</code> <span className="text-xs text-muted">({t.effect}) {t.description}</span></span>
          </label>
        ))}
      </div>
      {[...servers.entries()].map(([name, s]) => {
        const all = value.includes(`mcp:${s.id}`);
        return (
          <div key={name} className="mt-3 rounded-lg border border-line p-2">
            <label className="flex items-center gap-2 text-sm font-medium">
              <input type="checkbox" checked={all} onChange={(e) => toggle(`mcp:${s.id}`, e.target.checked)} />
              MCP server <code>{name}</code>: all {s.tools.length} tools (including tools it adds later)
            </label>
            {!all && <div className="mt-1 grid gap-1 pl-6 sm:grid-cols-2">{s.tools.map((t) => (
              <label key={t.name} className="flex items-start gap-2 text-sm">
                <input type="checkbox" checked={value.includes(t.name)} onChange={(e) => toggle(t.name, e.target.checked)} />
                <span><code>{t.name.split("__").pop()}</code> <span className="text-xs text-muted">({t.effect}: {EFFECT_HINT[t.effect]})</span></span>
              </label>))}</div>}
          </div>
        );
      })}
    </fieldset>
  );
}
