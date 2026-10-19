# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **"Report a vulnerability"** (Security → Advisories) on this
repository. Do not open a public issue. Include steps to reproduce, the affected version or commit, and impact. You
should get an acknowledgement within 7 days.

## Supported versions

Only the latest release on `main` receives fixes (pre-1.0 project).

## Security model (summary)

LocalCrew runs AI agents that can browse, run commands and call external tools, so the design assumes the model can
be manipulated (prompt injection) and enforces safety outside it:

- Every effectful action passes a policy engine (deny wins; organization rules cannot be relaxed). External writes,
  shell commands and destructive or unannotated MCP tools require human approval by default. Approvals are bound to
  a digest of the exact action and cannot be replayed.
- Secrets are encrypted with a key kept outside the database, injected by a broker, and never placed in prompts.
- Web pages, files, connector and MCP results are treated as untrusted data and redacted.
- Outbound requests are restricted to globally routable addresses with DNS pinning (server side and browser).
- Terminal commands and stdio MCP servers run in a separate service with resource limits.

## Known limitations

- **Single-user / trusted-team deployment only.** Running natively, approved terminal commands and stdio MCP servers
  run with the privileges of the account running LocalCrew. Strong isolation needs the container deployment, and
  untrusted multi-user use needs per-user VMs. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- MCP servers you connect are code you choose to run. Review them as you would any dependency.
- Services bind to localhost. Remote access requires a TLS reverse proxy or VPN (see `docs/OPERATIONS.md`).
