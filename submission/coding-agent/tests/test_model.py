import io
import json
import os
import unittest
from unittest.mock import patch

from agent.model import APIModel


class APISettingsTest(unittest.TestCase):
    def response(self):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "ready"}}]}).encode())

    def test_default_request_does_not_force_provider_reasoning(self):
        with patch.dict(os.environ, {}, clear=True), patch(
                "agent.model.urlopen", return_value=self.response()) as request:
            self.assertEqual(APIModel().complete([]), {"content": "ready", "tool_calls": []})
        args, kwargs = request.call_args
        self.assertEqual(kwargs["timeout"], 90)
        self.assertNotIn("reasoning_effort", json.loads(args[0].data))

    def test_local_inference_settings_reach_http_request(self):
        env = {"OPENAI_TIMEOUT_SECONDS": "600", "OPENAI_REASONING_EFFORT": "none"}
        with patch.dict(os.environ, env, clear=True), patch(
                "agent.model.urlopen", return_value=self.response()) as request:
            APIModel().complete([{"role": "user", "content": "Fix order"}])
        args, kwargs = request.call_args
        body = json.loads(args[0].data)
        self.assertEqual(kwargs["timeout"], 600)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertIn("Bash", [t["function"]["name"] for t in body["tools"]])
        self.assertEqual(body["max_tokens"], 2048)
        self.assertIn("/no_think", body["messages"][-1]["content"])

    def test_local_hints_do_not_modify_history_or_remote_requests(self):
        messages = [{"role": "user", "content": "Fix order"}]
        env = {"OPENAI_BASE_URL": "https://example.com/v1", "OPENAI_MODEL": "qwen3:4b",
               "OPENAI_REASONING_EFFORT": "none"}
        with patch.dict(os.environ, env, clear=True), patch(
                "agent.model.urlopen", return_value=self.response()) as request:
            APIModel().complete(messages)
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["messages"], messages)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(messages, [{"role": "user", "content": "Fix order"}])

    def test_truncated_response_is_not_treated_as_success(self):
        payload = {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        with patch.dict(os.environ, {}, clear=True), patch(
                "agent.model.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaises(ValueError):
                APIModel().complete([])

    def test_schemas_follow_runtime_phase_and_task(self):
        for phase, task in [("plan", None), ("execute", {"id": "inspect", "tools": ["read_file"]})]:
            with self.subTest(phase=phase), patch.dict(os.environ, {}, clear=True), patch(
                    "agent.model.urlopen", return_value=self.response()) as request:
                APIModel().complete([{"role": "system", "content": json.dumps(
                    {"phase": phase, "current_task": task})}])
                tools = json.loads(request.call_args.args[0].data)["tools"]
                names = {t["function"]["name"] for t in tools}
                if phase == "plan":
                    self.assertEqual(names, {"list_files", "read_file", "submit_plan"})
                    plan = next(t["function"] for t in tools if t["function"]["name"] == "submit_plan")
                    allowed = plan["parameters"]["properties"]["tasks"]["items"]["properties"]["tools"]["items"]["enum"]
                    self.assertEqual(set(allowed), {"list_files", "read_file", "write_file", "delete_file", "Bash"})
                    read = next(t["function"] for t in tools if t["function"]["name"] == "read_file")
                    self.assertNotIn("task_id", read["parameters"]["properties"])
                else:
                    self.assertEqual(names, {"read_file", "finish_task"})
                    for t in tools:
                        params = t["function"]["parameters"]
                        self.assertIn("task_id", params["required"])
                        self.assertEqual(params["properties"]["task_id"]["enum"], ["inspect"])

    def test_invalid_timeout_rejected_before_http_request(self):
        for value in ["0", "-1", "nan", "inf", "invalid"]:
            with self.subTest(value=value), patch.dict(
                    os.environ, {"OPENAI_TIMEOUT_SECONDS": value}, clear=True), patch(
                    "agent.model.urlopen") as request:
                with self.assertRaises(ValueError):
                    APIModel().complete([])
                request.assert_not_called()

    def test_verified_task_only_offers_completion_and_final_has_no_tools(self):
        for finished in [False, True]:
            context = {"phase": "execute", "current_task": {"id": "inspect", "tools": ["read_file"]},
                       "ready_to_finish": True, "all_tasks_done": finished}
            with self.subTest(finished=finished), patch.dict(os.environ, {}, clear=True), patch(
                    "agent.model.urlopen", return_value=self.response()) as request:
                APIModel().complete([{"role": "system", "content": json.dumps(context)}])
            body = json.loads(request.call_args.args[0].data)
            if finished:
                self.assertNotIn("tools", body)
                self.assertNotIn("tool_choice", body)
            else:
                self.assertEqual([t["function"]["name"] for t in body["tools"]], ["finish_task"])
