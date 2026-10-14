"""Content snapshots for the permitted project tree. No generated-file exclusions."""
import base64
import difflib
import hashlib
import os
from pathlib import Path
import stat

from .policy import PolicyError
from .storage import atomic_bytes, fsync_dir


class Conflict(ValueError):
    pass


def snapshot(policy):
    tree = {}
    size = 0
    for parent, dirs, files in os.walk(policy.root, followlinks=False):
        # Traverse allowed ancestors, but never a denied subtree.
        keep = []
        for name in sorted(dirs):
            p = Path(parent) / name
            if any(p.is_relative_to(d) for d in policy.denied):
                continue
            if p.is_symlink():
                raise PolicyError("Symlink in project tree: " + str(p))
            if policy.permitted(p) or any(a.is_relative_to(p) for a in policy.allowed):
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            p = Path(parent) / name
            if not policy.permitted(p):
                continue
            rel = p.relative_to(policy.root).as_posix()
            p = policy.path(rel)
            if not stat.S_ISREG(p.stat().st_mode):
                raise PolicyError("Only regular files are supported")
            if p.stat().st_size > 2 * 1024 * 1024:
                raise PolicyError("File too large for this teaching project")
            data = p.read_bytes()
            size += len(data)
            if size > 16 * 1024 * 1024:
                raise PolicyError("Project snapshot exceeds 16 MiB")
            tree[rel] = {"sha256": hashlib.sha256(data).hexdigest(),
                         "data": base64.b64encode(data).decode(),
                         "mode": stat.S_IMODE(p.stat().st_mode)}
    return tree


def changed(before, after):
    return {p: {"before": before.get(p), "after": after.get(p)}
            for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)}


def digest(tree):
    import json
    return hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest()


def readable_diff(before, after):
    chunks = []
    for path, delta in changed(before, after).items():
        def lines(entry):
            if entry is None:
                return []
            return base64.b64decode(entry["data"]).decode("utf-8", errors="replace").splitlines(True)
        chunks.append(f"\nFile: {path}\n")
        for label in ("before", "after"):
            entry = delta[label]
            chunks.append(f"{label}: {entry['sha256'] if entry else '(absent)'}\n")
        chunks.extend(difflib.unified_diff(lines(delta["before"]), lines(delta["after"]),
                                         fromfile="a/" + path, tofile="b/" + path))
    return "".join(chunks) or "No changes.\n"


def restore_file(policy, path, entry):
    target = policy.path(path, write=True)
    if entry is None:
        if target.exists():
            target.unlink()
            fsync_dir(target.parent)
    else:
        atomic_bytes(target, base64.b64decode(entry["data"]), entry["mode"])
