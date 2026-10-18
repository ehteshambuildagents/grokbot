import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

const cred = () => JSON.parse(readFileSync(new URL("../../../tests/e2e/.e2e-user.json", import.meta.url), "utf8"));

async function login(page: import("@playwright/test").Page) {
  const { email, password } = cred();
  await page.goto("/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your crew" })).toBeVisible();
}

test("create a bot, chat (MOCK model), receive a downloadable deliverable", async ({ page, isMobile }) => {
  await login(page);
  const name = `Echo${isMobile ? "M" : "D"}${Date.now() % 100000}`;
  await page.goto("/#/new-bot");
  await page.getByLabel("Name", { exact: true }).fill(name);
  await page.getByLabel("Role", { exact: true }).fill("E2E tester");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { name: new RegExp(name) })).toBeVisible();
  // Point the bot at the deterministic MOCK model through the API (not offered in the UI on purpose)
  const botId = page.url().split("/bot/")[1];
  await page.evaluate(async ([id]) => {
    const me = await (await fetch("/api/me")).json();
    await fetch(`/api/bots/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf }, body: JSON.stringify({ model: "scripted:e2e" }) });
  }, [botId]);
  await page.reload();
  await page.getByLabel("Message").fill("make me a note");
  await page.getByLabel("Message").press("Enter");
  const link = page.getByRole("link", { name: "e2e-note.md" }).first();
  await expect(link).toBeVisible({ timeout: 60_000 });
  const [download] = await Promise.all([page.waitForEvent("download"), link.click()]);
  expect(download.suggestedFilename()).toBe("e2e-note.md");
  await expect(page.getByText("Created e2e-note.md for you.")).toBeVisible();
});

test("keyboard navigation and responsive navigation", async ({ page, isMobile }) => {
  await login(page);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  if (isMobile) {
    await page.getByRole("button", { name: "Open menu" }).click();
  }
  await page.getByRole("link", { name: "Diagnostics" }).click();
  await expect(page.getByRole("heading", { name: "Diagnostics" })).toBeVisible();
  await expect(page.getByText("Local execution stops when this computer sleeps or shuts down.")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("rejects stored XSS in messages (rendered as text)", async ({ page }) => {
  await login(page);
  await page.goto("/#/memory");
  await page.getByLabel("New memory").fill(`<img src=x onerror="window.__xss=1"> note`);
  await page.getByRole("button", { name: "Add" }).click();
  await expect(page.getByText(`<img src=x onerror="window.__xss=1"> note`)).toBeVisible();
  expect(await page.evaluate(() => (window as any).__xss)).toBeUndefined();
});
