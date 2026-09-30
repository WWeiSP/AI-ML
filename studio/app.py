"""Agent Studio HTTP API + static web app."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine
from .db import Database, new_id, now
from .runtime import DEFAULT_MODEL, MODELS, AgentRuntime, llm_enabled

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
MAX_AGENT_DEPTH = 3

Role = Literal["viewer", "editor"]
Visibility = Literal["private", "org", "public"]


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)


class KnowledgeDoc(BaseModel):
    title: str = Field(max_length=200)
    content: str = Field(max_length=100_000)


class Topic(BaseModel):
    name: str = Field(max_length=100)
    triggers: list[str] = []
    response: str = ""
    workflow_id: str | None = None
    enabled: bool = True


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    icon: str = Field(default="🤖", max_length=8)
    instructions: str = ""
    model: str = DEFAULT_MODEL
    greeting: str = ""
    starters: list[str] = []
    knowledge: list[KnowledgeDoc] = []
    topics: list[Topic] = []
    tools: list[str] = []


class AgentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    icon: str | None = Field(default=None, max_length=8)
    instructions: str | None = None
    model: str | None = None
    greeting: str | None = None
    starters: list[str] | None = None
    knowledge: list[KnowledgeDoc] | None = None
    topics: list[Topic] | None = None
    tools: list[str] | None = None


class PublishIn(BaseModel):
    visibility: Visibility = "org"
    notes: str = ""


class ShareIn(BaseModel):
    username: str
    role: Role = "viewer"


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    conversation_id: str | None = None
    mode: Literal["draft", "published"] = "draft"


class TriggerIn(BaseModel):
    type: Literal["manual", "webhook", "schedule", "agent"] = "manual"
    interval_seconds: int | None = None


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    trigger: TriggerIn = TriggerIn()
    nodes: list[dict[str, Any]] | None = None
    edges: list[dict[str, Any]] = []
    template: str | None = None


class WorkflowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    trigger: TriggerIn | None = None
    nodes: list[dict[str, Any]] | None = None
    edges: list[dict[str, Any]] | None = None


class RunIn(BaseModel):
    input: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "agent"
    return f"{base}-{secrets.token_hex(3)}"


def default_nodes() -> list[dict[str, Any]]:
    return [{"id": "trigger", "type": "trigger", "name": "When triggered", "config": {}, "x": 80, "y": 160}]


TEMPLATES: dict[str, dict[str, Any]] = {
    "ask_agent": {
        "nodes": [
            {"id": "trigger", "type": "trigger", "name": "When triggered", "config": {}, "x": 60, "y": 160},
            {"id": "ask", "type": "agent", "name": "Ask agent", "x": 320, "y": 160,
             "config": {"agent_id": "", "prompt": "{{input.message}}"}},
            {"id": "out", "type": "respond", "name": "Return answer", "x": 580, "y": 160,
             "config": {"value": "{{last}}"}},
        ],
        "edges": [{"source": "trigger", "target": "ask"}, {"source": "ask", "target": "out"}],
    },
    "health_check": {
        "nodes": [
            {"id": "trigger", "type": "trigger", "name": "Every minute", "config": {}, "x": 40, "y": 180},
            {"id": "call", "type": "http_request", "name": "Ping API", "x": 270, "y": 180,
             "config": {"method": "GET", "url": "https://api.github.com/zen", "headers": "{}", "body": ""}},
            {"id": "check", "type": "condition", "name": "Status is 200?", "x": 500, "y": 180,
             "config": {"left": "{{last.status}}", "operator": "equals", "right": "200"}},
            {"id": "ok", "type": "log", "name": "Healthy", "x": 740, "y": 90,
             "config": {"message": "API healthy: {{steps.call.body}}"}},
            {"id": "bad", "type": "log", "name": "Alert", "x": 740, "y": 270,
             "config": {"message": "API returned {{steps.call.status}}"}},
        ],
        "edges": [
            {"source": "trigger", "target": "call"}, {"source": "call", "target": "check"},
            {"source": "check", "target": "ok", "branch": "true"},
            {"source": "check", "target": "bad", "branch": "false"},
        ],
    },
}


class Studio:
    """Service layer shared by the HTTP routes, the scheduler and agent tools."""

    def __init__(self, db: Database, runtime: AgentRuntime):
        self.db = db
        self.runtime = runtime

    # -- users -------------------------------------------------------------------
    def user_by_token(self, token: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT u.id, u.username, u.display_name FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?", [token])

    def issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self.db.insert("sessions", {"token": token, "user_id": user_id, "created_at": now()})
        return token

    # -- permissions -------------------------------------------------------------
    def role_for(self, resource_type: str, resource: dict[str, Any], user: dict[str, Any] | None) -> str | None:
        """owner > editor > viewer > user (can use a published agent) > None."""
        if user and resource["owner_id"] == user["id"]:
            return "owner"
        if user:
            share = self.db.one("SELECT role FROM shares WHERE resource_type=? AND resource_id=? AND user_id=?",
                                [resource_type, resource["id"], user["id"]])
            if share:
                return share["role"]
        if resource_type == "agent" and resource.get("status") == "published":
            if resource.get("visibility") == "public" or (user and resource.get("visibility") == "org"):
                return "user"
        return None

    def require(self, resource_type: str, resource_id: str, user: dict[str, Any] | None,
                allowed: set[str]) -> tuple[dict[str, Any], str]:
        table = "agents" if resource_type == "agent" else "workflows"
        res = self.db.one(f"SELECT * FROM {table} WHERE id = ?", [resource_id])
        role = self.role_for(resource_type, res, user) if res else None
        if res is None or role is None:
            raise HTTPException(404, f"{resource_type.title()} not found")
        if role not in allowed:
            raise HTTPException(403, "You don't have permission to do that")
        return res, role

    # -- shares ------------------------------------------------------------------
    def list_shares(self, resource_type: str, resource_id: str) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT u.username, u.display_name, s.role, s.created_at FROM shares s "
            "JOIN users u ON u.id = s.user_id WHERE s.resource_type=? AND s.resource_id=? ORDER BY u.username",
            [resource_type, resource_id])

    def share(self, resource_type: str, resource: dict[str, Any], body: ShareIn) -> list[dict[str, Any]]:
        target = self.db.one("SELECT id FROM users WHERE username = ?", [body.username])
        if not target:
            raise HTTPException(404, f"User '{body.username}' not found")
        if target["id"] == resource["owner_id"]:
            raise HTTPException(400, "The owner already has full access")
        self.db.execute(
            "INSERT INTO shares (resource_type, resource_id, user_id, role, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(resource_type, resource_id, user_id) DO UPDATE SET role = excluded.role",
            [resource_type, resource["id"], target["id"], body.role, now()])
        return self.list_shares(resource_type, resource["id"])

    def unshare(self, resource_type: str, resource_id: str, username: str) -> list[dict[str, Any]]:
        self.db.execute(
            "DELETE FROM shares WHERE resource_type=? AND resource_id=? "
            "AND user_id = (SELECT id FROM users WHERE username = ?)", [resource_type, resource_id, username])
        return self.list_shares(resource_type, resource_id)

    # -- agents ------------------------------------------------------------------
    @staticmethod
    def agent_config(agent: dict[str, Any]) -> dict[str, Any]:
        keys = ["name", "description", "icon", "instructions", "model", "greeting", "starters",
                "knowledge", "topics", "tools"]
        return {k: agent[k] for k in keys}

    def public_agent(self, agent: dict[str, Any], role: str | None = None) -> dict[str, Any]:
        out = dict(agent)
        owner = self.db.one("SELECT username, display_name FROM users WHERE id = ?", [agent["owner_id"]])
        out["owner"] = owner
        if role:
            out["role"] = role
        return out

    def resolve_tools(self, tool_ids: list[str]) -> list[dict[str, Any]]:
        if not tool_ids:
            return []
        marks = ",".join("?" for _ in tool_ids)
        return self.db.all(f"SELECT id, name, description FROM workflows WHERE id IN ({marks}) AND active = 1",
                           tool_ids)

    def chat(self, agent: dict[str, Any], config: dict[str, Any], message: str, user_id: str | None,
             channel: str, conversation_id: str | None, depth: int = 0) -> dict[str, Any]:
        convo = None
        if conversation_id:
            convo = self.db.one("SELECT * FROM conversations WHERE id = ? AND agent_id = ?",
                                [conversation_id, agent["id"]])
            if convo and convo["user_id"] != user_id:
                convo = None
        history = convo["messages"] if convo else []
        run_config = {**config, "tools_meta": self.resolve_tools(config.get("tools") or [])}

        def run_workflow(workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            allowed = set(config.get("tools") or []) | {t.get("workflow_id") for t in config.get("topics") or []}
            wf = self.db.one("SELECT * FROM workflows WHERE id = ?", [workflow_id])
            if wf is None or workflow_id not in allowed:
                return {"status": "failed", "error": "Workflow not available to this agent"}
            if not wf["active"]:
                return {"status": "failed", "error": f"Workflow '{wf['name']}' is not activated"}
            return self.run_workflow(wf, payload, "agent", depth=depth + 1)

        reply = self.runtime.respond(run_config, history, message, run_workflow)
        stamp = now()
        history = [*history, {"role": "user", "content": message, "at": stamp},
                   {"role": "assistant", "content": reply.text, "at": stamp, "source": reply.source,
                    "topic": reply.topic, "tool_calls": reply.tool_calls}]
        if convo:
            self.db.update("conversations", convo["id"], {"messages": history, "updated_at": stamp})
            cid = convo["id"]
        else:
            cid = new_id()
            self.db.insert("conversations", {"id": cid, "agent_id": agent["id"], "user_id": user_id,
                                             "channel": channel, "messages": history,
                                             "created_at": stamp, "updated_at": stamp})
        return {"conversation_id": cid, "reply": reply.as_dict()}

    # -- workflows ---------------------------------------------------------------
    def run_workflow(self, wf: dict[str, Any], payload: dict[str, Any], trigger_type: str,
                     depth: int = 0) -> dict[str, Any]:
        run_id = new_id()
        started = now()
        self.db.insert("runs", {"id": run_id, "workflow_id": wf["id"], "status": "running",
                                "trigger_type": trigger_type, "input": payload, "steps": [],
                                "started_at": started})

        def ask_agent(agent_id: str, prompt: str) -> str:
            if depth >= MAX_AGENT_DEPTH:
                raise engine.WorkflowError("Agent/workflow nesting is too deep")
            agent = self.db.one("SELECT * FROM agents WHERE id = ?", [agent_id])
            if agent is None:
                raise engine.WorkflowError("Agent not found")
            owner = {"id": wf["owner_id"]}
            if self.role_for("agent", agent, owner) is None:
                raise engine.WorkflowError("Workflow owner cannot use this agent")
            config = agent["snapshot"] or self.agent_config(agent)
            result = self.chat(agent, config, prompt, wf["owner_id"], f"workflow:{wf['id']}", None, depth)
            return result["reply"]["text"]

        try:
            result = engine.execute(wf, payload, ask_agent)
        except Exception as exc:  # engine errors are already captured; this is a safety net
            log.exception("workflow crashed")
            result = {"status": "failed", "output": None, "steps": [], "error": str(exc)}
        finished = now()
        self.db.update("runs", run_id, {"status": result["status"], "output": result["output"],
                                        "steps": result["steps"], "error": result["error"],
                                        "finished_at": finished})
        self.db.update("workflows", wf["id"], {"last_run_at": finished})
        return self.db.one("SELECT * FROM runs WHERE id = ?", [run_id])


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------
class Scheduler(threading.Thread):
    def __init__(self, studio: Studio, tick: float = 1.0):
        super().__init__(daemon=True, name="workflow-scheduler")
        self.studio = studio
        self.tick = tick
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run_due(self, at: datetime | None = None) -> list[str]:
        at = at or datetime.now(timezone.utc)
        fired = []
        for wf in self.studio.db.all("SELECT * FROM workflows WHERE active = 1"):
            trig = wf["trigger"]
            if trig.get("type") != "schedule":
                continue
            interval = max(int(trig.get("interval_seconds") or 60), 10)
            last = wf["last_run_at"]
            if last and (at - datetime.fromisoformat(last)).total_seconds() < interval:
                continue
            self.studio.run_workflow(wf, {"scheduled_at": at.isoformat(timespec="seconds")}, "schedule")
            fired.append(wf["id"])
        return fired

    def run(self) -> None:
        while not self._halt.wait(self.tick):
            try:
                self.run_due()
            except Exception:
                log.exception("scheduler tick failed")


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------
def create_app(db_path: str | None = None, runtime: AgentRuntime | None = None,
               start_scheduler: bool | None = None) -> FastAPI:
    db = Database(db_path or os.environ.get("STUDIO_DB", "data/studio.db"))
    studio = Studio(db, runtime or AgentRuntime())
    scheduler = Scheduler(studio)
    if start_scheduler is None:
        start_scheduler = os.environ.get("STUDIO_SCHEDULER", "1") != "0"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Agent Studio", lifespan=lifespan)
    app.state.studio = studio
    app.state.scheduler = scheduler

    # -- auth dependencies -------------------------------------------------------
    def optional_user(authorization: str | None = Header(default=None)) -> dict[str, Any] | None:
        if authorization and authorization.lower().startswith("bearer "):
            return studio.user_by_token(authorization[7:].strip())
        return None

    def current_user(user: dict[str, Any] | None = Depends(optional_user)) -> dict[str, Any]:
        if user is None:
            raise HTTPException(401, "Sign in required")
        return user

    # -- meta --------------------------------------------------------------------
    @app.get("/api/meta")
    def meta():
        return {"models": MODELS, "default_model": DEFAULT_MODEL, "llm_enabled": llm_enabled(),
                "node_types": engine.NODE_TYPES, "trigger_types": engine.TRIGGER_TYPES,
                "templates": list(TEMPLATES)}

    # -- auth --------------------------------------------------------------------
    @app.post("/api/auth/register")
    def register(body: Credentials):
        if db.one("SELECT id FROM users WHERE username = ?", [body.username]):
            raise HTTPException(409, "Username already taken")
        uid = new_id()
        db.insert("users", {"id": uid, "username": body.username,
                            "display_name": body.display_name or body.username,
                            "password_hash": hash_password(body.password), "created_at": now()})
        return {"token": studio.issue_token(uid),
                "user": {"id": uid, "username": body.username, "display_name": body.display_name or body.username}}

    @app.post("/api/auth/login")
    def login(body: Credentials):
        row = db.one("SELECT * FROM users WHERE username = ?", [body.username])
        if not row or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        return {"token": studio.issue_token(row["id"]),
                "user": {"id": row["id"], "username": row["username"], "display_name": row["display_name"]}}

    @app.post("/api/auth/logout")
    def logout(authorization: str | None = Header(default=None)):
        if authorization and authorization.lower().startswith("bearer "):
            db.execute("DELETE FROM sessions WHERE token = ?", [authorization[7:].strip()])
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/users")
    def users(q: str = "", user=Depends(current_user)):
        return db.all("SELECT username, display_name FROM users WHERE username LIKE ? AND id != ? "
                      "ORDER BY username LIMIT 20", [f"%{q}%", user["id"]])

    @app.get("/api/stats")
    def stats(user=Depends(current_user)):
        uid = user["id"]
        return {
            "agents": db.one("SELECT COUNT(*) n FROM agents WHERE owner_id = ?", [uid])["n"],
            "published": db.one("SELECT COUNT(*) n FROM agents WHERE owner_id = ? AND status='published'",
                                [uid])["n"],
            "workflows": db.one("SELECT COUNT(*) n FROM workflows WHERE owner_id = ?", [uid])["n"],
            "active": db.one("SELECT COUNT(*) n FROM workflows WHERE owner_id = ? AND active = 1", [uid])["n"],
            "runs": db.one("SELECT COUNT(*) n FROM runs r JOIN workflows w ON w.id = r.workflow_id "
                           "WHERE w.owner_id = ?", [uid])["n"],
            "conversations": db.one("SELECT COUNT(*) n FROM conversations c JOIN agents a ON a.id = c.agent_id "
                                    "WHERE a.owner_id = ?", [uid])["n"],
            "recent_runs": db.all(
                "SELECT r.id, r.status, r.trigger_type, r.started_at, w.name workflow, w.id workflow_id "
                "FROM runs r JOIN workflows w ON w.id = r.workflow_id WHERE w.owner_id = ? "
                "ORDER BY r.started_at DESC LIMIT 8", [uid]),
        }

    # -- agents ------------------------------------------------------------------
    @app.get("/api/agents")
    def list_agents(user=Depends(current_user)):
        rows = db.all(
            "SELECT a.*, CASE WHEN a.owner_id = ? THEN 'owner' ELSE s.role END role FROM agents a "
            "LEFT JOIN shares s ON s.resource_type='agent' AND s.resource_id=a.id AND s.user_id = ? "
            "WHERE a.owner_id = ? OR s.user_id IS NOT NULL ORDER BY a.updated_at DESC",
            [user["id"], user["id"], user["id"]])
        return [studio.public_agent(r) for r in rows]

    @app.get("/api/catalog")
    def catalog(user=Depends(current_user)):
        rows = db.all("SELECT * FROM agents WHERE status='published' AND visibility IN ('org','public') "
                      "ORDER BY published_at DESC")
        return [studio.public_agent({k: v for k, v in r.items() if k != "snapshot"} | {
            "name": r["snapshot"]["name"], "description": r["snapshot"]["description"],
            "icon": r["snapshot"]["icon"]}) for r in rows]

    def check_agent_refs(data: dict[str, Any], user: dict[str, Any]) -> None:
        if "model" in data and data["model"] not in {m["id"] for m in MODELS}:
            raise HTTPException(400, f"Unknown model '{data['model']}'")
        refs = set(data.get("tools") or []) | {t.get("workflow_id") for t in data.get("topics") or []
                                                if t.get("workflow_id")}
        for wid in refs:
            wf = db.one("SELECT * FROM workflows WHERE id = ?", [wid])
            if not wf or studio.role_for("workflow", wf, user) is None:
                raise HTTPException(400, f"Workflow '{wid}' not found")

    @app.post("/api/agents", status_code=201)
    def create_agent(body: AgentIn, user=Depends(current_user)):
        aid, stamp = new_id(), now()
        data = body.model_dump()
        check_agent_refs(data, user)
        db.insert("agents", {"id": aid, "owner_id": user["id"], **data, "slug": slugify(body.name),
                             "created_at": stamp, "updated_at": stamp})
        return studio.public_agent(db.one("SELECT * FROM agents WHERE id = ?", [aid]), "owner")

    @app.get("/api/agents/{agent_id}")
    def get_agent(agent_id: str, user=Depends(current_user)):
        agent, role = studio.require("agent", agent_id, user, {"owner", "editor", "viewer", "user"})
        if role == "user":  # consumers only see the published configuration
            agent = {**agent, **agent["snapshot"], "knowledge": [], "topics": []}
        return studio.public_agent(agent, role)

    @app.put("/api/agents/{agent_id}")
    def update_agent(agent_id: str, body: AgentPatch, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner", "editor"})
        changes = body.model_dump(exclude_none=True)
        check_agent_refs(changes, user)
        changes["updated_at"] = now()
        db.update("agents", agent_id, changes)
        agent, role = studio.require("agent", agent_id, user, {"owner", "editor"})
        return studio.public_agent(agent, role)

    @app.delete("/api/agents/{agent_id}")
    def delete_agent(agent_id: str, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner"})
        db.execute("DELETE FROM shares WHERE resource_type='agent' AND resource_id = ?", [agent_id])
        db.execute("DELETE FROM agents WHERE id = ?", [agent_id])
        return {"ok": True}

    @app.post("/api/agents/{agent_id}/publish")
    def publish_agent(agent_id: str, body: PublishIn, user=Depends(current_user)):
        agent, _ = studio.require("agent", agent_id, user, {"owner", "editor"})
        if not agent["instructions"].strip() and not agent["knowledge"] and not agent["topics"]:
            raise HTTPException(400, "Add instructions, knowledge or topics before publishing")
        snapshot = studio.agent_config(agent)
        version, stamp = agent["version"] + 1, now()
        db.insert("agent_versions", {"id": new_id(), "agent_id": agent_id, "version": version,
                                     "snapshot": snapshot, "notes": body.notes,
                                     "published_by": user["username"], "created_at": stamp})
        db.update("agents", agent_id, {"status": "published", "visibility": body.visibility,
                                       "version": version, "snapshot": snapshot, "published_at": stamp})
        agent, role = studio.require("agent", agent_id, user, {"owner", "editor"})
        return studio.public_agent(agent, role)

    @app.post("/api/agents/{agent_id}/unpublish")
    def unpublish_agent(agent_id: str, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner", "editor"})
        db.update("agents", agent_id, {"status": "draft"})
        agent, role = studio.require("agent", agent_id, user, {"owner", "editor"})
        return studio.public_agent(agent, role)

    @app.get("/api/agents/{agent_id}/versions")
    def agent_versions(agent_id: str, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner", "editor", "viewer"})
        return db.all("SELECT version, notes, published_by, created_at FROM agent_versions "
                      "WHERE agent_id = ? ORDER BY version DESC", [agent_id])

    @app.post("/api/agents/{agent_id}/versions/{version}/restore")
    def restore_version(agent_id: str, version: int, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner", "editor"})
        row = db.one("SELECT snapshot FROM agent_versions WHERE agent_id = ? AND version = ?", [agent_id, version])
        if not row:
            raise HTTPException(404, "Version not found")
        db.update("agents", agent_id, {**row["snapshot"], "updated_at": now()})
        agent, role = studio.require("agent", agent_id, user, {"owner", "editor"})
        return studio.public_agent(agent, role)

    @app.get("/api/agents/{agent_id}/shares")
    def agent_shares(agent_id: str, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner", "editor", "viewer"})
        return studio.list_shares("agent", agent_id)

    @app.post("/api/agents/{agent_id}/shares")
    def share_agent(agent_id: str, body: ShareIn, user=Depends(current_user)):
        agent, _ = studio.require("agent", agent_id, user, {"owner"})
        return studio.share("agent", agent, body)

    @app.delete("/api/agents/{agent_id}/shares/{username}")
    def unshare_agent(agent_id: str, username: str, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner"})
        return studio.unshare("agent", agent_id, username)

    @app.post("/api/agents/{agent_id}/chat")
    def chat_agent(agent_id: str, body: ChatIn, user=Depends(current_user)):
        allowed = {"owner", "editor"} if body.mode == "draft" else {"owner", "editor", "viewer", "user"}
        agent, _ = studio.require("agent", agent_id, user, allowed)
        if body.mode == "published":
            if agent["status"] != "published":
                raise HTTPException(400, "This agent is not published")
            config = agent["snapshot"]
        else:
            config = studio.agent_config(agent)
        return studio.chat(agent, config, body.message, user["id"], body.mode, body.conversation_id)

    @app.get("/api/agents/{agent_id}/conversations")
    def conversations(agent_id: str, user=Depends(current_user)):
        studio.require("agent", agent_id, user, {"owner", "editor"})
        return db.all("SELECT c.id, c.channel, c.messages, c.updated_at, u.username FROM conversations c "
                      "LEFT JOIN users u ON u.id = c.user_id WHERE c.agent_id = ? "
                      "ORDER BY c.updated_at DESC LIMIT 50", [agent_id])

    # -- public (anonymous) agent channel ------------------------------------------
    def published_by_slug(slug: str, user) -> dict[str, Any]:
        agent = db.one("SELECT * FROM agents WHERE slug = ?", [slug])
        if not agent or agent["status"] != "published" or studio.role_for("agent", agent, user) is None:
            raise HTTPException(404, "Agent not found")
        return agent

    @app.get("/api/public/agents/{slug}")
    def public_agent(slug: str, user=Depends(optional_user)):
        agent = published_by_slug(slug, user)
        snap = agent["snapshot"]
        return {"slug": slug, "name": snap["name"], "description": snap["description"], "icon": snap["icon"],
                "greeting": snap["greeting"], "starters": snap["starters"], "version": agent["version"]}

    @app.post("/api/public/agents/{slug}/chat")
    def public_chat(slug: str, body: ChatIn, user=Depends(optional_user)):
        agent = published_by_slug(slug, user)
        return studio.chat(agent, agent["snapshot"], body.message, user["id"] if user else None,
                           "public", body.conversation_id)

    # -- workflows -----------------------------------------------------------------
    @app.get("/api/workflows")
    def list_workflows(user=Depends(current_user)):
        rows = db.all(
            "SELECT w.*, CASE WHEN w.owner_id = ? THEN 'owner' ELSE s.role END role FROM workflows w "
            "LEFT JOIN shares s ON s.resource_type='workflow' AND s.resource_id=w.id AND s.user_id = ? "
            "WHERE w.owner_id = ? OR s.user_id IS NOT NULL ORDER BY w.updated_at DESC",
            [user["id"], user["id"], user["id"]])
        for r in rows:
            r.pop("webhook_key", None)
        return rows

    def workflow_out(wf: dict[str, Any], role: str) -> dict[str, Any]:
        out = {**wf, "role": role}
        if role not in {"owner", "editor"}:
            out.pop("webhook_key", None)
        return out

    @app.post("/api/workflows", status_code=201)
    def create_workflow(body: WorkflowIn, user=Depends(current_user)):
        if body.template and body.template not in TEMPLATES:
            raise HTTPException(400, "Unknown template")
        tpl = TEMPLATES.get(body.template or "", {})
        wid, stamp = new_id(), now()
        trigger = body.trigger.model_dump()
        if body.template == "health_check" and trigger["type"] == "manual":
            trigger = {"type": "schedule", "interval_seconds": 60}
        db.insert("workflows", {
            "id": wid, "owner_id": user["id"], "name": body.name, "description": body.description,
            "trigger": trigger, "nodes": body.nodes or tpl.get("nodes") or default_nodes(),
            "edges": body.edges or tpl.get("edges") or [], "active": 0,
            "webhook_key": secrets.token_urlsafe(18), "created_at": stamp, "updated_at": stamp})
        return workflow_out(db.one("SELECT * FROM workflows WHERE id = ?", [wid]), "owner")

    @app.get("/api/workflows/{workflow_id}")
    def get_workflow(workflow_id: str, user=Depends(current_user)):
        wf, role = studio.require("workflow", workflow_id, user, {"owner", "editor", "viewer"})
        return workflow_out(wf, role)

    @app.put("/api/workflows/{workflow_id}")
    def update_workflow(workflow_id: str, body: WorkflowPatch, user=Depends(current_user)):
        wf, role = studio.require("workflow", workflow_id, user, {"owner", "editor"})
        changes = body.model_dump(exclude_none=True)
        candidate = {**wf, **changes}
        if wf["active"] and engine.validate(candidate):
            raise HTTPException(400, "Deactivate the workflow before saving an invalid version: "
                                + "; ".join(engine.validate(candidate)))
        changes["updated_at"] = now()
        db.update("workflows", workflow_id, changes)
        wf, role = studio.require("workflow", workflow_id, user, {"owner", "editor"})
        return workflow_out(wf, role)

    @app.delete("/api/workflows/{workflow_id}")
    def delete_workflow(workflow_id: str, user=Depends(current_user)):
        studio.require("workflow", workflow_id, user, {"owner"})
        db.execute("DELETE FROM shares WHERE resource_type='workflow' AND resource_id = ?", [workflow_id])
        db.execute("DELETE FROM workflows WHERE id = ?", [workflow_id])
        return {"ok": True}

    @app.post("/api/workflows/{workflow_id}/validate")
    def validate_workflow(workflow_id: str, user=Depends(current_user)):
        wf, _ = studio.require("workflow", workflow_id, user, {"owner", "editor", "viewer"})
        errors = engine.validate(wf)
        return {"valid": not errors, "errors": errors}

    @app.post("/api/workflows/{workflow_id}/activate")
    def activate_workflow(workflow_id: str, user=Depends(current_user)):
        wf, role = studio.require("workflow", workflow_id, user, {"owner", "editor"})
        errors = engine.validate(wf)
        if errors:
            raise HTTPException(400, "Fix these issues first: " + "; ".join(errors))
        db.update("workflows", workflow_id, {"active": 1, "updated_at": now()})
        wf, role = studio.require("workflow", workflow_id, user, {"owner", "editor"})
        return workflow_out(wf, role)

    @app.post("/api/workflows/{workflow_id}/deactivate")
    def deactivate_workflow(workflow_id: str, user=Depends(current_user)):
        studio.require("workflow", workflow_id, user, {"owner", "editor"})
        db.update("workflows", workflow_id, {"active": 0, "updated_at": now()})
        wf, role = studio.require("workflow", workflow_id, user, {"owner", "editor"})
        return workflow_out(wf, role)

    @app.post("/api/workflows/{workflow_id}/run")
    def run_workflow(workflow_id: str, body: RunIn, user=Depends(current_user)):
        wf, _ = studio.require("workflow", workflow_id, user, {"owner", "editor"})
        return studio.run_workflow(wf, body.input, "manual")

    @app.post("/api/workflows/{workflow_id}/rotate-key")
    def rotate_key(workflow_id: str, user=Depends(current_user)):
        studio.require("workflow", workflow_id, user, {"owner"})
        db.update("workflows", workflow_id, {"webhook_key": secrets.token_urlsafe(18)})
        wf, role = studio.require("workflow", workflow_id, user, {"owner"})
        return workflow_out(wf, role)

    @app.get("/api/workflows/{workflow_id}/runs")
    def workflow_runs(workflow_id: str, user=Depends(current_user)):
        studio.require("workflow", workflow_id, user, {"owner", "editor", "viewer"})
        return db.all("SELECT * FROM runs WHERE workflow_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 50",
                      [workflow_id])

    @app.get("/api/workflows/{workflow_id}/shares")
    def workflow_shares(workflow_id: str, user=Depends(current_user)):
        studio.require("workflow", workflow_id, user, {"owner", "editor", "viewer"})
        return studio.list_shares("workflow", workflow_id)

    @app.post("/api/workflows/{workflow_id}/shares")
    def share_workflow(workflow_id: str, body: ShareIn, user=Depends(current_user)):
        wf, _ = studio.require("workflow", workflow_id, user, {"owner"})
        return studio.share("workflow", wf, body)

    @app.delete("/api/workflows/{workflow_id}/shares/{username}")
    def unshare_workflow(workflow_id: str, username: str, user=Depends(current_user)):
        studio.require("workflow", workflow_id, user, {"owner"})
        return studio.unshare("workflow", workflow_id, username)

    @app.post("/api/hooks/{workflow_id}")
    async def webhook(workflow_id: str, request: Request, key: str = ""):
        wf = db.one("SELECT * FROM workflows WHERE id = ?", [workflow_id])
        if not wf or not hmac.compare_digest(key, wf["webhook_key"]):
            raise HTTPException(404, "Not found")
        if not wf["active"] or wf["trigger"].get("type") != "webhook":
            raise HTTPException(409, "Workflow is not active for webhooks")
        try:
            payload = await request.json()
        except ValueError:
            payload = {"body": (await request.body()).decode("utf-8", "replace")}
        if not isinstance(payload, dict):
            payload = {"body": payload}
        run = await anyio.to_thread.run_sync(lambda: studio.run_workflow(wf, payload, "webhook"))
        return {"run_id": run["id"], "status": run["status"], "output": run["output"], "error": run["error"]}

    # -- static web app ------------------------------------------------------------
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/a/{slug}")
    def public_chat_page(slug: str):
        return FileResponse(STATIC_DIR / "chat.html")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
