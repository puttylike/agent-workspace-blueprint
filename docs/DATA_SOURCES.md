# Data Sources

| Source | Read operations | Authority |
|---|---|---|
| Private registry | YAML parsing | Project identity, lifecycle, phase, policy |
| Git | branch, revision, status, divergence, latest commit | Source history and worktree state |
| Project tasks | version, list JSON, ready JSON, interaction metadata | Task readiness and logical state |
| Code host | pull-request view | Pull-request state |
| Agent runtime | schema-validated agent/session list RPC | Agent presence and recent activity |
| Knowledge Git tree | Markdown reads | Curated summaries and links |

No reader writes to a project. Some task-store implementations can perform
physical storage maintenance during a logical read; such raw metadata is not
interpreted as task-state change.

