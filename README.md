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
- `awc projects plan --type venture` emits a plan-only venture lifecycle proposal;
  it never creates a workspace, repository, Lead, Beads state, or local runner.
- `awc projects apply --plan-file <plan.json> --approval-sha256 <sha256>` validates
  an approval-bound venture plan and emits an ordered action manifest. Despite
  its name, this public command is validation-only: it has no execute option and
  never writes files or invokes Git, GitHub, Beads, Hermes, a runner, or a
  network mutation. Execution belongs to a separately gated private adapter.
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

## Approval-bound apply contract

Apply plans use schema `agent-workspace-project-apply/v1`. Canonical bytes are
UTF-8 JSON objects with recursively sorted object keys, compact `,` and `:`
separators, and no trailing newline. NaN and Infinity are rejected. The exact
lowercase SHA-256 of those bytes is the approval identity: any plan change
changes the digest and invalidates the approval.

The contract accepts only non-executable, private venture plans, validates the
workspace/repository/Lead/Beads/lifecycle policies, requires the five separate
external-action approval gates, keeps live trading prohibited, and rejects
actual-revenue, conversion, success, or progress claims. An unconfigured local
runner is valid in `DISCOVER`, with a warning that it is required before
`LOCAL_PARITY`.

The returned manifest contains only ordered action names and required-input
labels. Collision observations are classified as `READY_TO_APPLY`,
`ALREADY_APPLIED`, `PARTIAL_COLLISION`, `CONFLICT`, or `BLOCKED`; this public
repository does not perform any of those actions.

