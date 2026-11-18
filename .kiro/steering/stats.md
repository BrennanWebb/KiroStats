---
inclusion: manual
---
Call `get_session_stats` from the kiro-stats MCP server. Pass one of this session's workspace root paths as `workspace_path` so the right session is picked when several Kiro windows are open.

For a workflow, call it from the main chat after the workflow's steps finish: pass the run id (`wf_...`) as `workflow_id` for per-step stats (model, effort, step) and a total with credits by model. To pick sessions by hand, pass their ids as `session_ids` instead. A turn's credits are written only when it ends, so steps should not call it themselves mid-turn.
