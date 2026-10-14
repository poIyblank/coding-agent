"""OpenAI-compatible Chat Completions over HTTP, not an agent framework."""
import json
import math
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .schema import tools_for_model


class APIModel:
    def complete(self, messages):
        base = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1").rstrip("/")
        key = os.environ.get("OPENAI_API_KEY", "ollama")
        model = os.environ.get("OPENAI_MODEL", "qwen3:4b")
        timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("OPENAI_TIMEOUT_SECONDS must be a positive finite number")
        url = urlparse(base)
        if url.scheme not in {"http", "https"} or url.username or url.password:
            raise ValueError("Invalid API URL")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote API requires HTTPS")
        effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip()
        local_qwen = url.hostname in {"localhost", "127.0.0.1", "::1"} and model.startswith("qwen3")
        request_messages = list(messages)
        context = {}
        if messages and messages[-1].get("role") == "system":
            try:
                context = json.loads(messages[-1]["content"])
            except (ValueError, KeyError, TypeError):
                pass
        if not isinstance(context, dict):
            context = {}
        task = context.get("current_task") or {}
        task_id = task.get("id") if isinstance(task, dict) else None
        allowed_tools = task.get("tools") if isinstance(task, dict) else None
        if context.get("ready_to_finish"):
            allowed_tools = []
        if local_qwen and effort == "none":
            hint = "/no_think\nUse function tools directly. Do not narrate hypothetical actions."
            if context.get("all_tasks_done"):
                hint += " All tasks are verified. Return a concise final summary."
            elif context.get("ready_to_finish"):
                hint += f" The task result is verified. Call finish_task now with task_id={task_id}."
            elif task_id:
                hint += f" Work on the current task only. Include task_id={task_id} in every call."
            if context:
                request_messages[-1] = {**request_messages[-1],
                                        "content": request_messages[-1]["content"] + "\n" + hint}
            else:
                request_messages.append({"role": "system", "content": hint})
        data = {"model": model, "messages": request_messages,
                "tools": tools_for_model(phase=context.get("phase"), task_id=task_id,
                                         allowed_tools=allowed_tools),
                "tool_choice": "auto", "stream": False}
        if context.get("all_tasks_done"):
            data.pop("tools")
            data.pop("tool_choice")
        if effort:
            data["reasoning_effort"] = effort
        raw_max_tokens = os.environ.get("OPENAI_MAX_TOKENS", "2048" if local_qwen else "")
        if raw_max_tokens:
            max_tokens = int(raw_max_tokens)
            if max_tokens <= 0:
                raise ValueError("OPENAI_MAX_TOKENS must be positive")
            data["max_tokens"] = max_tokens
        if local_qwen:
            data["temperature"] = 0
        body = json.dumps(data).encode()
        request = Request(base + "/chat/completions", data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key})
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("Model response too large")
        choice = json.loads(payload)["choices"][0]
        if choice.get("finish_reason") not in {None, "stop", "tool_calls"}:
            raise ValueError("Model response did not finish normally")
        message = choice["message"]
        return {"content": message.get("content"), "tool_calls": message.get("tool_calls") or []}
