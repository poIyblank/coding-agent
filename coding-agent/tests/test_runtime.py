import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent.cli import checklist
from agent.demo import DemoModel, FIXED_ORDER, PLAN, call, response
from agent.executor import run_bash
from agent.journal import Conflict, snapshot
from agent.policy import Policy, PolicyError
from agent.runtime import BudgetError, InjectedCrash, Runtime, wire_result
from agent.schema import ValidationError, parse_arguments, tools_for_model

ROOT = Path(__file__).resolve().parents[1]


class SequenceModel:
    def __init__(self, messages):
        self.messages = iter(messages)

    def complete(self, _):
        return next(self.messages)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.workspace = self.home / "workspace"
        shutil.copytree(ROOT / "workspace", self.workspace)
        self.policy_file = self.home / "policy.json"
        shutil.copy(ROOT / "policy.json", self.policy_file)
        self.states = self.home / "runs"
        self.original = (self.workspace / "order.py").read_bytes()
        self.tests = (self.workspace / "test_order.py").read_bytes()
        self.n = 0

    def runtime(self, mode="auto", limits=None, crash_at=None):
        self.n += 1
        return Runtime.create(self.workspace, self.policy_file, self.states, "Fix order",
                              mode=mode, limits=limits, run_id=f"test{self.n}", crash_at=crash_at)

    def reload(self, r, crash_at=None):
        return Runtime.load(self.states, r.s["run_id"], crash_at)

    def send(self, r, name, args, ident=None):
        ident = ident or f"c{len(r.s['calls'])}"
        r.accept(response(call(ident, name, args)))
        r.step()
        return r.s["calls"].get(ident)

    def execute_task(self, r, tools=None, kind="file", paths=None):
        plan = copy.deepcopy(PLAN)
        plan["tasks"] = [{"id": "work", "description": "Test task", "depends_on": [],
                          "tools": tools or ["read_file", "write_file", "delete_file", "Bash", "list_files"],
                          "result": {"kind": kind, "paths": ["order.py"] if paths is None else paths}}]
        self.send(r, "submit_plan", plan, "plan")
        r.approve_plan(True)

    def complete_demo(self, mode="auto"):
        r = self.runtime(mode)
        self.assertEqual(r.drive(DemoModel()), "wait_plan")
        r.approve_plan(True)
        while True:
            r.drive(DemoModel())
            if r.s["status"] != "wait_action":
                break
            r = self.reload(r)
            r.decide_action(True)
        self.assertEqual(r.s["status"], "completed")
        return r

    def test_demo_full_flow_bash_and_unchanged_tests(self):
        r = self.complete_demo()
        self.assertEqual((self.workspace / "test_order.py").read_bytes(), self.tests)
        self.assertEqual((self.workspace / "order.py").read_text(), FIXED_ORDER)
        bash = r.s["calls"]["test-bash"]
        self.assertEqual(bash["result"]["exit_code"], 0)
        self.assertIn("Ran 2 tests", bash["result"]["stderr"])
        self.assertTrue(all(t["status"] == "done" for t in r.s["tasks"]))
        self.assertEqual(r.s["spent"]["side_effects"], 2)
        self.assertEqual(len(r.s["messages"][2]["tool_calls"]), 1)
        self.assertEqual(r.s["spent"]["result_bytes"], sum(
            len(m["content"].encode()) for m in r.s["messages"] if m["role"] == "tool"))

    def test_safe_mode_stops_and_resumes_twice(self):
        r = self.complete_demo("safe")
        approvals = [d for d in r.s["decisions"] if d["kind"] == "action"]
        self.assertEqual(len(approvals), 2)
        self.assertTrue(all(d["approved"] for d in approvals))

    def test_multiple_tool_calls_and_ids(self):
        r = self.runtime()
        r.accept(response(call("a", "read_file", {"path": "order.py"}),
                          call("b", "read_file", {"path": "test_order.py"})))
        r.step()
        r = self.reload(r)
        r.step()
        results = [m for m in r.s["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["a", "b"])
        self.assertEqual(len(r.s["messages"][2]["tool_calls"]), 2)

    def test_tool_error_is_returned_and_agent_continues(self):
        r = self.runtime()
        model = SequenceModel([
            response(call("missing", "read_file", {"path": "missing.py"})),
            response(call("found", "read_file", {"path": "order.py"})),
            response(call("plan", "submit_plan", PLAN)),
        ])
        self.assertEqual(r.drive(model), "wait_plan")
        self.assertFalse(r.s["calls"]["missing"]["result"]["ok"])
        self.assertTrue(r.s["calls"]["found"]["result"]["ok"])

    def test_unknown_tool_and_invalid_schema(self):
        r = self.runtime()
        cases = [("no_tool", {}), ("read_file", {"path": 12}),
                 ("read_file", {"path": "order.py", "extra": True})]
        for name, args in cases:
            self.assertFalse(self.send(r, name, args)["result"]["ok"])

    def test_invalid_json(self):
        r = self.runtime()
        c = call("bad", "read_file", {})
        c["function"]["arguments"] = "{bad"
        r.accept(response(c))
        r.step()
        self.assertIn("Invalid JSON", r.s["calls"]["bad"]["result"]["error"])

    def test_allowed_and_denied_commands(self):
        p = Policy(self.policy_file, self.workspace)
        self.assertEqual(p.command("pwd", "."), self.workspace.resolve())
        for cmd in ["rm -rf .", "whoami", "pwd; pwd", "pwd | pwd", "$(pwd)",
                    "pwd > order.py", "bash -c pwd", "python3 -c print(1)", "pwd\npwd"]:
            with self.subTest(cmd=cmd), self.assertRaises(PolicyError):
                p.command(cmd, ".")

    def test_deny_wins_and_unsupported_shape_even_if_allowlisted(self):
        config = json.loads(self.policy_file.read_text())
        config["commands"]["deny"].append("pwd")
        config["commands"]["allow"].extend(["pwd; pwd", "python3 -m unittest ../test.py"])
        self.policy_file.write_text(json.dumps(config))
        p = Policy(self.policy_file, self.workspace)
        for command in ["pwd", "pwd; pwd", "python3 -m unittest ../test.py"]:
            with self.assertRaises(PolicyError):
                p.command(command, ".")

    def test_allowed_command_denied_cwd_and_file_subdir(self):
        r = self.runtime()
        self.execute_task(r)
        c = self.send(r, "Bash", {"command": "pwd", "cwd": "private", "task_id": "work"})
        self.assertFalse(c["result"]["ok"])
        c = self.send(r, "read_file", {"path": "private/README.txt", "task_id": "work"})
        self.assertFalse(c["result"]["ok"])
        self.assertEqual(r.s["spent"]["side_effects"], 0)

    def test_escape_absolute_symlink_and_hardlink_paths(self):
        p = Policy(self.policy_file, self.workspace)
        (self.workspace / "link").symlink_to(self.home)
        (self.workspace / "inside").symlink_to(self.workspace / "order.py")
        outside = self.home / "outside"
        outside.write_text("secret")
        os.link(outside, self.workspace / "hard")
        for path in ["../policy.json", str(outside), "link/policy.json", "inside", "hard"]:
            with self.subTest(path=path), self.assertRaises(PolicyError):
                p.path(path)

    def test_policy_and_state_must_be_outside_workspace(self):
        shutil.copy(self.policy_file, self.workspace / "policy.json")
        with self.assertRaises(PolicyError):
            Policy(self.workspace / "policy.json", self.workspace)
        with self.assertRaises(ValueError):
            Runtime.create(self.workspace, self.policy_file, self.workspace / ".runs", "x")

    def test_policy_directories_cannot_leave_workspace(self):
        config = json.loads(self.policy_file.read_text())
        config["directories"]["allow"] = ["../"]
        self.policy_file.write_text(json.dumps(config))
        with self.assertRaises(PolicyError):
            self.runtime()

    def test_tests_readonly(self):
        r = self.runtime()
        self.execute_task(r)
        c = self.send(r, "write_file", {"path": "test_order.py", "content": "", "task_id": "work"})
        self.assertFalse(c["result"]["ok"])
        self.assertEqual((self.workspace / "test_order.py").read_bytes(), self.tests)

    def test_policy_is_rechecked_after_approval(self):
        r = self.runtime("safe")
        self.execute_task(r)
        self.send(r, "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "work"})
        self.assertEqual(r.s["status"], "wait_action")
        r.decide_action(True)
        self.policy_file.write_text(self.policy_file.read_text() + "\n")
        r.step()
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)
        self.assertFalse(r.s["calls"]["c1"]["result"]["ok"])

    def test_denial_is_returned_to_model_and_alternative_runs(self):
        r = self.runtime("safe")
        self.execute_task(r)
        self.send(r, "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "work"}, "w")
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)
        r = self.reload(r)
        r.decide_action(False, "Please inspect first")
        self.assertIn("Please inspect first", r.s["messages"][-1]["content"])
        c = self.send(r, "read_file", {"path": "order.py", "task_id": "work"})
        self.assertTrue(c["result"]["ok"])
        self.assertEqual(r.s["spent"]["side_effects"], 0)

    def test_deletion_needs_confirmation_in_auto(self):
        r = self.runtime()
        self.execute_task(r)
        self.send(r, "delete_file", {"path": "order.py", "task_id": "work"}, "d")
        self.assertEqual(r.s["status"], "wait_action")
        r.decide_action(True)
        r.step()
        self.assertFalse((self.workspace / "order.py").exists())
        r.rollback()
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)

    def test_plan_blocks_all_side_effect_tools(self):
        r = self.runtime()
        before = snapshot(r.policy)
        for name, args in [("Bash", {"command": "pwd", "cwd": "."}),
                           ("write_file", {"path": "order.py", "content": "x"}),
                           ("delete_file", {"path": "order.py"})]:
            self.assertFalse(self.send(r, name, args)["result"]["ok"])
        self.assertEqual(snapshot(r.policy), before)
        self.assertEqual(r.s["spent"]["side_effects"], 0)

    def test_plan_approval_does_not_approve_safe_actions(self):
        r = self.runtime("safe")
        self.execute_task(r)
        self.send(r, "Bash", {"command": "pwd", "cwd": ".", "task_id": "work"})
        self.assertEqual(r.s["status"], "wait_action")
        self.assertEqual(r.s["spent"]["side_effects"], 0)

    def test_reject_plan_returns_to_readonly_phase(self):
        r = self.runtime()
        r.drive(DemoModel())
        before = dict(r.s["spent"])
        r.approve_plan(False, "Too broad")
        r = self.reload(r)
        self.assertEqual(r.s["phase"], "plan")
        self.assertEqual(r.s["spent"], before)
        self.assertIsNone(r.s["plan"])
        self.assertIn("Too broad", r.s["messages"][-1]["content"])

    def test_completed_call_does_not_repeat_after_reload(self):
        r = self.runtime()
        self.execute_task(r)
        c = call("write", "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "work"})
        r.accept(response(c))
        r.step()
        r = self.reload(r)
        with patch.object(r.executor, "execute", side_effect=AssertionError("Executed twice")):
            # step reloads executor, so also assert durable debit/attempt count.
            r.accept(response(c))
            r.step()
        self.assertEqual(r.s["spent"]["side_effects"], 1)
        self.assertEqual(len(r.s["calls"]["write"]["attempts"]), 1)

    def test_reused_id_with_different_arguments_is_protocol_error(self):
        r = self.runtime()
        self.send(r, "read_file", {"path": "order.py"}, "same")
        r.accept(response(call("same", "read_file", {"path": "test_order.py"})))
        r.step()
        self.assertIn("Protocol error", r.s["messages"][-1]["content"])
        self.assertEqual(r.s["calls"]["same"]["args"]["path"], "order.py")

    def test_crash_after_effect_requires_explicit_resolution(self):
        r = self.runtime()
        self.execute_task(r)
        r.crash_at = "after_execute"
        with self.assertRaises(InjectedCrash):
            self.send(r, "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "work"}, "w")
        r = self.reload(r)
        self.assertEqual(r.s["status"], "uncertain")
        self.assertEqual((self.workspace / "order.py").read_text(), FIXED_ORDER)
        self.assertIn("discount_percent / 100", r.uncertain_diff())
        with self.assertRaises(Conflict):
            r.rollback()
        with self.assertRaises(Conflict):
            r.resolve("retry")
        r.resolve("retry", adopt_changes=True)
        r.step()
        self.assertEqual(len(r.s["calls"]["w"]["attempts"]), 2)
        self.assertEqual(r.s["spent"]["side_effects"], 2)
        r.rollback()
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)

    def test_crash_after_budget_charge_does_not_refund(self):
        r = self.runtime()
        self.execute_task(r)
        r.crash_at = "after_charge"
        with self.assertRaises(InjectedCrash):
            self.send(r, "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "work"}, "w")
        charged = dict(r.s["spent"])
        r = self.reload(r)
        self.assertEqual(r.s["spent"], charged)
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)
        r.resolve("fail")
        self.assertFalse(r.s["calls"]["w"]["result"]["ok"])
        self.assertEqual(r.s["spent"]["side_effects"], 1)

    def test_uncertain_bash_cannot_be_retried(self):
        r = self.runtime()
        self.execute_task(r)
        r.crash_at = "after_execute"
        with self.assertRaises(InjectedCrash):
            self.send(r, "Bash", {"command": "pwd", "cwd": ".", "task_id": "work"}, "b")
        r = self.reload(r)
        with self.assertRaises(ValueError):
            r.resolve("retry")
        r.resolve("fail")
        self.assertEqual(r.s["status"], "running")

    def test_uncertain_abort_stays_blocked(self):
        r = self.runtime()
        self.execute_task(r)
        r.crash_at = "after_charge"
        with self.assertRaises(InjectedCrash):
            self.send(r, "write_file", {"path": "order.py", "content": "x", "task_id": "work"})
        r = self.reload(r)
        r.resolve("abort")
        self.assertEqual(r.s["status"], "failed")
        r = self.reload(r)
        self.assertEqual(r.s["status"], "failed")
        with self.assertRaises(Conflict):
            r.rollback()

    def test_model_call_budget(self):
        r = self.runtime(limits={"model_calls": 0})
        self.assertEqual(r.drive(DemoModel()), "budget_exhausted")
        self.assertEqual(r.s["spent"]["model_calls"], 0)

    def test_model_charge_survives_crash_and_network_failure(self):
        r = self.runtime(crash_at="after_model_charge")
        with self.assertRaises(InjectedCrash):
            r.drive(DemoModel())
        r = self.reload(r)
        self.assertEqual(r.s["spent"]["model_calls"], 1)
        r.drive(SequenceModel([]))
        self.assertEqual(r.s["status"], "model_error")
        self.assertEqual(r.s["spent"]["model_calls"], 2)

    def test_tool_batch_budget_all_or_nothing(self):
        r = self.runtime(limits={"tool_calls": 1})
        with self.assertRaises(BudgetError):
            r.accept(response(call("a", "read_file", {"path": "order.py"}),
                              call("b", "read_file", {"path": "test_order.py"})))
        self.assertEqual(r.s["calls"], {})
        self.assertEqual(r.s["spent"]["tool_calls"], 0)

    def test_side_effect_batch_budget_all_or_nothing(self):
        r = self.runtime(limits={"side_effects": 1})
        self.execute_task(r)
        with self.assertRaises(BudgetError):
            r.accept(response(call("a", "write_file", {"path": "a.py", "content": "a", "task_id": "work"}),
                              call("b", "write_file", {"path": "b.py", "content": "b", "task_id": "work"})))
        self.assertFalse((self.workspace / "a.py").exists())
        self.assertEqual(r.s["spent"]["side_effects"], 0)

    def test_result_byte_budget_exhaustion_and_truncation(self):
        r = self.runtime(limits={"result_bytes": 128})
        self.send(r, "read_file", {"path": "test_order.py"}, "read")
        result = json.loads(r.s["messages"][-1]["content"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(r.s["spent"]["result_bytes"], 128)
        with self.assertRaises(BudgetError):
            r.accept(response(call("next", "read_file", {"path": "order.py"})))

    def test_results_reserve_space_for_rest_of_batch(self):
        r = self.runtime(limits={"result_bytes": 256})
        r.accept(response(call("a", "read_file", {"path": "test_order.py"}),
                          call("b", "read_file", {"path": "order.py"})))
        r.step()
        r = self.reload(r)
        r.step()
        self.assertFalse(r.s["batch"])
        self.assertLessEqual(r.s["spent"]["result_bytes"], 256)

    def test_budgets_survive_plan_approval_and_resume(self):
        r = self.runtime()
        r.drive(DemoModel())
        spent = dict(r.s["spent"])
        r = self.reload(r)
        r.approve_plan(True)
        self.assertEqual(r.s["spent"], spent)
        r = self.reload(r)
        self.assertEqual(r.s["spent"], spent)

    def test_tasks_cannot_complete_on_model_claim(self):
        r = self.runtime()
        self.execute_task(r)
        self.assertFalse(json.loads(r.context()["content"])["ready_to_finish"])
        c = self.send(r, "finish_task", {"task_id": "work", "summary": "All done"})
        self.assertFalse(c["result"]["ok"])
        self.assertEqual(r.current_task()["status"], "running")
        r.accept({"content": "Done!"})
        self.assertEqual(r.s["status"], "running")

        self.send(r, "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "work"})
        self.assertTrue(json.loads(r.context()["content"])["ready_to_finish"])

    def test_task_dependency_and_wrong_task_id(self):
        r = self.runtime()
        r.drive(DemoModel())
        r.approve_plan(True)
        c = self.send(r, "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "fix"})
        self.assertFalse(c["result"]["ok"])
        self.assertEqual(r.current_task()["id"], "inspect")
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)

    def test_cycle_in_plan_rejected(self):
        r = self.runtime()
        plan = copy.deepcopy(PLAN)
        plan["tasks"][0]["depends_on"] = ["test"]
        self.assertFalse(self.send(r, "submit_plan", plan)["result"]["ok"])
        self.assertIsNone(r.s["plan"])

    def test_completed_tasks_not_rerun_on_resume_and_checklist(self):
        r = self.complete_demo()
        before = copy.deepcopy(r.s["calls"])
        r = self.reload(r)
        r.drive(DemoModel())
        self.assertEqual(r.s["calls"], before)
        with redirect_stdout(io.StringIO()) as output:
            checklist(r.s)
        self.assertEqual(output.getvalue().count("[x]"), 3)

    def test_diff_and_rollback_after_restart(self):
        r = self.complete_demo()
        self.assertIn("-    return sum(prices) - discount_percent", r.diff())
        r = self.reload(r)
        r.rollback()
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)
        self.assertEqual((self.workspace / "test_order.py").read_bytes(), self.tests)

    def test_rollback_preserves_unrelated_file(self):
        r = self.complete_demo()
        (self.workspace / "notes.txt").write_text("external")
        r.rollback()
        self.assertEqual((self.workspace / "notes.txt").read_text(), "external")

    def test_rollback_conflict_does_not_overwrite_any_file(self):
        r = self.runtime()
        self.execute_task(r)
        for name in ["a.py", "b.py"]:
            self.send(r, "write_file", {"path": name, "content": "agent", "task_id": "work"})
        (self.workspace / "b.py").write_text("external")
        r = self.reload(r)
        with self.assertRaises(Conflict):
            r.rollback()
        self.assertEqual((self.workspace / "a.py").read_text(), "agent")
        self.assertEqual((self.workspace / "b.py").read_text(), "external")

    def test_creation_and_deletion_are_journaled_and_rolled_back(self):
        r = self.runtime()
        self.execute_task(r)
        self.send(r, "write_file", {"path": "new.py", "content": "new", "task_id": "work"})
        self.send(r, "delete_file", {"path": "order.py", "task_id": "work"})
        r.decide_action(True)
        r.step()
        self.assertIn("new.py", r.diff())
        r.rollback()
        self.assertFalse((self.workspace / "new.py").exists())
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)

    def test_external_change_stops_next_side_effect(self):
        r = self.runtime()
        self.execute_task(r)
        (self.workspace / "notes.txt").write_text("external")
        c = self.send(r, "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": "work"})
        self.assertEqual(r.s["status"], "conflict")
        self.assertFalse(c["result"]["ok"])
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)

    def test_bash_changes_are_also_journaled(self):
        # Trusted fixture test creates a file; runtime still snapshots its effect.
        with (self.workspace / "test_order.py").open("a") as out:
            out.write('\nfrom pathlib import Path\nPath("created.txt").write_text("by tests")\n')
        r = self.runtime()
        self.execute_task(r)
        self.send(r, "Bash", {"command": "python3 -m unittest -v", "cwd": ".", "task_id": "work"})
        self.assertIn("created.txt", r.diff())
        r.rollback()
        self.assertFalse((self.workspace / "created.txt").exists())

    def test_rollback_crash_has_explicit_restart(self):
        r = self.complete_demo()
        r.crash_at = "rollback_after_file"
        with self.assertRaises(InjectedCrash):
            r.rollback()
        r = self.reload(r)
        self.assertEqual(r.s["status"], "rollback_uncertain")
        r.rollback()
        self.assertEqual(r.s["status"], "rolled_back")
        self.assertEqual((self.workspace / "order.py").read_bytes(), self.original)

    def test_bash_timeout_and_bounded_output(self):
        # Executor unit tests bypass policy solely to stress process supervision.
        result = run_bash("sleep 2", self.workspace, 0.1, 512)
        self.assertTrue(result["timed_out"])
        result = run_bash("yes output", self.workspace, 2, 128)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"].encode()) + len(result["stderr"].encode()), 128)
        self.assertFalse(result["ok"])

    def test_unicode_result_accounting_in_bytes(self):
        encoded = wire_result({"ok": True, "content": "Привет🙂" * 100}, 128)
        self.assertLessEqual(len(encoded.encode()), 128)
        self.assertTrue(json.loads(encoded)["truncated"])

    def test_tools_have_json_schemas(self):
        tools = tools_for_model()
        self.assertIn("Bash", [t["function"]["name"] for t in tools])
        for tool in tools:
            self.assertEqual(tool["function"]["parameters"]["type"], "object")
            self.assertFalse(tool["function"]["parameters"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
