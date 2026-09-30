# Agent Studio

A self-hosted, Copilot Studio-style app for **building, publishing and sharing AI agents** and for
**designing and activating workflows**. It runs on FastAPI and SQLite and ships a browser UI that needs no build step.
Generative answers come from Claude.

## Features

### Agents
- **Builder**: set the name, icon, description, model, instructions, greeting and suggested prompts.
- **Knowledge**: paste text or upload `.txt`/`.md` files. The agent grounds its answers on them.
- **Topics**: scripted conversation paths set off by trigger phrases. A topic can run a workflow first and
  put its result into the reply with `{{result}}`.
- **Actions**: attach activated workflows. Claude can call them as tools during a conversation.
- **Test chat** runs against the saved draft.
- **Publish** saves a versioned snapshot. Users only see published versions, so you can keep editing
  the draft. You can see version history and restore an earlier version into the draft.
- **Visibility**: `private` (only people you share with), `org` (every signed-in user; listed in the
  **Catalog**), or `public` (anyone with the link, with no sign-in needed; comes with an embed `<iframe>` snippet).
- **Sharing**: invite users as `viewer` or `editor`.
- **Activity**: read the transcripts of every conversation.

### Workflows
- **Visual designer**: drag blocks onto a canvas and connect ports. Condition blocks have true/false branches.
  Each block has a property panel.
- **Blocks**: Trigger, HTTP request, Ask an agent, Condition, Set variable, Compose (template/JSON), Delay,
  Log, Return output.
- **Templating** in any field: `{{input.x}}`, `{{last}}`, `{{vars.name}}`, `{{steps.<node_id>.field}}`.
- **Triggers**: `manual`, `webhook` (a secret URL whose key you can rotate), `schedule` (every N seconds),
  and `agent` (called as an agent action).
- **Validate → Activate**: a workflow must pass validation before it can be activated. Webhooks,
  schedules and agent calls only fire while it is active. Test runs work at any time.
- **Run history** records the input, output, timing and branch taken for each step. The last run is shown on the canvas.
- **Sharing**: invite users as viewer or editor, the same way as for agents.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # optional, see below
python -m studio --port 8000          # open http://localhost:8000
```

Create an account on the sign-in screen. Data is stored in `data/studio.db`; you can change this with
`--db` or `STUDIO_DB`.

### How an agent answers
1. **Topics**: if the message contains a topic's trigger phrase, the agent gives the authored response.
2. **Claude**: if the server has `ANTHROPIC_API_KEY` set, the agent's instructions and knowledge go to the
   model the agent is configured with. Its attached workflows are sent as tools. The default model is `claude-opus-5-5`;
   you can also pick Sonnet 5.5 or Haiku 4.5. Opus and Sonnet requests turn on server-side refusal fallbacks
   (`fallbacks: "default"`).
3. **Offline fallback**: with no API key, the agent answers from the best-matching knowledge paragraph.
   This lets you build and demo agents without spending on the API.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Turns on generative answers |
| `STUDIO_LLM` | — | Set to `off` to force offline mode |
| `STUDIO_DB` | `data/studio.db` | SQLite database path |
| `STUDIO_SCHEDULER` | `1` | Set to `0` to turn off the schedule runner |
| `STUDIO_ALLOW_PRIVATE_HTTP` | — | Set to `1` to let HTTP blocks call private or loopback addresses (blocked by default to prevent SSRF) |

## API overview

All endpoints are under `/api` and take `Authorization: Bearer <token>` (returned by `/api/auth/login`
or `/api/auth/register`).

| Area | Endpoints |
|---|---|
| Agents | `GET/POST /agents`, `GET/PUT/DELETE /agents/{id}`, `POST /agents/{id}/publish`, `POST /agents/{id}/unpublish`, `GET /agents/{id}/versions`, `POST /agents/{id}/versions/{v}/restore`, `POST /agents/{id}/chat`, `GET /agents/{id}/conversations` |
| Sharing | `GET/POST /agents/{id}/shares`, `DELETE /agents/{id}/shares/{username}` (the same routes exist under `/workflows`) |
| Catalog | `GET /catalog` |
| Public channel | `GET /public/agents/{slug}`, `POST /public/agents/{slug}/chat`, web page at `/a/{slug}` (`?embed=1` for iframes) |
| Workflows | `GET/POST /workflows`, `GET/PUT/DELETE /workflows/{id}`, `POST /workflows/{id}/validate`, `/activate`, `/deactivate`, `/run`, `/rotate-key`, `GET /workflows/{id}/runs` |
| Webhooks | `POST /hooks/{workflow_id}?key=<webhook_key>` (the JSON body becomes `{{input}}`) |

## Project layout

```
studio/
  app.py       FastAPI routes, permissions, publishing, sharing, scheduler
  runtime.py   Agent runtime: topics → Claude (with workflow tools) → offline fallback
  engine.py    Workflow node catalog, validation, templating and executor
  db.py        SQLite storage
  static/      Single-page web app (index.html, app.js, styles.css) and public chat page (chat.html)
tests/         pytest suite (API, engine, scheduler, Claude tool loop with a fake client)
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## Notes and limitations
- Sign-in uses username and password (PBKDF2) with bearer session tokens. Put the app behind SSO or a
  reverse proxy before exposing it beyond a trusted network.
- Workflows run synchronously inside the request that triggers them. Delays are capped at 30 seconds and a
  run stops after 200 steps.
- Knowledge retrieval is lexical, and the whole knowledge set goes into Claude's context. This works for FAQ-sized
  collections; very large corpora would need a vector index.
