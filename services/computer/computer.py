"""LocalCrew computer service: the only process that drives Chromium profiles and runs terminal commands.

- One persistent Chromium context per user profile directory, opened only by this process.
- A control lease decides who may act on a user's browser: one task at a time, or a human.
  Human takeover revokes automation; automation resumes only after release.
- Password / MFA / CAPTCHA pages pause automation (409 human_required). Bots can never type into password fields.
- Human keystrokes marked sensitive (or aimed at password fields) are never logged or recorded.
- Terminal: argv arrays (shell separately gated), workspace cwd, scrubbed env, timeouts, process-tree kill,
  POSIX rlimits when running on Linux.
It holds no database credentials; callers authenticate with X-Internal-Token.
"""
import asyncio
import base64
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from playwright.async_api import Error as PWError  # noqa: E402
from playwright.async_api import TimeoutError as PWTimeout  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from localcrew import workspace  # noqa: E402
from localcrew.config import settings  # noqa: E402
from localcrew.security import Redactor, check_url  # noqa: E402

LEASE_S = 120
IS_WIN = os.name == "nt"
SENSITIVE_JS = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const pw = [...document.querySelectorAll('input[type=password]')].some(vis);
  const t = (document.body ? document.body.innerText : '').toLowerCase();
  const captcha = !!document.querySelector('iframe[src*="captcha"], .g-recaptcha, .h-captcha, [data-captcha]')
                  || /captcha|i'm not a robot|verify you are human/.test(t);
  const mfa = /verification code|two-factor|2fa code|one-time code|authenticator app/.test(t);
  return {pw, captcha, mfa};
}"""
SNAPSHOT_JS = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const label = e => (e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].innerText)
                      || e.getAttribute('placeholder') || e.innerText || e.value || '').trim().slice(0, 80);
  const els = [...document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=link]')]
    .filter(vis).slice(0, 80).map(e => ({tag: e.tagName.toLowerCase(), role: e.getAttribute('role') ||
      ({a:'link',button:'button',select:'combobox',textarea:'textbox'}[e.tagName.toLowerCase()] ||
       (e.type === 'checkbox' ? 'checkbox' : e.type === 'submit' ? 'button' : 'textbox')),
      name: label(e), type: e.type === 'password' ? 'password' : (e.type || undefined), href: e.getAttribute('href') || undefined}));
  return {title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 8000), elements: els};
}"""
POINT_JS = """([x, y]) => { const e = document.elementFromPoint(x, y); if (!e) return null;
  const t = e.closest('a,button,input,select,textarea,[role]') || e;
  return {role: t.getAttribute('role') || ({a:'link',button:'button',input: t.type === 'submit' ? 'button' : 'textbox',
          textarea:'textbox',select:'combobox'}[t.tagName.toLowerCase()] || ''),
          name: (t.getAttribute('aria-label') || (t.labels && t.labels[0] && t.labels[0].innerText) ||
                 t.getAttribute('placeholder') || t.innerText || '').trim().slice(0, 80),
          password: t.type === 'password', sensitive: t.type === 'password' ||
                 /one-time-code|cc-|current-password|new-password/.test(t.getAttribute('autocomplete') || '')}; }"""


@dataclass
class UserBrowser:
    ctx: object = None
    page: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    holder_kind: str = "none"  # none|task|human
    holder_id: str = ""
    expires: float = 0.0
    recording: list | None = None
    focused_sensitive: bool = False
    console: list = field(default_factory=list)


class State:
    pw = None
    browsers: dict[str, UserBrowser] = {}
    procs: dict[str, set] = {}
    cancelled: set[str] = set()


app = FastAPI(title="LocalCrew computer service", docs_url=None, redoc_url=None)


def auth(x_internal_token: str = Header("")):
    if x_internal_token != settings().computer_token:
        raise HTTPException(401, "bad internal token")


def blocked(code: str, message: str):
    raise HTTPException(409, {"code": code, "message": message})


def profile_dir(user_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", user_id):
        raise HTTPException(400, "bad user id")
    p = settings().data_dir / "browser-profiles" / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


@lru_cache(maxsize=512)
def _host_allowed(scheme_host: str) -> bool:
    try:
        check_url(scheme_host)
        return True
    except ValueError:
        return False


async def browser_for(user_id: str) -> UserBrowser:
    ub = State.browsers.setdefault(user_id, UserBrowser())
    if ub.ctx is None:
        if State.pw is None:
            State.pw = await async_playwright().start()
        ub.ctx = await State.pw.chromium.launch_persistent_context(
            str(profile_dir(user_id)), headless=True, accept_downloads=True, viewport={"width": 1280, "height": 800})

        async def guard(route):
            from urllib.parse import urlsplit
            u = urlsplit(route.request.url)
            if u.scheme in ("data", "blob", "about"):
                return await route.continue_()
            if _host_allowed(f"{u.scheme}://{u.netloc}/"):
                return await route.continue_()
            await route.abort("blockedbyclient")

        await ub.ctx.route("**/*", guard)
        ub.page = ub.ctx.pages[0] if ub.ctx.pages else await ub.ctx.new_page()
        _watch(ub)
    if ub.page.is_closed():
        ub.page = await ub.ctx.new_page()
        _watch(ub)
    return ub


def _watch(ub: UserBrowser):
    """Keep the last console messages as evidence (redacted)."""
    def on_console(msg):
        ub.console = [*ub.console[-49:], {"type": msg.type, "text": Redactor.text(msg.text)[:500], "url": ub.page.url}]
    ub.page.on("console", on_console)


def acquire(ub: UserBrowser, task_id: str):
    now = time.monotonic()
    if task_id in State.cancelled:
        raise HTTPException(499, "cancelled")
    if ub.holder_kind == "human":
        blocked("control_revoked", "A human has taken control of the browser. Waiting until control is released.")
    if ub.holder_kind == "task" and ub.holder_id != task_id and ub.expires > now:
        raise HTTPException(423, f"browser busy: held by another task until released (≤{int(ub.expires - now)}s)")
    ub.holder_kind, ub.holder_id, ub.expires = "task", task_id, now + LEASE_S


async def guard_sensitive(ub: UserBrowser, requested_url: str | None = None):
    s = await ub.page.evaluate(SENSITIVE_JS)
    if s["pw"]:
        if requested_url and ub.page.url.rstrip("/") != requested_url.rstrip("/"):
            blocked("session_expired", f"Redirected to a sign-in page ({ub.page.url}). Sign in via Computer → Take control.")
        blocked("human_required", f"Password entry required at {ub.page.url}. A human must sign in (Take control).")
    if s["captcha"]:
        blocked("human_required", f"CAPTCHA / human check at {ub.page.url}. A human must complete it.")
    if s["mfa"]:
        blocked("human_required", f"Multi-factor verification at {ub.page.url}. A human must complete it.")


async def page_state(ub):
    snap = await ub.page.evaluate(SNAPSHOT_JS)
    console = [c for c in ub.console if c["url"] == ub.page.url][-20:]
    return {"url": ub.page.url, **{k: Redactor.obj(v) for k, v in snap.items()}, "console": console}


class Base(BaseModel):
    user_id: str
    task_id: str = ""


class OpenB(Base):
    url: str


class ClickB(Base):
    role: str = ""
    name: str


class TypeB(Base):
    label: str
    text: str
    submit: bool = False


class WaitB(Base):
    text: str
    timeout_s: int = 15


class UploadB(Base):
    label: str
    path: str


def locate(page, role: str, name: str):
    if role:
        return page.get_by_role(role, name=name).first
    return page.get_by_text(name).first


async def automation(body: Base, fn, requested_url=None, check_after=True):
    ub = await browser_for(body.user_id)
    async with ub.lock:
        acquire(ub, body.task_id)
        try:
            out = await fn(ub)
        except PWTimeout as e:
            raise HTTPException(404, f"element or condition not found in time: {str(e).splitlines()[0][:200]}") from e
        except PWError as e:
            if body.task_id in State.cancelled:
                raise HTTPException(499, "cancelled") from e
            raise HTTPException(400, f"browser error: {str(e).splitlines()[0][:300]}") from e
        if check_after:
            await guard_sensitive(ub, requested_url)
        return out


@app.post("/browser/open", dependencies=[Depends(auth)])
async def b_open(b: OpenB):
    try:
        check_url(b.url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    async def go(ub):
        await ub.page.goto(b.url, wait_until="domcontentloaded", timeout=30000)
        return await page_state(ub)
    return await automation(b, go, requested_url=b.url)


@app.post("/browser/snapshot", dependencies=[Depends(auth)])
async def b_snapshot(b: Base):
    return await automation(b, page_state)


@app.post("/browser/click", dependencies=[Depends(auth)])
async def b_click(b: ClickB):
    async def go(ub):
        await locate(ub.page, b.role, b.name).click(timeout=5000)
        await ub.page.wait_for_load_state("domcontentloaded")
        return await page_state(ub)
    return await automation(b, go)


@app.post("/browser/type", dependencies=[Depends(auth)])
async def b_type(b: TypeB):
    async def go(ub):
        loc = ub.page.get_by_label(b.label).or_(ub.page.get_by_placeholder(b.label)).first
        typ = (await loc.get_attribute("type", timeout=5000) or "").lower()
        ac = (await loc.get_attribute("autocomplete") or "").lower()
        if typ == "password" or "password" in ac or "one-time-code" in ac:
            blocked("human_required", "Bots never type into password or one-time-code fields. A human must do this.")
        await loc.fill(b.text, timeout=5000)
        if b.submit:
            await loc.press("Enter")
            await ub.page.wait_for_load_state("domcontentloaded")
        return await page_state(ub)
    return await automation(b, go)


@app.post("/browser/wait", dependencies=[Depends(auth)])
async def b_wait(b: WaitB):
    async def go(ub):
        await ub.page.get_by_text(b.text).first.wait_for(timeout=b.timeout_s * 1000)
        return {"found": True, "url": ub.page.url}
    return await automation(b, go)


@app.post("/browser/screenshot", dependencies=[Depends(auth)])
async def b_screenshot(b: Base):
    async def go(ub):
        png = await ub.page.screenshot(type="png")
        return {"png_b64": base64.b64encode(png).decode(), "url": ub.page.url}
    return await automation(b, go, check_after=False)


@app.post("/browser/upload", dependencies=[Depends(auth)])
async def b_upload(b: UploadB):
    path = workspace.resolve(b.user_id, b.path, must_exist=True)

    async def go(ub):
        await ub.page.get_by_label(b.label).first.set_input_files(str(path), timeout=5000)
        return {"uploaded": b.path, "url": ub.page.url}
    return await automation(b, go)


@app.post("/browser/download", dependencies=[Depends(auth)])
async def b_download(b: ClickB):
    async def go(ub):
        async with ub.page.expect_download(timeout=30000) as info:
            await locate(ub.page, b.role, b.name).click(timeout=5000)
        dl = await info.value
        name = re.sub(r"[^\w.\- ]", "_", dl.suggested_filename)[:120] or "download.bin"
        dest = workspace.resolve(b.user_id, f"downloads/{name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        workspace.backup(b.user_id, dest)
        await dl.save_as(str(dest))
        return {"saved": f"downloads/{name}", "size": dest.stat().st_size}
    return await automation(b, go)


# ---------------- human control & live view ----------------

class HumanB(BaseModel):
    user_id: str
    human_id: str = "user"


class HumanClick(HumanB):
    x: float
    y: float


class HumanType(HumanB):
    text: str = Field(max_length=5000)
    sensitive: bool = False


class HumanKey(HumanB):
    key: str = Field(pattern=r"^(Enter|Tab|Backspace|Escape|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|PageDown|PageUp)$")


class HumanGoto(HumanB):
    url: str


def require_human(ub: UserBrowser):
    if ub.holder_kind != "human":
        blocked("not_in_control", "Take control first")


@app.post("/control/takeover", dependencies=[Depends(auth)])
async def takeover(b: HumanB):
    ub = await browser_for(b.user_id)
    async with ub.lock:  # waits for any in-flight automation step, then revokes the automation lease
        revoked = ub.holder_id if ub.holder_kind == "task" else None
        ub.holder_kind, ub.holder_id, ub.expires = "human", b.human_id, float("inf")
    return {"controller": "human", "revoked_task": revoked}


@app.post("/control/release", dependencies=[Depends(auth)])
async def release(b: HumanB):
    ub = await browser_for(b.user_id)
    async with ub.lock:
        ub.holder_kind, ub.holder_id, ub.expires = "none", "", 0
    return {"controller": "none"}


@app.post("/control/status", dependencies=[Depends(auth)])
async def control_status(b: HumanB):
    ub = State.browsers.get(b.user_id) or UserBrowser()
    held = ub.holder_kind if (ub.holder_kind != "task" or ub.expires > time.monotonic()) else "none"
    return {"controller": held, "holder": ub.holder_id if held != "none" else "",
            "recording": ub.recording is not None, "open": ub.ctx is not None}


@app.post("/human/screenshot", dependencies=[Depends(auth)])
async def h_screenshot(b: HumanB):
    """Live view (allowed for the owner at any time; never fed to a model)."""
    ub = await browser_for(b.user_id)
    png = await ub.page.screenshot(type="jpeg", quality=60)
    return {"jpeg_b64": base64.b64encode(png).decode(), "url": ub.page.url, "controller": ub.holder_kind}


@app.post("/human/goto", dependencies=[Depends(auth)])
async def h_goto(b: HumanGoto):
    ub = await browser_for(b.user_id)
    require_human(ub)
    try:
        check_url(b.url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await ub.page.goto(b.url, wait_until="domcontentloaded")
    if ub.recording is not None:
        ub.recording.append({"action": "open", "url": b.url})
    return {"url": ub.page.url}


@app.post("/human/click", dependencies=[Depends(auth)])
async def h_click(b: HumanClick):
    ub = await browser_for(b.user_id)
    require_human(ub)
    target = await ub.page.evaluate(POINT_JS, [b.x, b.y])
    await ub.page.mouse.click(b.x, b.y)
    ub.focused_sensitive = bool(target and target.get("sensitive"))
    if ub.recording is not None and target and not target.get("sensitive"):
        ub.recording.append({"action": "click", "role": target["role"], "name": target["name"]})
    return {"url": ub.page.url, "target": {k: v for k, v in (target or {}).items() if k != "sensitive"}}


@app.post("/human/type", dependencies=[Depends(auth)])
async def h_type(b: HumanType):
    ub = await browser_for(b.user_id)
    require_human(ub)
    focused = await ub.page.evaluate("""() => { const e = document.activeElement; if (!e) return {};
        return {password: e.type === 'password', label: (e.labels && e.labels[0] && e.labels[0].innerText) ||
                e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.name || '',
                ac: e.getAttribute('autocomplete') || ''}; }""")
    sensitive = b.sensitive or focused.get("password") or ub.focused_sensitive or \
        bool(re.search(r"password|one-time-code|cc-", focused.get("ac", "")))
    await ub.page.keyboard.type(b.text)
    if ub.recording is not None:
        if sensitive:
            ub.recording.append({"action": "sensitive_input_excluded"})
        else:
            n = sum(1 for s in ub.recording if s.get("action") == "type")
            ub.recording.append({"action": "type", "label": focused.get("label", "")[:80], "param": f"input_{n + 1}",
                                 "example": b.text[:200]})
    return {"ok": True, "sensitive": bool(sensitive)}  # the typed text is never echoed or logged


@app.post("/human/key", dependencies=[Depends(auth)])
async def h_key(b: HumanKey):
    ub = await browser_for(b.user_id)
    require_human(ub)
    await ub.page.keyboard.press(b.key)
    if ub.recording is not None and b.key == "Enter":
        ub.recording.append({"action": "press", "key": "Enter"})
    return {"url": ub.page.url}


@app.post("/record/start", dependencies=[Depends(auth)])
async def rec_start(b: HumanB):
    ub = await browser_for(b.user_id)
    ub.recording = [{"action": "open", "url": ub.page.url}] if ub.page.url.startswith("http") else []
    return {"recording": True}


@app.post("/record/stop", dependencies=[Depends(auth)])
async def rec_stop(b: HumanB):
    ub = await browser_for(b.user_id)
    steps, ub.recording = ub.recording or [], None
    return {"steps": steps}


@app.post("/session/delete", dependencies=[Depends(auth)])
async def session_delete(b: HumanB):
    ub = State.browsers.pop(b.user_id, None)
    if ub and ub.ctx:
        async with ub.lock:
            await ub.ctx.close()
    shutil.rmtree(profile_dir(b.user_id), ignore_errors=True)
    return {"deleted": True}


# ---------------- terminal ----------------

class RunB(Base):
    argv: list[str] | None = None
    shell: str | None = None
    cwd: str = ""
    timeout_s: int = Field(60, ge=1, le=600)


def _limits():  # POSIX only (container); rlimits per process tree
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 << 20, 512 << 20))
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))


def _kill_tree(p: subprocess.Popen):
    if p.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@app.post("/terminal/run", dependencies=[Depends(auth)])
async def t_run(b: RunB):
    if b.task_id in State.cancelled:
        raise HTTPException(499, "cancelled")
    if bool(b.argv) == bool(b.shell):
        raise HTTPException(400, "give exactly one of argv or shell")
    cwd = workspace.resolve(b.user_id, b.cwd, must_exist=True)
    argv = b.argv or (["cmd", "/c", b.shell] if IS_WIN else ["/bin/sh", "-c", b.shell])
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "LANG": "C.UTF-8",
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "TEMP": str(cwd), "TMP": str(cwd)}
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN else {"start_new_session": True, "preexec_fn": _limits}
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, **kw)
    except (FileNotFoundError, PermissionError) as e:
        raise HTTPException(400, f"cannot start {argv[0]}: {e}") from e
    State.procs.setdefault(b.task_id, set()).add(p)
    start = time.monotonic()
    try:
        while p.poll() is None:
            if b.task_id in State.cancelled:
                _kill_tree(p)
                raise HTTPException(499, "cancelled")
            if time.monotonic() - start > b.timeout_s:
                _kill_tree(p)
                out = p.stdout.read(65536).decode("utf-8", "replace")
                return {"exit_code": None, "timed_out": True, "output": Redactor.text(out)}
            await asyncio.sleep(0.1)
        out = p.stdout.read(65536).decode("utf-8", "replace")
        return {"exit_code": p.returncode, "timed_out": False, "output": Redactor.text(out),
                "seconds": round(time.monotonic() - start, 2)}
    finally:
        State.procs.get(b.task_id, set()).discard(p)


# ---------------- cancellation ----------------

class CancelB(BaseModel):
    task_id: str


@app.post("/cancel", dependencies=[Depends(auth)])
async def cancel(b: CancelB):
    State.cancelled.add(b.task_id)
    killed = 0
    for p in list(State.procs.get(b.task_id, ())):
        _kill_tree(p)
        killed += 1
    aborted = False
    for ub in State.browsers.values():
        if ub.holder_kind == "task" and ub.holder_id == b.task_id:
            try:
                await ub.page.close()  # aborts in-flight navigation/clicks for this task
            except PWError:
                pass
            ub.holder_kind, ub.holder_id, ub.expires = "none", "", 0
            aborted = True
    return {"killed_processes": killed, "aborted_browser": aborted}


@app.post("/browser/release", dependencies=[Depends(auth)])
async def b_release(b: CancelB):
    """Called by the worker when a task stops running so waiting tasks need not wait for lease expiry."""
    for ub in State.browsers.values():
        if ub.holder_kind == "task" and ub.holder_id == b.task_id:
            ub.holder_kind, ub.holder_id, ub.expires = "none", "", 0
    return {"released": True}


@app.get("/health")
async def health():
    return {"ok": True, "browsers_open": sum(1 for b in State.browsers.values() if b.ctx), "platform": sys.platform}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("COMPUTER_HOST", "127.0.0.1"), port=int(os.environ.get("COMPUTER_PORT", "8765")))
