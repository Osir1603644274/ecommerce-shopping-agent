"""Durable observed child lifecycle; Windows job owns only this new child tree.

The runner blocks on a start gate until job ownership is established. Closing
the supervisor/job (including host termination) kills its children, not other
Codex sessions. A lost supervisor itself cannot manufacture a terminal record;
the immutable started record supports next-turn interruption reconciliation.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .artifacts import ROOT, append, now, write_new


class OwnedProcessJob:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        class Basic(ctypes.Structure):
            _fields_ = [("processTime", ctypes.c_int64), ("jobTime", ctypes.c_int64),
                ("limitFlags", wintypes.DWORD), ("minWorkingSet", ctypes.c_size_t),
                ("maxWorkingSet", ctypes.c_size_t), ("activeProcesses", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priorityClass", wintypes.DWORD),
                ("schedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                ("readOps", "writeOps", "otherOps", "readBytes", "writeBytes", "otherBytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("processMemory", ctypes.c_size_t),
                ("jobMemory", ctypes.c_size_t), ("peakProcessMemory", ctypes.c_size_t),
                ("peakJobMemory", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.limitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def supervise(command, output, *, timeout, interval=30, start_token=None):
    output = Path(output)
    if not output.is_absolute() or timeout <= 0 or interval <= 0:
        raise ValueError("invalid_supervisor_boundary")
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    write_new(output / "started.json", {"at": now(), "supervisorPid": os.getpid(),
        "parentPid": os.getppid(), "command": command, "cwd": str(ROOT),
        "hardTimeoutSeconds": timeout, "pollSeconds": interval,
        "startGateRequired": start_token is not None, "scope": "OWNED_CHILD_ONLY"})
    process, job, reason, failure = None, None, None, None
    try:
        job = OwnedProcessJob()
        with (output / "stdout.log").open("x", encoding="utf-8") as stdout, (output / "stderr.log").open("x", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr,
                stdin=subprocess.PIPE if start_token is not None else subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt")
            job.assign(process)
            write_new(output / "child.json", {"at": now(), "pid": process.pid,
                "parentPid": os.getpid(), "command": command,
                "ownership": "WINDOWS_JOB" if os.name == "nt" else "NEW_PROCESS_GROUP"})
            if start_token is not None:
                process.stdin.write(start_token + "\n")
                process.stdin.flush()
                process.stdin.close()
            last_size, unchanged = -1, 0
            while True:
                code = process.poll()
                elapsed = time.monotonic() - started
                if code is not None:
                    reason = "EXITED_ZERO" if code == 0 else "EXITED_NONZERO"
                    break
                if elapsed >= timeout:
                    reason = "HARD_TIMEOUT"
                    break
                size = (output / "stdout.log").stat().st_size + (output / "stderr.log").stat().st_size
                unchanged = unchanged + 1 if size == last_size else 0
                append(output / "observations.jsonl", {"at": now(), "pid": process.pid,
                    "elapsedSeconds": elapsed, "alive": True, "logBytes": size,
                    "unchangedChecks": unchanged, "stallAdvisoryOnly": unchanged >= 3})
                last_size = size
                try:
                    process.wait(timeout=min(interval, max(0.01, timeout - elapsed)))
                except subprocess.TimeoutExpired:
                    pass
    except BaseException as exc:
        reason = "SUPERVISOR_EXCEPTION"
        failure = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        if job is not None:
            job.close()  # Also cleans unexpected descendants after normal root exit.
        if process is not None:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()  # Exact child handle, also safe if job assignment failed.
            process.wait(timeout=10)
        write_new(output / "result.json", {"at": now(), "reason": reason, "failure": failure,
            "childExitCode": process.returncode if process is not None else None,
            "elapsedSeconds": time.monotonic() - started, "allExpectedOutputsVerified": False,
            "note": "Exit zero does not prove experiment/quality completion. Read attempt result and source audit."})
    return process.returncode if reason != "HARD_TIMEOUT" else 124


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("supervisor_output", type=Path)
    parser.add_argument("attempt_output", type=Path)
    parser.add_argument("--hard-timeout", type=int, default=7320)
    args, runner_args = parser.parse_known_args()
    if not args.attempt_output.is_absolute() or args.attempt_output.exists():
        raise ValueError("new_absolute_attempt_required")
    if not 60 <= args.hard_timeout <= 7500:
        raise ValueError("supervisor_timeout_out_of_bounds")
    token = uuid.uuid4().hex
    command = [sys.executable, "-X", "utf8", "-B", "-m",
        "agent.evaluation.context_history_strategies_v1_20260905.agent_smoke", str(args.attempt_output),
        "--supervised-start-token", token, *runner_args]
    raise SystemExit(supervise(command, args.supervisor_output, timeout=args.hard_timeout, start_token=token))
