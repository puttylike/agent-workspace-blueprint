# Agent Workspace Blueprint

Agent Workspace Blueprint is a read-only controller and server-rendered
operations dashboard for multi-agent project workspaces. It combines declared
project metadata with Git, task, agent-runtime, pull-request, and Markdown
knowledge signals without becoming a new source of truth.

The first release intentionally contains no project creation, task mutation,
Git write, pull-request mutation, file-write, or arbitrary-command API. Runtime
configuration belongs in a private operations workspace; this repository is
designed to remain safe to publish.

## Components

- `awc doctor` validates configuration and local prerequisites.
- `awc projects list` and `awc projects show <project-id>` aggregate project
  state from read-only sources.
- `awc agents list` reports declared agent presence and recent activity.
- `awc wiki index` builds a local SQLite FTS5 search cache for Markdown.
- `awc serve --host 127.0.0.1 --port 3001` serves the read-only Agent Ops UI.

Copy `config/agent-ops.example.yaml` into a private workspace, replace its
placeholders there, and pass that path through `--config` or `AWC_CONFIG`.

## Development

```text
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/pytest
```

The service accepts only `127.0.0.1:3001`. Deployment and private network
exposure are deliberately outside this release.

