import { defineConfig, devices } from "@playwright/test";
import { resolve } from "node:path";

// Runs against an isolated stack (fresh DB, MOCK scripted model) started by tests/e2e/e2e_server.py.
// Requires: PostgreSQL reachable (see README), `npm run build` done, Python venv at ../../.venv.
const root = resolve(process.cwd(), "../..");  // run from apps/web
const py = resolve(root, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
export default defineConfig({
  testDir: "./e2e",
  retries: 0, // no retries: deterministic defects must fail loudly
  workers: 1,
  use: { baseURL: "http://127.0.0.1:18100", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: { command: `"${py}" "${resolve(root, "tests/e2e/e2e_server.py")}"`, url: "http://127.0.0.1:18100/api/setup", timeout: 120_000, reuseExistingServer: false },
});
