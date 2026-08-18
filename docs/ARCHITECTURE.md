# Architecture

Agent Ops is a read-only observability layer. The private project registry
declares identity and policy; Git, the project task system, the agent runtime,
and the code-hosting API remain authoritative for their own state.

```text
private registry  ─┐
project Git       ─┤
project tasks     ─┼─> read-only controller ─> HTML and JSON views
agent runtime     ─┤
pull requests     ─┘

private Markdown knowledge ─> sanitized renderer + FTS5 search cache
```

Readers fail independently. A temporary failure is represented as `UNKNOWN`
or `UNAVAILABLE`; it does not take down unrelated project data. Successful
in-memory observations can be labelled as stale fallback data. SQLite is used
only as a rebuildable Wiki search cache.

A future Workspace Controller may add explicitly approved lifecycle operations,
but those operations do not exist in this release. A future Agent Ops service
may be exposed through private networking after a separate deployment review.

