"""Real CLI processes and an HTTP stub: no credentials or external network needed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from agent.demo import DemoModel
from agent.model import APIModel
from agent.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        shutil.copytree(ROOT / "workspace", self.home / "workspace")
        shutil.copy(ROOT / "policy.json", self.home / "policy.json")
        self.original = (self.home / "workspace/order.py").read_bytes()
        self.original_tests = (self.home / "workspace/test_order.py").read_bytes()
        self.env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-m", "agent", *args], cwd=self.home,
                                env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_cli_safe_restart_approvals_diff_rollback(self):
        self.assertIn("wait_plan", self.cli("start", "--model", "demo", "--mode", "safe", "--run-id", "cli"))
        self.assertIn("wait_action", self.cli("approve-plan", "cli"))
        before = json.loads((self.home / ".runs/cli.json").read_text())
        self.assertEqual(before["tasks"][0]["status"], "done")
        self.assertIn("[x] inspect", self.cli("tasks", "cli"))
        self.assertIn("wait_action", self.cli("approve", "cli"))
        self.assertIn("completed", self.cli("approve", "cli"))
        after = json.loads((self.home / ".runs/cli.json").read_text())
        self.assertEqual(before["calls"]["inspect-order"], after["calls"]["inspect-order"])
        self.assertEqual(before["calls"]["inspect-tests"], after["calls"]["inspect-tests"])
        self.assertEqual((self.home / "workspace/test_order.py").read_bytes(), self.original_tests)
        self.assertIn("discount_percent / 100", self.cli("diff", "cli"))
        self.assertIn("rolled_back", self.cli("rollback", "cli"))
        self.assertEqual((self.home / "workspace/order.py").read_bytes(), self.original)

    def test_cli_crash_99_and_manual_recovery(self):
        self.cli("start", "--model", "demo", "--mode", "safe", "--run-id", "crash")
        self.cli("approve-plan", "crash")
        self.cli("approve", "crash", "--crash-at", "after_execute", code=99)
        self.assertIn("uncertain", self.cli("resume", "crash"))
        self.assertIn("discount_percent / 100", self.cli("uncertain-diff", "crash"))
        self.cli("rollback", "crash", code=1)
        self.assertIn("wait_action", self.cli("resolve", "crash", "retry", "--adopt-changes"))
        self.assertIn("completed", self.cli("approve", "crash"))
        state = json.loads((self.home / ".runs/crash.json").read_text())
        self.assertEqual(len(state["calls"]["fix-write"]["attempts"]), 2)
        self.assertEqual(state["spent"]["side_effects"], 3)
        self.cli("rollback", "crash")
        self.assertEqual((self.home / "workspace/order.py").read_bytes(), self.original)

    def test_actual_http_adapter_full_tool_call_protocol(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "payload": payload})
                message = DemoModel().complete(payload["messages"])
                body = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = {"OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1/",
                   "OPENAI_API_KEY": "test-only", "OPENAI_MODEL": "http-test-double"}
            with patch.dict(os.environ, env):
                r = Runtime.create(self.home / "workspace", self.home / "policy.json",
                                   self.home / ".runs", "Fix order", mode="auto")
                self.assertEqual(r.drive(APIModel()), "wait_plan")
                r.approve_plan(True)
                self.assertEqual(r.drive(APIModel()), "completed")
            self.assertEqual(len(requests), 10)
            self.assertTrue(all(x["path"] == "/v1/chat/completions" for x in requests))
            self.assertNotIn("Bash", [t["function"]["name"] for t in requests[0]["payload"]["tools"]])
            self.assertTrue(any(t["function"]["name"] == "Bash"
                                for x in requests for t in x["payload"].get("tools", [])))
            messages = requests[-1]["payload"]["messages"]
            pending = set()
            for message in messages:
                if message["role"] == "assistant":
                    self.assertFalse(pending)
                    pending = {c["id"] for c in message.get("tool_calls", [])}
                elif message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], pending)
                    pending.remove(message["tool_call_id"])
            self.assertFalse(pending)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_concurrent_workspace_lock(self):
        r = Runtime.create(self.home / "workspace", self.home / "policy.json",
                           self.home / ".runs", "x", run_id="lock")
        original_state = (self.home / ".runs/lock.json").read_bytes()
        with r.store.lock():
            self.assertIn("Another runtime", self.cli("resume", "lock", code=1))
        self.assertEqual((self.home / ".runs/lock.json").read_bytes(), original_state)


if __name__ == "__main__":
    unittest.main()
