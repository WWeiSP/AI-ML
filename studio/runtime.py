"""Agent runtime: decides how an agent answers a message.

Resolution order (mirrors how Copilot Studio agents behave):

1. **Topics** – authored conversation paths. If a trigger phrase matches, the
   topic's response is returned (optionally after running a workflow).
2. **Generative answers** – when Claude is configured, the agent's
   instructions, knowledge and attached workflows (exposed as tools) are sent
   to the Claude API.
3. **Offline fallback** – without an API key, answers are grounded on the
   best-matching knowledge snippet so the studio is still usable for demos.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
MODELS = [
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5 (most capable)"},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5 (balanced)"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (fastest)"},
]
# Models that accept `output_config.effort` and server-side refusal fallbacks.
_EFFORT_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
MAX_TOOL_ROUNDS = 6
MAX_HISTORY_TURNS = 20

# (workflow_id, input) -> run record dict
WorkflowRunner = Callable[[str, dict[str, Any]], dict[str, Any]]

_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "your", "with", "this", "that",
    "have", "from", "what", "how", "can", "does", "was", "were", "will", "about",
    "which", "when", "where", "who", "why", "our", "has", "had", "its", "into",
}


@dataclass
class Reply:
    text: str
    source: str  # topic | llm | knowledge | fallback
    topic: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "source": self.source, "topic": self.topic,
                "tool_calls": self.tool_calls}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def match_topic(topics: list[dict[str, Any]], message: str) -> dict[str, Any] | None:
    """Return the first enabled topic with a trigger phrase contained in the message."""
    msg = f" {_normalize(message)} "
    for topic in topics:
        if not topic.get("enabled", True):
            continue
        for phrase in topic.get("triggers", []):
            norm = _normalize(phrase)
            if norm and f" {norm} " in msg:
                return topic
    return None


def search_knowledge(knowledge: list[dict[str, Any]], query: str) -> tuple[dict[str, Any], str] | None:
    """Very small lexical retriever: best (document, paragraph) by token overlap."""
    q = _tokens(query)
    if not q:
        return None
    best, best_score = None, 0
    for doc in knowledge:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", doc.get("content", "")) if p.strip()]
        for para in paragraphs or [doc.get("content", "")]:
            score = len(q & _tokens(para)) * 2 + len(q & _tokens(doc.get("title", "")))
            if score > best_score:
                best, best_score = (doc, para), score
    return best


def tool_name(workflow_id: str) -> str:
    return f"workflow_{workflow_id}"


def llm_enabled() -> bool:
    if os.environ.get("STUDIO_LLM", "").lower() in {"off", "0", "false"}:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def build_system_prompt(agent: dict[str, Any]) -> str:
    parts = [f"You are \"{agent['name']}\", an assistant built in Agent Studio."]
    if agent.get("description"):
        parts.append(f"Purpose: {agent['description']}")
    if agent.get("instructions"):
        parts.append(f"<instructions>\n{agent['instructions']}\n</instructions>")
    knowledge = agent.get("knowledge") or []
    if knowledge:
        docs = "\n".join(
            f"<document title=\"{d.get('title', 'Untitled')}\">\n{d.get('content', '')}\n</document>"
            for d in knowledge
        )
        parts.append(
            "Ground your answers in the knowledge below when it is relevant. If the "
            "knowledge does not cover the question, say so rather than inventing facts.\n"
            f"<knowledge>\n{docs}\n</knowledge>"
        )
    if agent.get("tools_meta"):
        parts.append(
            "You can run the workflows exposed as tools. Use one when the user asks for "
            "the action it performs, then summarize its result for the user."
        )
    return "\n\n".join(parts)


class AgentRuntime:
    def __init__(self, client_factory: Callable[[], Any] | None = None):
        self._client_factory = client_factory
        self._client = None

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import anthropic
                self._client = anthropic.Anthropic()
        return self._client

    # -- public ---------------------------------------------------------------
    def respond(self, agent: dict[str, Any], history: list[dict[str, str]], message: str,
                run_workflow: WorkflowRunner) -> Reply:
        topic = match_topic(agent.get("topics") or [], message)
        if topic:
            return self._run_topic(topic, message, run_workflow)
        if self._client_factory is not None or llm_enabled():
            try:
                return self._run_llm(agent, history, message, run_workflow)
            except Exception as exc:  # surface API problems without killing the chat
                log.exception("LLM call failed")
                return Reply(f"⚠️ The model call failed: {exc}", "error")
        return self._offline(agent, message)

    # -- strategies -------------------------------------------------------------
    def _run_topic(self, topic: dict[str, Any], message: str, run_workflow: WorkflowRunner) -> Reply:
        text = topic.get("response") or ""
        calls = []
        if topic.get("workflow_id"):
            run = run_workflow(topic["workflow_id"], {"message": message, "topic": topic.get("name")})
            calls.append({"workflow_id": topic["workflow_id"], "run_id": run.get("id"),
                          "status": run.get("status")})
            result = run.get("output") if run.get("status") == "succeeded" else run.get("error")
            if isinstance(result, (dict, list)):
                result = json.dumps(result)
            text = text.replace("{{result}}", str(result if result is not None else ""))
        text = text.replace("{{message}}", message)
        return Reply(text or "(empty topic response)", "topic", topic=topic.get("name"), tool_calls=calls)

    def _offline(self, agent: dict[str, Any], message: str) -> Reply:
        hit = search_knowledge(agent.get("knowledge") or [], message)
        if hit:
            doc, para = hit
            return Reply(f"From **{doc.get('title', 'knowledge')}**:\n\n{para}", "knowledge")
        return Reply(
            "I couldn't find that in my knowledge sources. (Generative answers are off — "
            "set ANTHROPIC_API_KEY on the server to let this agent answer with Claude.)",
            "fallback",
        )

    def _run_llm(self, agent: dict[str, Any], history: list[dict[str, str]], message: str,
                 run_workflow: WorkflowRunner) -> Reply:
        client = self._get_client()
        model = agent.get("model") or DEFAULT_MODEL
        tools_meta = agent.get("tools_meta") or []
        tools = [
            {
                "name": tool_name(t["id"]),
                "description": (f"Run the '{t['name']}' workflow. {t.get('description', '')}").strip(),
                "input_schema": {
                    "type": "object",
                    "description": "Input fields passed to the workflow trigger as `input`.",
                    "additionalProperties": True,
                },
            }
            for t in tools_meta
        ]
        by_tool = {tool_name(t["id"]): t for t in tools_meta}

        messages: list[dict[str, Any]] = [
            {"role": m["role"], "content": m["content"]}
            for m in history[-MAX_HISTORY_TURNS * 2:] if m.get("content")
        ]
        messages.append({"role": "user", "content": message})

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": 16000,
            "system": build_system_prompt(agent),
            "messages": messages,
        }
        if tools:
            params["tools"] = tools
        if model in _EFFORT_MODELS:
            params["output_config"] = {"effort": "medium"}
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"
            create = client.beta.messages.create
        else:
            create = client.messages.create

        calls: list[dict[str, Any]] = []
        response = None
        for _ in range(MAX_TOOL_ROUNDS):
            response = create(**params)
            if response.stop_reason == "refusal":
                return Reply("I can't help with that request.", "llm", tool_calls=calls)
            if response.stop_reason != "tool_use":
                break
            # Pass the full assistant content (incl. thinking blocks) back unchanged.
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                meta = by_tool.get(block.name)
                if meta is None:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Unknown tool {block.name}", "is_error": True})
                    continue
                tool_input = block.input if isinstance(block.input, dict) else {}
                run = run_workflow(meta["id"], tool_input)
                calls.append({"workflow_id": meta["id"], "workflow": meta["name"],
                              "run_id": run.get("id"), "status": run.get("status")})
                ok = run.get("status") == "succeeded"
                payload = run.get("output") if ok else run.get("error")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(payload, default=str), "is_error": not ok})
            messages.append({"role": "user", "content": results})

        text = "".join(b.text for b in (response.content if response else []) if b.type == "text").strip()
        return Reply(text or "(no response)", "llm", tool_calls=calls)
