"""CLI deliberately exits at approvals. No process must remain alive to resume."""
import argparse
import json
from pathlib import Path
import sys

from .demo import DemoModel
from .model import APIModel
from .runtime import BudgetError, InjectedCrash, Runtime
from .storage import Store

TASK = "Исправь расчёт итоговой стоимости заказа, чтобы тесты проходили. Тесты не меняй. Сам найди нужные файлы, внеси исправление и запусти тесты."


def checklist(state):
    marks = {"done": "x", "running": ">", "pending": " "}
    for t in state["tasks"]:
        print(f"[{marks[t['status']]}] {t['id']}: {t['description']}")


def show(runtime):
    s = runtime.s
    print(f"run_id: {s['run_id']}\nstatus: {s['status']}\nphase: {s['phase']}\nmode: {s['mode']}")
    checklist(s)
    print("budget:", json.dumps(s["spent"], ensure_ascii=False), "/", json.dumps(s["limits"]))
    if s["status"] == "wait_plan":
        print(json.dumps(s["plan"], ensure_ascii=False, indent=2))
        print("Next: python3 -m agent approve-plan " + s["run_id"])
    elif s["status"] == "wait_action":
        record = s["calls"][s["batch"][0]["id"]]
        print("Action awaiting approval:", record["name"])
        print(json.dumps(record["args"], ensure_ascii=False, indent=2))
        print("Next: python3 -m agent approve " + s["run_id"] + " (or deny)")
    elif s["status"] == "uncertain":
        print("Uncertain action:", runtime.uncertain_record()["id"])
        print(runtime.uncertain_diff())
        print("Next: inspect uncertain-diff; resolve RUN_ID {retry,fail,abort} [--adopt-changes]")
    elif s["status"] == "completed":
        print(s["messages"][-1]["content"])
    if s["error"]:
        print("Last diagnostic:", s["error"])


def parser():
    p = argparse.ArgumentParser(description="Учебный coding agent с собственным runtime")
    p.add_argument("--state-dir", default=".runs", help="Place before the subcommand")
    sub = p.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--workspace", default="workspace")
    start.add_argument("--policy", default="policy.json")
    start.add_argument("--mode", choices=["safe", "auto"], default="safe")
    start.add_argument("--model", choices=["api", "demo"], default="api")
    start.add_argument("--task", default=TASK)
    start.add_argument("--limits", help="JSON file containing budget limits")
    start.add_argument("--run-id")
    for cmd in ["resume", "status", "tasks", "diff", "uncertain-diff", "rollback",
                "approve-plan", "reject-plan", "approve", "deny", "resolve"]:
        child = sub.add_parser(cmd)
        child.add_argument("run_id")
        if cmd in {"deny", "reject-plan"}:
            child.add_argument("--reason", default="User rejected the proposal")
        if cmd == "resolve":
            child.add_argument("decision", choices=["retry", "fail", "abort"])
            child.add_argument("--adopt-changes", action="store_true")
        if cmd in {"resume", "approve-plan", "reject-plan", "approve", "deny", "resolve", "rollback"}:
            child.add_argument("--crash-at", choices=["after_charge", "after_execute", "after_model_charge",
                                                     "rollback_after_start", "rollback_after_file"])

    return p


def execute(args):
    if args.command == "start":
        store = Store(args.state_dir, args.workspace)
        with store.lock():
            limits = json.loads(Path(args.limits).read_text()) if args.limits else None
            r = Runtime.create(args.workspace, args.policy, args.state_dir, args.task,
                               args.mode, limits, args.model, args.run_id)
            drive(r)
    else:
        # Only read metadata before locking. Reload state while holding the lock.
        candidate = Runtime.load(args.state_dir, args.run_id)
        with candidate.store.lock():
            r = Runtime.load(args.state_dir, args.run_id, getattr(args, "crash_at", None))
            cmd = args.command
            if cmd == "diff":
                print(r.diff())
                return 0
            if cmd == "uncertain-diff":
                print(r.uncertain_diff())
                return 0
            if cmd == "tasks":
                checklist(r.s)
                return 0
            if cmd == "approve-plan":
                r.approve_plan(True)
            elif cmd == "reject-plan":
                r.approve_plan(False, args.reason)
            elif cmd == "approve":
                r.decide_action(True)
            elif cmd == "deny":
                r.decide_action(False, args.reason)
            elif cmd == "resolve":
                r.resolve(args.decision, args.adopt_changes)
            elif cmd == "rollback":
                r.rollback()
            if cmd not in {"status", "rollback"}:
                drive(r)
    show(r)
    return 1 if r.s["status"] in {"failed", "budget_exhausted", "model_error", "conflict"} else 0


def drive(runtime):
    class ReportingAPIModel(APIModel):
        def complete(self, messages):
            print(f"Model request {runtime.s['spent']['model_calls']}...", flush=True)
            response = super().complete(messages)
            calls = response.get("tool_calls")
            count = len(calls) if isinstance(calls, list) else "invalid"
            print(f"Model response received; tool calls: {count}", flush=True)
            return response

    model = DemoModel() if runtime.s["model_kind"] == "demo" else ReportingAPIModel()
    runtime.drive(model)


def main():
    args = parser().parse_args()
    try:
        return execute(args)
    except InjectedCrash as exc:
        print(f"Simulated crash at {exc}. Run resume with the same run_id.", file=sys.stderr)
        return 99
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
