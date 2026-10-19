"""Process limits for the terminal tool.

Windows: each command runs in its own Job Object (created suspended, assigned, then resumed, so no child escapes
the job). Limits: committed memory per job, number of active processes. Closing/terminating the job kills the
whole tree. POSIX: new session + rlimits (address space, file size, CPU seconds, processes), kill by process group.
Output is drained continuously (no pipe deadlock) and capped.

Not provided here: filesystem or network isolation. Natively the command runs as the user that runs LocalCrew;
isolation needs the container/VM deployment (docs/ARCHITECTURE.md).
"""
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field

IS_WIN = os.name == "nt"


@dataclass(frozen=True)
class Limits:
    memory_mb: int = int(os.environ.get("LOCALCREW_TERMINAL_MEMORY_MB", "1024"))
    max_procs: int = int(os.environ.get("LOCALCREW_TERMINAL_MAX_PROCS", "64"))
    max_output: int = int(os.environ.get("LOCALCREW_TERMINAL_MAX_OUTPUT", "65536"))
    file_size_mb: int = 512
    cpu_seconds: int = 600


if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class _Ext(ctypes.Structure):
        _fields_ = [("Basic", _Basic), ("Io", _Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _LIMIT_ACTIVE_PROCESS, _LIMIT_PROCESS_MEMORY, _LIMIT_JOB_MEMORY = 0x8, 0x100, 0x200
    _LIMIT_DIE_ON_UNHANDLED_EXCEPTION, _LIMIT_KILL_ON_JOB_CLOSE = 0x400, 0x2000
    _CREATE_SUSPENDED = 0x4

    def _make_job(lim: Limits):
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = _Ext()
        info.Basic.LimitFlags = (_LIMIT_ACTIVE_PROCESS | _LIMIT_PROCESS_MEMORY | _LIMIT_JOB_MEMORY |
                                 _LIMIT_DIE_ON_UNHANDLED_EXCEPTION | _LIMIT_KILL_ON_JOB_CLOSE)
        info.Basic.ActiveProcessLimit = lim.max_procs
        info.ProcessMemoryLimit = info.JobMemoryLimit = lim.memory_mb * 1024 * 1024
        if not _k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(h)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        return h


@dataclass(eq=False)  # identity hashing: kept in a set per task
class Proc:
    popen: subprocess.Popen
    job: object = None
    out: bytearray = field(default_factory=bytearray)
    total: int = 0
    reader: threading.Thread | None = None

    def kill(self):
        if IS_WIN and self.job:
            _k32.TerminateJobObject(self.job, 1)  # whole tree, including grandchildren
        elif not IS_WIN and self.popen.poll() is None:
            try:
                os.killpg(self.popen.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        if self.reader:
            self.reader.join(5)
        if IS_WIN and self.job:
            _k32.CloseHandle(self.job)  # KILL_ON_JOB_CLOSE: nothing outlives the command
            self.job = None

    def output(self) -> tuple[str, bool]:
        return self.out.decode("utf-8", "replace"), self.total > len(self.out)


def _posix_limits(lim: Limits):
    def apply():
        import resource
        mem = lim.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim.file_size_mb << 20, lim.file_size_mb << 20))
        resource.setrlimit(resource.RLIMIT_CPU, (lim.cpu_seconds, lim.cpu_seconds))
        # No RLIMIT_NPROC: on Linux it counts every process *and thread* of the user (Chromium alone has hundreds),
        # so it cannot express a per-command cap. Process counts are capped by the container's pids_limit instead.
    return apply


def spawn(argv: list[str], cwd, env: dict, lim: Limits | None = None) -> Proc:
    lim = lim or Limits()
    if IS_WIN:
        job = _make_job(lim)
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED)
        except BaseException:
            _k32.CloseHandle(job)
            raise
        if not _k32.AssignProcessToJobObject(job, int(p._handle)):
            p.kill()
            _k32.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        _ntdll.NtResumeProcess(int(p._handle))
        proc = Proc(p, job)
    else:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=_posix_limits(lim))  # noqa: PLW1509
        proc = Proc(p)

    def drain():
        for chunk in iter(lambda: p.stdout.read1(8192), b""):
            proc.total += len(chunk)
            room = lim.max_output - len(proc.out)
            if room > 0:
                proc.out += chunk[:room]
    proc.reader = threading.Thread(target=drain, daemon=True)
    proc.reader.start()
    return proc
