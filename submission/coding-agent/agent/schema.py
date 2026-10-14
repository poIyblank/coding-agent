"""Small validator for the JSON Schema subset used by our tools (no dependencies)."""
import json


class ValidationError(ValueError):
    pass


def validate(value, schema, where="arguments"):
    kind = schema.get("type")
    matches = {
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: type(x) is int,
        "boolean": lambda x: type(x) is bool,
    }
    if kind not in matches or not matches[kind](value):
        raise ValidationError(f"{where}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{where}: not in {schema['enum']}")
    if kind == "object":
        props = schema["properties"]
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - props.keys()
        if missing or (extra and not schema.get("additionalProperties", True)):
            raise ValidationError(f"{where}: missing={sorted(missing)}, extra={sorted(extra)}")
        for name, item in value.items():
            if name in props:
                validate(item, props[name], f"{where}.{name}")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1000):
            raise ValidationError(f"{where}: wrong array length")
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{where}[{i}]")
    if kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 200000):
            raise ValidationError(f"{where}: wrong string length")
    if kind == "integer":
        if not schema.get("minimum", -10**18) <= value <= schema.get("maximum", 10**18):
            raise ValidationError(f"{where}: out of range")


def obj(props, required=None):
    return {"type": "object", "properties": props,
            "required": list(props) if required is None else required,
            "additionalProperties": False}


def string(maximum=2000):
    return {"type": "string", "maxLength": maximum}


def array(items, maximum=50, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


RESULT = obj({
    "kind": {"type": "string", "enum": ["read", "file", "absent", "tests"]},
    "paths": array(string()), "command": string(), "cwd": string(),
}, ["kind", "paths"])
TASK = obj({"id": string(64), "description": string(), "depends_on": array(string(64)),
            "tools": array(string(64), minimum=1), "result": RESULT})
PLAN = obj({"goal": string(), "tasks": array(TASK, maximum=20, minimum=1),
            "risks": array(string()), "assumptions": array(string()),
            "done_when": string()})

DEFINITIONS = {
    "list_files": ("List permitted files in a directory; paths are workspace-relative.",
                   obj({"path": string()}, ["path"])),
    "read_file": ("Read a UTF-8 file in the workspace.", obj({"path": string()})),
    "write_file": ("Atomically replace/create a UTF-8 file. Parent must exist. Tests are read-only.",
                   obj({"path": string(), "content": string(200000)})),
    "delete_file": ("Delete one file. Always requires explicit user approval.", obj({"path": string()})),
    "Bash": ("Run one exact command allowed by policy through Bash, with timeout/output cap.",
             obj({"command": string(), "cwd": string()})),
    "submit_plan": ("Submit structured tasks for user approval. Planning only.", PLAN),
    "finish_task": ("After completing the current task, call this to verify its result and advance to the next task.",
                    obj({"summary": string()})),
}
SIDE_EFFECTS = {"write_file", "delete_file", "Bash"}
READ_TOOLS = {"read_file", "list_files"}


def schema_for(name):
    if name not in DEFINITIONS:
        raise ValidationError(f"Unknown tool: {name}")
    schema = json.loads(json.dumps(DEFINITIONS[name][1]))
    if name == "submit_plan":
        schema["properties"]["tasks"]["items"]["properties"]["tools"]["items"]["enum"] = sorted(
            READ_TOOLS | SIDE_EFFECTS)
    else:
        schema["properties"]["task_id"] = string(64)
    return schema


def tools_for_model(*, phase=None, task_id=None, allowed_tools=None):
    tools = []
    for name, (desc, _) in DEFINITIONS.items():
        if phase == "plan" and name not in READ_TOOLS | {"submit_plan"}:
            continue
        if phase == "execute" and name == "submit_plan":
            continue
        if phase == "execute" and allowed_tools is not None and name not in set(allowed_tools) | {"finish_task"}:
            continue
        schema = schema_for(name)
        if phase == "plan" and name != "submit_plan":
            schema["properties"].pop("task_id", None)
        elif phase == "execute" and name != "submit_plan" and task_id:
            schema["properties"]["task_id"] = {"type": "string", "enum": [task_id]}
            schema["required"].append("task_id")
        tools.append({"type": "function", "function": {"name": name, "description": desc,
                      "parameters": schema}})
    return tools


def parse_arguments(name, raw):
    def reject_constant(value):
        raise ValidationError(f"Invalid JSON constant: {value}")
    try:
        args = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid JSON arguments: {exc}") from exc
    validate(args, schema_for(name))
    return args
