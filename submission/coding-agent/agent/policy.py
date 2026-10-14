"""Policy has no dependency on the agent loop. Deny always wins."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re


class PolicyError(ValueError):
    pass


class Policy:
    # This is deliberately a small grammar: words only, no shell operators/quotes.
    SIMPLE = re.compile(r"[A-Za-z0-9_-]+(?: [A-Za-z0-9_.-]+)*\Z")
    # New command *shapes* require a code review, not just an allow-list entry.
    SUPPORTED = {"python3 -m unittest -v", "python3 -m unittest", "pwd"}

    def __init__(self, filename, workspace):
        self.filename = Path(filename).resolve(strict=True)
        self.root = Path(workspace).resolve(strict=True)
        if not self.root.is_dir():
            raise PolicyError("workspace is not a directory")
        if self.filename.is_relative_to(self.root):
            raise PolicyError("Policy must be outside workspace")
        raw = self.filename.read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        self.data = json.loads(raw)
        dirs = self.data["directories"]
        self.allowed = [self._config_path(p) for p in dirs["allow"]]
        self.denied = [self._config_path(p) for p in dirs["deny"]]
        self.read_only = self.data.get("read_only", [])
        self.timeout = float(self.data.get("bash_timeout", 10))
        self.output_limit = int(self.data.get("max_output_bytes", 16384))
        if not 0 < self.timeout <= 60 or not 256 <= self.output_limit <= 1048576:
            raise PolicyError("Invalid timeout/output limit")

    def _config_path(self, raw):
        p = Path(raw)
        if p.is_absolute() or ".." in p.parts:
            raise PolicyError("Configured directories must be workspace-relative without ..")
        result = (self.root / p).resolve()
        if not result.is_relative_to(self.root):
            raise PolicyError("Configured directory leaves workspace")
        # A nonexistent denied directory is fine, but existing symlinks are not.
        cur = self.root
        for part in p.parts:
            cur /= part
            if cur.is_symlink():
                raise PolicyError("Symlinks in policy directories are forbidden")
        return result

    def permitted(self, p):
        return (p.is_relative_to(self.root)
                and any(p.is_relative_to(a) for a in self.allowed)
                and not any(p.is_relative_to(d) for d in self.denied))

    def path(self, raw, *, write=False, directory=False):
        p = Path(raw)
        if not raw or p.is_absolute() or ".." in p.parts or "\x00" in raw:
            raise PolicyError("Use a relative path without ..")
        cur = self.root
        for part in p.parts:
            cur /= part
            if cur.is_symlink():
                raise PolicyError("Symlink paths are forbidden")
        target = (self.root / p).resolve()
        if not self.permitted(target):
            raise PolicyError("Path denied by directory policy")
        if target.exists() and not (target.is_file() or target.is_dir()):
            raise PolicyError("Special files are forbidden")
        if target.exists() and target.is_file() and target.stat().st_nlink != 1:
            raise PolicyError("Hard-linked files are forbidden")
        if directory and not target.is_dir():
            raise PolicyError("cwd/path must be an existing directory")
        relative = target.relative_to(self.root).as_posix()
        if write and any(fnmatch.fnmatchcase(relative, rule) for rule in self.read_only):
            raise PolicyError("Read-only file: " + relative)
        if write and (target == self.root or not target.parent.is_dir()):
            raise PolicyError("Parent directory must already exist")
        return target

    def command(self, command, cwd):
        path = self.path(cwd, directory=True)
        commands = self.data["commands"]
        if command in commands["deny"]:
            raise PolicyError("Command explicitly denied")
        if command not in commands["allow"]:
            raise PolicyError("Command is not explicitly allowed")
        if not self.SIMPLE.fullmatch(command) or command not in self.SUPPORTED:
            raise PolicyError("Unsupported command syntax/arguments")
        return path

    def check(self, name, args, phase, mode):
        if mode not in {"safe", "auto"}:
            raise PolicyError("Unknown execution mode")
        if phase == "plan" and name not in {"list_files", "read_file", "submit_plan"}:
            raise PolicyError("Plan Mode allows only reading and submit_plan; Bash is blocked")
        if phase != "plan" and name == "submit_plan":
            raise PolicyError("Plan already approved")
        if name == "Bash":
            self.command(args["command"], args["cwd"])
        elif name in {"list_files", "read_file", "write_file", "delete_file"}:
            self.path(args["path"], write=name in {"write_file", "delete_file"},
                      directory=name == "list_files")
        return name == "delete_file" or (mode == "safe" and name in {"write_file", "Bash"})
