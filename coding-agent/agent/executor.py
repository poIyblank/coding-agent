"""Trusted executor. It knows no model/history/plans; runtime calls it after checks."""
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from .policy import PolicyError
from .storage import atomic_bytes, fsync_dir


def run_bash(command, cwd, timeout, max_bytes):
    # Clean environment: no API keys, BASH_ENV, PYTHONPATH, startup hooks.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    proc = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", command],
                            cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    collected = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = False
    truncated = False
    remaining = max_bytes
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                collected[key.data].extend(chunk[:remaining])
                remaining -= min(remaining, len(chunk))
                if remaining == 0:
                    truncated = True
                    break
            if truncated:
                break
        if not timed_out and not truncated:
            try:
                proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        # Kill the whole process group, including children that retain open pipes.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        selector.close()
        proc.stdout.close()
        proc.stderr.close()
    return {"ok": proc.returncode == 0 and not timed_out and not truncated,
            "stdout": collected["stdout"].decode("utf-8", errors="replace"),
            "stderr": collected["stderr"].decode("utf-8", errors="replace"),
            "exit_code": proc.returncode, "timed_out": timed_out, "truncated": truncated}


class Executor:
    def __init__(self, policy):
        self.policy = policy

    def execute(self, name, args, max_bytes):
        if name == "Bash":
            cwd = self.policy.command(args["command"], args["cwd"])
            return run_bash(args["command"], cwd, self.policy.timeout, max_bytes)
        path = self.policy.path(args["path"], write=name in {"write_file", "delete_file"},
                                directory=name == "list_files")
        if name == "list_files":
            files = []
            # One level is intentional: agent decides which directory to explore next.
            for child in sorted(path.iterdir()):
                rel = child.relative_to(self.policy.root).as_posix()
                try:
                    self.policy.path(rel)
                except PolicyError:
                    continue
                files.append(rel + ("/" if child.is_dir() else ""))
                if len("\n".join(files).encode()) > max_bytes:
                    return {"ok": True, "files": files[:-1], "truncated": True}
            return {"ok": True, "files": files}
        if name == "read_file":
            with path.open("rb") as file:
                data = file.read(max_bytes + 1)
            cut = len(data) > max_bytes
            return {"ok": True, "content": data[:max_bytes].decode("utf-8", errors="replace"),
                    "truncated": cut}
        if name == "write_file":
            data = args["content"].encode("utf-8")
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            atomic_bytes(path, data, mode)
            return {"ok": True, "path": args["path"], "sha256": hashlib.sha256(data).hexdigest()}
        if name == "delete_file":
            path.unlink()
            fsync_dir(path.parent)
            return {"ok": True, "deleted": args["path"]}
        raise ValueError("Not an executable file/command tool")
