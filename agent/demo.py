"""Deterministic test double. Deliberately NOT presented as a real LLM."""
import json

FIXED_ORDER = '''def calculate_total(prices: list[float], discount_percent: float) -> float:
    if not 0 <= discount_percent <= 100:
        raise ValueError("discount_percent must be between 0 and 100")
    return sum(prices) * (1 - discount_percent / 100)
'''
PLAN = {
    "goal": "Исправить процентную скидку и проверить результат, не меняя тесты.",
    "tasks": [
        {"id": "inspect", "description": "Прочитать код и тесты", "depends_on": [],
         "tools": ["list_files", "read_file"],
         "result": {"kind": "read", "paths": ["order.py", "test_order.py"]}},
        {"id": "fix", "description": "Исправить формулу и валидацию скидки", "depends_on": ["inspect"],
         "tools": ["write_file", "read_file"], "result": {"kind": "file", "paths": ["order.py"]}},
        {"id": "test", "description": "Запустить unittest через Bash", "depends_on": ["fix"],
         "tools": ["Bash", "read_file"],
         "result": {"kind": "tests", "paths": [], "command": "python3 -m unittest -v", "cwd": "."}},
    ],
    "risks": ["Нельзя менять test_order.py; Bash запускает Python с правами пользователя."],
    "assumptions": ["Скидка — процент от суммы цен; допустим диапазон от 0 до 100 включительно."],
    "done_when": "Все задачи подтверждены runtime, исходные тесты прошли через Bash.",
}


def call(ident, name, args):
    return {"id": ident, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def response(*calls):
    return {"content": None, "tool_calls": list(calls)}


class DemoModel:
    def complete(self, messages):
        context = json.loads(messages[-1]["content"])
        known = {c["id"] for m in messages for c in m.get("tool_calls", [])}
        if context["phase"] == "plan":
            if "p-list" not in known:
                return response(call("p-list", "list_files", {"path": "."}))
            if "p-order" not in known:
                return response(call("p-order", "read_file", {"path": "order.py"}),
                                call("p-tests", "read_file", {"path": "test_order.py"}))
            # A rejected plan can be re-proposed under a new id.
            return response(call(f"plan-{len(known)}", "submit_plan", PLAN))
        task = context["current_task"]
        if task is None:
            return {"content": "Исправлены процентная скидка и проверка диапазона. Тесты прошли через Bash; test_order.py не изменён."}
        ident = task["id"]
        if ident == "inspect" and "inspect-order" not in known:
            return response(call("inspect-order", "read_file", {"path": "order.py", "task_id": ident}),
                            call("inspect-tests", "read_file", {"path": "test_order.py", "task_id": ident}))
        if ident == "fix" and "fix-write" not in known:
            return response(call("fix-write", "write_file", {"path": "order.py", "content": FIXED_ORDER, "task_id": ident}))
        if ident == "test" and "test-bash" not in known:
            return response(call("test-bash", "Bash", {"command": "python3 -m unittest -v", "cwd": ".", "task_id": ident}))
        # Demo does not invent an alternate repair after a denied/failed action.
        last_tool = next((m for m in reversed(messages) if m["role"] == "tool"), None)
        if last_tool and not json.loads(last_tool["content"]).get("ok"):
            raise RuntimeError("Demo action failed/denied; use API mode for adaptive recovery")
        return response(call(f"finish-{ident}", "finish_task", {"task_id": ident, "summary": "Результат готов к проверке."}))
