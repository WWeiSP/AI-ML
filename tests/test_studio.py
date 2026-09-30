from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from studio import engine
from studio.app import create_app
from studio.runtime import AgentRuntime, tool_name


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("STUDIO_LLM", "off")


def make_client(runtime=None):
    app = create_app(":memory:", runtime=runtime, start_scheduler=False)
    return TestClient(app), app


def signup(client, username):
    r = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def linear_workflow(client, h, nodes, trigger=None, name="wf"):
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]
    r = client.post("/api/workflows", headers=h, json={
        "name": name, "trigger": trigger or {"type": "manual"}, "nodes": nodes, "edges": edges})
    assert r.status_code == 201, r.text
    return r.json()


TRIGGER = {"id": "t", "type": "trigger", "name": "start", "config": {}}


# ---------------------------------------------------------------------------
def test_auth_flow():
    client, _ = make_client()
    assert client.get("/api/me").status_code == 401
    h = signup(client, "alice")
    assert client.get("/api/me", headers=h).json()["username"] == "alice"
    assert client.post("/api/auth/register", json={"username": "alice", "password": "secret123"}).status_code == 409
    bad = client.post("/api/auth/login", json={"username": "alice", "password": "wrongpass"})
    assert bad.status_code == 401
    ok = client.post("/api/auth/login", json={"username": "alice", "password": "secret123"})
    assert ok.status_code == 200


def test_agent_build_publish_share_and_chat():
    client, _ = make_client()
    alice, bob, carol = signup(client, "alice"), signup(client, "bob"), signup(client, "carol")

    r = client.post("/api/agents", headers=alice, json={
        "name": "HR Helper", "instructions": "Answer HR questions.",
        "knowledge": [{"title": "Leave policy", "content": "Employees get 25 vacation days per year.\n\n"
                                                           "Sick leave is unlimited with a doctor's note."}],
        "topics": [{"name": "Greeting", "triggers": ["hello", "hi there"], "response": "Hi! Ask me about HR."}],
    })
    agent = r.json()
    aid = agent["id"]
    assert agent["status"] == "draft"

    # Topic match, then knowledge grounding (offline mode).
    r = client.post(f"/api/agents/{aid}/chat", headers=alice, json={"message": "Hello!"})
    assert r.json()["reply"]["source"] == "topic"
    cid = r.json()["conversation_id"]
    r = client.post(f"/api/agents/{aid}/chat", headers=alice,
                    json={"message": "How many vacation days do I get?", "conversation_id": cid})
    reply = r.json()["reply"]
    assert reply["source"] == "knowledge" and "25 vacation days" in reply["text"]
    assert r.json()["conversation_id"] == cid

    # Not visible to others while private draft.
    assert client.get(f"/api/agents/{aid}", headers=bob).status_code == 404

    # Publish to the org: everyone signed in can use it, but not edit it.
    r = client.post(f"/api/agents/{aid}/publish", headers=alice, json={"visibility": "org", "notes": "v1"})
    assert r.json()["version"] == 1 and r.json()["status"] == "published"
    assert [a["id"] for a in client.get("/api/catalog", headers=bob).json()] == [aid]
    assert client.get(f"/api/agents/{aid}", headers=bob).json()["role"] == "user"
    assert client.put(f"/api/agents/{aid}", headers=bob, json={"name": "x"}).status_code == 403
    assert client.post(f"/api/agents/{aid}/chat", headers=bob, json={"message": "hi", "mode": "draft"}).status_code == 403
    r = client.post(f"/api/agents/{aid}/chat", headers=bob, json={"message": "hello", "mode": "published"})
    assert r.status_code == 200

    # Draft edits don't leak into the published snapshot until republished.
    client.put(f"/api/agents/{aid}", headers=alice, json={"topics": [
        {"name": "Greeting", "triggers": ["hello"], "response": "Hey (v2)"}]})
    r = client.post(f"/api/agents/{aid}/chat", headers=bob, json={"message": "hello", "mode": "published"})
    assert r.json()["reply"]["text"] == "Hi! Ask me about HR."
    client.post(f"/api/agents/{aid}/publish", headers=alice, json={"visibility": "org"})
    r = client.post(f"/api/agents/{aid}/chat", headers=bob, json={"message": "hello", "mode": "published"})
    assert r.json()["reply"]["text"] == "Hey (v2)"
    assert [v["version"] for v in client.get(f"/api/agents/{aid}/versions", headers=alice).json()] == [2, 1]

    # Share as editor with carol.
    r = client.post(f"/api/agents/{aid}/shares", headers=alice, json={"username": "carol", "role": "editor"})
    assert r.json()[0]["role"] == "editor"
    assert client.put(f"/api/agents/{aid}", headers=carol, json={"description": "edited"}).status_code == 200
    assert client.post(f"/api/agents/{aid}/shares", headers=carol,
                       json={"username": "bob", "role": "editor"}).status_code == 403
    assert any(a["id"] == aid and a["role"] == "editor" for a in client.get("/api/agents", headers=carol).json())

    # Public link works anonymously only when visibility is public.
    slug = agent["slug"]
    assert client.get(f"/api/public/agents/{slug}").status_code == 404
    client.post(f"/api/agents/{aid}/publish", headers=alice, json={"visibility": "public"})
    assert client.get(f"/api/public/agents/{slug}").json()["name"] == "HR Helper"
    r = client.post(f"/api/public/agents/{slug}/chat", json={"message": "hello"})
    assert r.json()["reply"]["text"] == "Hey (v2)"

    # Unpublish removes access for consumers.
    client.post(f"/api/agents/{aid}/unpublish", headers=alice)
    assert client.get(f"/api/public/agents/{slug}").status_code == 404
    assert client.get(f"/api/agents/{aid}", headers=bob).status_code == 404


def test_publish_requires_content():
    client, _ = make_client()
    h = signup(client, "alice")
    aid = client.post("/api/agents", headers=h, json={"name": "Empty"}).json()["id"]
    assert client.post(f"/api/agents/{aid}/publish", headers=h, json={}).status_code == 400


def test_workflow_design_activate_and_webhook():
    client, _ = make_client()
    h = signup(client, "alice")
    wf = linear_workflow(client, h, [
        TRIGGER,
        {"id": "v", "type": "set_variable", "config": {"name": "who", "value": "{{input.name}}"}},
        {"id": "c", "type": "transform", "config": {"template": '{"greeting": "Hello {{vars.who}}"}'}},
        {"id": "r", "type": "respond", "config": {"value": "{{last}}"}},
    ], trigger={"type": "webhook"})
    wid = wf["id"]
    run = client.post(f"/api/workflows/{wid}/run", headers=h, json={"input": {"name": "Ada"}}).json()
    assert run["status"] == "succeeded"
    assert run["output"] == {"greeting": "Hello Ada"}
    assert [s["node_id"] for s in run["steps"]] == ["t", "v", "c", "r"]

    hook = f"/api/hooks/{wid}?key={wf['webhook_key']}"
    assert client.post(hook, json={"name": "Bob"}).status_code == 409  # not active yet
    assert client.post(f"/api/workflows/{wid}/activate", headers=h).json()["active"] == 1
    assert client.post(f"/api/hooks/{wid}?key=wrong", json={}).status_code == 404
    r = client.post(hook, json={"name": "Bob"})
    assert r.json()["output"] == {"greeting": "Hello Bob"}
    runs = client.get(f"/api/workflows/{wid}/runs", headers=h).json()
    assert [r["trigger_type"] for r in runs] == ["webhook", "manual"]


def test_activation_blocked_for_invalid_workflow():
    client, _ = make_client()
    h = signup(client, "alice")
    wf = client.post("/api/workflows", headers=h, json={
        "name": "bad", "nodes": [TRIGGER, {"id": "x", "type": "log", "config": {}}], "edges": []}).json()
    r = client.post(f"/api/workflows/{wf['id']}/activate", headers=h)
    assert r.status_code == 400 and "Not connected" in r.json()["detail"]
    v = client.post(f"/api/workflows/{wf['id']}/validate", headers=h).json()
    assert not v["valid"]


def test_condition_branches():
    wf = {
        "trigger": {"type": "manual"},
        "nodes": [TRIGGER,
                  {"id": "c", "type": "condition", "config": {"left": "{{input.n}}", "operator": "greater_than",
                                                              "right": "10"}},
                  {"id": "big", "type": "respond", "config": {"value": "big"}},
                  {"id": "small", "type": "respond", "config": {"value": "small"}}],
        "edges": [{"source": "t", "target": "c"},
                  {"source": "c", "target": "big", "branch": "true"},
                  {"source": "c", "target": "small", "branch": "false"}],
    }
    no_agent = lambda *_: ""
    assert engine.execute(wf, {"n": 42}, no_agent)["output"] == "big"
    assert engine.execute(wf, {"n": 3}, no_agent)["output"] == "small"


def test_http_node_and_private_address_guard(monkeypatch):
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url, headers, body))
        return {"status": 200, "body": {"ok": True}}

    wf = {"trigger": {"type": "manual"},
          "nodes": [TRIGGER, {"id": "h", "type": "http_request",
                              "config": {"method": "POST", "url": "https://example.com/{{input.path}}",
                                         "headers": '{"X-Test": "1"}', "body": '{"q": "{{input.q}}"}'}}],
          "edges": [{"source": "t", "target": "h"}]}
    res = engine.execute(wf, {"path": "items", "q": "hi"}, lambda *_: "", http=fake_http)
    assert res["status"] == "succeeded"
    assert calls == [("POST", "https://example.com/items", {"X-Test": "1"}, {"q": "hi"})]

    monkeypatch.delenv("STUDIO_ALLOW_PRIVATE_HTTP", raising=False)
    with pytest.raises(engine.WorkflowError):
        engine.http_request("GET", "http://127.0.0.1:9/", {}, None)


def test_workflow_calls_agent_and_sharing():
    client, _ = make_client()
    alice, bob = signup(client, "alice"), signup(client, "bob")
    aid = client.post("/api/agents", headers=alice, json={
        "name": "Echo", "topics": [{"name": "ping", "triggers": ["ping"], "response": "pong: {{message}}"}],
    }).json()["id"]
    wf = linear_workflow(client, alice, [
        TRIGGER, {"id": "a", "type": "agent", "config": {"agent_id": aid, "prompt": "ping {{input.x}}"}}])
    run = client.post(f"/api/workflows/{wf['id']}/run", headers=alice, json={"input": {"x": "7"}}).json()
    assert run["status"] == "succeeded" and run["output"] == "pong: ping 7"

    # Bob can't see it until shared; viewers can't run it or see the webhook key.
    assert client.get(f"/api/workflows/{wf['id']}", headers=bob).status_code == 404
    client.post(f"/api/workflows/{wf['id']}/shares", headers=alice, json={"username": "bob", "role": "viewer"})
    got = client.get(f"/api/workflows/{wf['id']}", headers=bob).json()
    assert got["role"] == "viewer" and "webhook_key" not in got
    assert client.post(f"/api/workflows/{wf['id']}/run", headers=bob, json={}).status_code == 403
    client.delete(f"/api/workflows/{wf['id']}/shares/bob", headers=alice)
    assert client.get(f"/api/workflows/{wf['id']}", headers=bob).status_code == 404


def test_topic_runs_workflow_and_agent_rejects_foreign_workflow():
    client, _ = make_client()
    alice, bob = signup(client, "alice"), signup(client, "bob")
    wf = linear_workflow(client, alice, [
        TRIGGER, {"id": "r", "type": "respond", "config": {"value": "ORDER-{{input.message}}"}}])
    client.post(f"/api/workflows/{wf['id']}/activate", headers=alice)
    aid = client.post("/api/agents", headers=alice, json={"name": "Orders", "topics": [
        {"name": "status", "triggers": ["order status"], "response": "Result: {{result}}",
         "workflow_id": wf["id"]}]}).json()["id"]
    r = client.post(f"/api/agents/{aid}/chat", headers=alice, json={"message": "order status 5"})
    assert r.json()["reply"]["text"] == "Result: ORDER-order status 5"

    r = client.post("/api/agents", headers=bob, json={"name": "Thief", "tools": [wf["id"]]})
    assert r.status_code == 400


def test_scheduler_runs_due_workflows():
    client, app = make_client()
    h = signup(client, "alice")
    wf = linear_workflow(client, h, [TRIGGER, {"id": "l", "type": "log", "config": {"message": "tick"}}],
                         trigger={"type": "schedule", "interval_seconds": 60})
    sched = app.state.scheduler
    assert sched.run_due() == []  # inactive
    client.post(f"/api/workflows/{wf['id']}/activate", headers=h)
    assert sched.run_due() == [wf["id"]]
    assert sched.run_due() == []  # not due yet
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    assert sched.run_due(later) == [wf["id"]]


def test_llm_tool_loop_invokes_workflow():
    """Claude calls an attached workflow as a tool; its output is fed back."""

    def block(**kw):
        return SimpleNamespace(**kw)

    class FakeMessages:
        def __init__(self):
            self.requests = []

        def create(self, **params):
            self.requests.append(params)
            if len(self.requests) == 1:
                tool = params["tools"][0]["name"]
                return SimpleNamespace(stop_reason="tool_use", content=[
                    block(type="tool_use", id="tu1", name=tool, input={"city": "Paris"})])
            return SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="It's sunny in Paris.")])

    fake = FakeMessages()
    runtime = AgentRuntime(client_factory=lambda: SimpleNamespace(beta=SimpleNamespace(messages=fake),
                                                                  messages=fake))
    client, _ = make_client(runtime)
    h = signup(client, "alice")
    wf = linear_workflow(client, h, [
        TRIGGER, {"id": "r", "type": "respond", "config": {"value": '{"forecast": "sunny in {{input.city}}"}'}}],
        name="Weather")
    client.post(f"/api/workflows/{wf['id']}/activate", headers=h)
    aid = client.post("/api/agents", headers=h, json={
        "name": "Weather bot", "instructions": "Report weather.", "tools": [wf["id"]]}).json()["id"]

    reply = client.post(f"/api/agents/{aid}/chat", headers=h, json={"message": "Weather in Paris?"}).json()["reply"]
    assert reply["source"] == "llm" and reply["text"] == "It's sunny in Paris."
    assert reply["tool_calls"][0]["status"] == "succeeded"
    first, second = fake.requests
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert first["tools"][0]["name"] == tool_name(wf["id"])
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "sunny in Paris" in tool_result["content"]
