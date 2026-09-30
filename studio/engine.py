"""Workflow engine: node catalog, validation and execution.

A workflow is a directed graph. ``nodes`` are ``{id, type, name, config, x, y}``
and ``edges`` are ``{source, target, branch?}``. Execution starts at the single
``trigger`` node and follows edges; ``condition`` nodes only follow the edge
whose ``branch`` matches their result ("true"/"false").

String config values support ``{{ path }}`` templates resolved against the run
context: ``input``, ``vars``, ``steps.<node_id>`` and ``last`` (previous output).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

MAX_STEPS = 200
MAX_DELAY_SECONDS = 30
HTTP_TIMEOUT = 15

NODE_TYPES: list[dict[str, Any]] = [
    {"type": "trigger", "label": "Trigger", "icon": "⚡", "category": "Start",
     "description": "Where the workflow starts. Receives the run input.", "fields": []},
    {"type": "http_request", "label": "HTTP request", "icon": "🌐", "category": "Actions",
     "description": "Call an external API. Output: {status, body}.",
     "fields": [
         {"key": "method", "label": "Method", "kind": "select",
          "options": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
         {"key": "url", "label": "URL", "kind": "text", "default": "https://"},
         {"key": "headers", "label": "Headers (JSON)", "kind": "textarea", "default": "{}"},
         {"key": "body", "label": "Body", "kind": "textarea", "default": ""},
     ]},
    {"type": "agent", "label": "Ask an agent", "icon": "🤖", "category": "AI",
     "description": "Send a prompt to one of your agents. Output: the agent's reply text.",
     "fields": [
         {"key": "agent_id", "label": "Agent", "kind": "agent"},
         {"key": "prompt", "label": "Prompt", "kind": "textarea", "default": "{{input.message}}"},
     ]},
    {"type": "condition", "label": "Condition", "icon": "🔀", "category": "Logic",
     "description": "Branch on a comparison. Connect the true and false outputs.",
     "fields": [
         {"key": "left", "label": "Value", "kind": "text", "default": "{{last}}"},
         {"key": "operator", "label": "Operator", "kind": "select",
          "options": ["equals", "not_equals", "contains", "not_contains", "greater_than",
                      "less_than", "is_empty", "is_not_empty"], "default": "equals"},
         {"key": "right", "label": "Compare to", "kind": "text", "default": ""},
     ]},
    {"type": "set_variable", "label": "Set variable", "icon": "📝", "category": "Data",
     "description": "Store a value in vars.<name>.",
     "fields": [
         {"key": "name", "label": "Variable name", "kind": "text", "default": "result"},
         {"key": "value", "label": "Value", "kind": "textarea", "default": "{{last}}"},
     ]},
    {"type": "transform", "label": "Compose", "icon": "🧩", "category": "Data",
     "description": "Build text or JSON from a template. JSON is parsed when valid.",
     "fields": [{"key": "template", "label": "Template", "kind": "textarea", "default": ""}]},
    {"type": "delay", "label": "Delay", "icon": "⏱️", "category": "Logic",
     "description": f"Wait up to {MAX_DELAY_SECONDS} seconds.",
     "fields": [{"key": "seconds", "label": "Seconds", "kind": "number", "default": 1}]},
    {"type": "log", "label": "Log message", "icon": "📣", "category": "Actions",
     "description": "Write a message to the run log.",
     "fields": [{"key": "message", "label": "Message", "kind": "textarea", "default": "{{last}}"}]},
    {"type": "respond", "label": "Return output", "icon": "🏁", "category": "Actions",
     "description": "Set the workflow's output and stop.",
     "fields": [{"key": "value", "label": "Output", "kind": "textarea", "default": "{{last}}"}]},
]
NODE_TYPE_NAMES = {n["type"] for n in NODE_TYPES}

TRIGGER_TYPES = ["manual", "webhook", "schedule", "agent"]

_TEMPLATE = re.compile(r"\{\{\s*([\w.\-\[\]]+)\s*\}\}")


class WorkflowError(Exception):
    pass


# ---------------------------------------------------------------------------
# templating
# ---------------------------------------------------------------------------
def lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in re.split(r"\.|\[(\d+)\]", path):
        if part in (None, ""):
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Render templates. A string that is exactly one template keeps the raw type."""
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    whole = _TEMPLATE.fullmatch(value.strip())
    if whole:
        return lookup(ctx, whole.group(1))

    def sub(m: re.Match) -> str:
        v = lookup(ctx, m.group(1))
        if v is None:
            return ""
        return json.dumps(v) if isinstance(v, (dict, list)) else str(v)

    return _TEMPLATE.sub(sub, value)


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[":
            try:
                return json.loads(s)
            except ValueError:
                pass
    return value


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate(workflow: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    ids = [n.get("id") for n in nodes]
    by_id = {n.get("id"): n for n in nodes}
    if len(ids) != len(set(ids)):
        errors.append("Node ids must be unique.")
    triggers = [n for n in nodes if n.get("type") == "trigger"]
    if len(triggers) != 1:
        errors.append("A workflow needs exactly one Trigger node.")
    for n in nodes:
        if n.get("type") not in NODE_TYPE_NAMES:
            errors.append(f"Unknown node type '{n.get('type')}'.")
    for e in edges:
        if e.get("source") not in by_id or e.get("target") not in by_id:
            errors.append("An edge points to a missing node.")
        elif by_id[e["target"]].get("type") == "trigger":
            errors.append("Nothing can connect into the Trigger node.")
    for n in nodes:
        label = n.get("name") or n.get("type")
        cfg = n.get("config") or {}
        if n.get("type") == "condition":
            branches = {e.get("branch") for e in edges if e.get("source") == n.get("id")}
            if not branches & {"true", "false"}:
                errors.append(f"Condition '{label}' has no true/false branch connected.")
        if n.get("type") == "http_request" and str(cfg.get("url", "")).strip() in ("", "http://", "https://"):
            errors.append(f"HTTP request '{label}' needs a URL.")
        if n.get("type") == "agent" and not cfg.get("agent_id"):
            errors.append(f"Agent step '{label}' needs an agent selected.")
    trig = workflow.get("trigger") or {}
    if trig.get("type") not in TRIGGER_TYPES:
        errors.append("Choose a trigger type.")
    if trig.get("type") == "schedule":
        try:
            if int(trig.get("interval_seconds") or 0) < 10:
                errors.append("Schedule interval must be at least 10 seconds.")
        except (TypeError, ValueError):
            errors.append("Schedule interval must be a number.")
    if triggers and not errors:
        reachable = _reachable(triggers[0]["id"], edges)
        orphans = [by_id[i].get("name") or i for i in ids if i not in reachable]
        if orphans:
            errors.append("Not connected to the trigger: " + ", ".join(orphans))
    return errors


def _reachable(start: str, edges: list[dict[str, Any]]) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        cur = stack.pop()
        for e in edges:
            if e.get("source") == cur and e.get("target") not in seen:
                seen.add(e["target"])
                stack.append(e["target"])
    return seen


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def _check_url_allowed(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WorkflowError(f"Unsupported URL: {url!r}")
    if os.environ.get("STUDIO_ALLOW_PRIVATE_HTTP") == "1":
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise WorkflowError(f"Cannot resolve host {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise WorkflowError(
                f"Requests to private address {ip} are blocked "
                "(set STUDIO_ALLOW_PRIVATE_HTTP=1 to allow)."
            )


def http_request(method: str, url: str, headers: dict[str, str], body: Any) -> dict[str, Any]:
    _check_url_allowed(url)
    data = None
    if body not in (None, "") and method not in {"GET", "DELETE"}:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status, raw = resp.status, resp.read(1_000_000)
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read(1_000_000)
    text = raw.decode("utf-8", errors="replace")
    return {"status": status, "body": _maybe_json(text)}


def _compare(left: Any, op: str, right: Any) -> bool:
    def num(v):
        return float(v)

    if op == "is_empty":
        return left in (None, "", [], {})
    if op == "is_not_empty":
        return left not in (None, "", [], {})
    if op in {"greater_than", "less_than"}:
        try:
            return num(left) > num(right) if op == "greater_than" else num(left) < num(right)
        except (TypeError, ValueError):
            return False
    ls = left if isinstance(left, str) else json.dumps(left) if isinstance(left, (dict, list)) else str(left)
    rs = str(right)
    if op == "equals":
        return ls == rs
    if op == "not_equals":
        return ls != rs
    if op == "contains":
        return rs.lower() in ls.lower()
    if op == "not_contains":
        return rs.lower() not in ls.lower()
    raise WorkflowError(f"Unknown operator {op}")


AgentCaller = Callable[[str, str], str]


def execute(workflow: dict[str, Any], run_input: dict[str, Any], ask_agent: AgentCaller,
            sleep: Callable[[float], None] = time.sleep,
            http: Callable[..., dict[str, Any]] = http_request) -> dict[str, Any]:
    """Run a workflow synchronously. Returns {status, output, steps, error}."""
    errors = validate(workflow)
    if errors:
        return {"status": "failed", "output": None, "steps": [], "error": "; ".join(errors)}

    nodes = {n["id"]: n for n in workflow["nodes"]}
    edges = workflow.get("edges") or []
    trigger = next(n for n in workflow["nodes"] if n["type"] == "trigger")
    ctx: dict[str, Any] = {"input": run_input, "vars": {}, "steps": {}, "last": run_input}
    steps: list[dict[str, Any]] = []
    output: Any = None
    queue = [trigger["id"]]
    count = 0

    while queue:
        node_id = queue.pop(0)
        count += 1
        if count > MAX_STEPS:
            return {"status": "failed", "output": output, "steps": steps,
                    "error": f"Stopped after {MAX_STEPS} steps (loop?)"}
        node = nodes[node_id]
        cfg = node.get("config") or {}
        started = time.time()
        step = {"node_id": node_id, "name": node.get("name") or node["type"], "type": node["type"]}
        branch = None
        stop = False
        try:
            t = node["type"]
            if t == "trigger":
                result = run_input
            elif t == "http_request":
                headers = _maybe_json(render(cfg.get("headers") or "{}", ctx))
                if not isinstance(headers, dict):
                    raise WorkflowError("Headers must be a JSON object.")
                result = http(str(cfg.get("method") or "GET").upper(), str(render(cfg.get("url", ""), ctx)),
                              {str(k): str(v) for k, v in headers.items()},
                              _maybe_json(render(cfg.get("body", ""), ctx)))
            elif t == "agent":
                result = ask_agent(cfg.get("agent_id", ""), str(render(cfg.get("prompt", ""), ctx) or ""))
            elif t == "condition":
                result = _compare(render(cfg.get("left"), ctx), cfg.get("operator", "equals"),
                                  render(cfg.get("right", ""), ctx))
                branch = "true" if result else "false"
            elif t == "set_variable":
                result = _maybe_json(render(cfg.get("value"), ctx))
                ctx["vars"][cfg.get("name") or "value"] = result
            elif t == "transform":
                result = _maybe_json(render(cfg.get("template", ""), ctx))
            elif t == "delay":
                secs = min(max(float(render(cfg.get("seconds", 0), ctx) or 0), 0), MAX_DELAY_SECONDS)
                sleep(secs)
                result = ctx["last"]
            elif t == "log":
                result = render(cfg.get("message", ""), ctx)
            elif t == "respond":
                result = _maybe_json(render(cfg.get("value"), ctx))
                output = result
                stop = True
            else:
                raise WorkflowError(f"Unknown node type {t}")
        except Exception as exc:
            step.update(status="failed", error=str(exc), ms=int((time.time() - started) * 1000))
            steps.append(step)
            return {"status": "failed", "output": output, "steps": steps,
                    "error": f"{step['name']}: {exc}"}

        step.update(status="succeeded", output=result, ms=int((time.time() - started) * 1000))
        if branch:
            step["branch"] = branch
        steps.append(step)
        ctx["steps"][node_id] = result
        ctx["last"] = result
        if node["type"] != "trigger":
            output = result
        if stop:
            break
        for e in edges:
            if e["source"] != node_id:
                continue
            if branch is not None and e.get("branch") not in (branch, None, ""):
                continue
            queue.append(e["target"])

    return {"status": "succeeded", "output": output, "steps": steps, "error": None}
