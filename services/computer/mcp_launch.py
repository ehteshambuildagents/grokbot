"""Launcher for stdio MCP servers: applies the terminal sandbox limits, then runs the server with inherited stdio.

  python mcp_launch.py <memory_mb> <max_procs> -- <command> [args...]

Windows: puts itself in a Job Object (memory, process count, kill-on-close) before starting the server, so the
server and everything it spawns inherit the job and die with it. POSIX: sets rlimits and execs the server.
"""
import os
import subprocess
import sys


def main():
    mem_mb, max_procs = int(sys.argv[1]), int(sys.argv[2])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    if not cmd:
        sys.exit("no command")
    if os.name == "nt":
        import ctypes

        import sandbox
        job = sandbox._make_job(sandbox.Limits(memory_mb=mem_mb, max_procs=max_procs))
        if not sandbox._k32.AssignProcessToJobObject(job, ctypes.windll.kernel32.GetCurrentProcess()):
            sys.exit("could not apply process limits")
        sys.exit(subprocess.call(cmd))  # inherits stdin/stdout; the job handle stays open until we exit
    import resource
    mem = mem_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_NPROC, (max_procs * 8, max_procs * 8))
    os.execvp(cmd[0], cmd)  # noqa: S606 — argv list configured by the account owner, no shell


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
