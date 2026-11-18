---
inclusion: manual
---
Call `get_session_stats` from the kiro-stats MCP server. Pass one of this session's workspace root paths as `workspace_path` so the right session is picked when several Kiro windows are open.

For a workflow, call it from the main chat after the workflow's steps finish: get the step session ids from one `inspect_workflow` call, then pass them all in one `get_session_stats` call as `session_ids` for per-step stats and a total. A turn's credits are written only when it ends, so steps should not call it themselves mid-turn.
