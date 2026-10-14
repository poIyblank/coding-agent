"""One atomically replaced JSON state; CLI holds a process lock."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".agent-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, folder, workspace):
        self.folder = Path(folder).resolve()
        self.root = Path(workspace).resolve()
        if self.folder.is_relative_to(self.root):
            raise ValueError("State directory must be outside workspace")
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, run_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
            raise ValueError("Invalid run_id")
        return self.folder / (run_id + ".json")

    def save(self, state):
        data = json.dumps(state, ensure_ascii=False, indent=2).encode()
        atomic_bytes(self.path(state["run_id"]), data)

    def load(self, run_id):
        return json.loads(self.path(run_id).read_text())

    @contextmanager
    def lock(self):
        # Per-workspace lock, also covers different run_ids/state directories.
        import hashlib
        key = hashlib.sha256(str(self.root).encode()).hexdigest()[:20]
        path = self.root.parent / (".agent-lock-" + key)
        with path.open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another runtime is using this workspace") from exc
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)
