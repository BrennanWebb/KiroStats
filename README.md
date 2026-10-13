# KiroStats

Credit usage and session timing for Kiro IDE, as a local MCP server.

No auth, no network, no cloud. Reads Kiro's own session files on disk. Works with
Kiro 1.0+ and pre-1.0.

## Usage

Type `/stats` in any Kiro chat.

```json
{
  "credits_used": 84.7983,
  "agent_time": "27m 4s",
  "session_time": "49m 46s",
  "turns": 4,
  "source": "kiro-1.x",
  "session_id": "sess_ec3ae179-3b53-4ee1-a6be-95c67922ebeb",
  "last_turn_credits": 7.2263,
  "last_turn_time": "1m 29s",
  "current_turn_time": "42s",
  "note": "Current turn still running (42s so far); its credits post when it completes."
}
```

## Install

```powershell
git clone https://github.com/BrennanWebb/KiroStats.git
cd KiroStats
.\install.ps1
```

The installer locates a Python 3.10+ interpreter, pip-installs the package,
registers the server in `~/.kiro/settings/mcp.json`, and writes a `/stats`
steering file.

Kiro normally picks up the new server on its own. Restart it if `/stats` does not
respond.

## Tool

`get_session_stats(layout="auto", workspace_path=None, session_id=None)`

| Arg | Purpose |
|-----|---------|
| `layout` | `auto` prefers 1.0+ and falls back to pre-1.0. Force with `v1` or `legacy`. |
| `workspace_path` | A workspace root path. Narrows the search to that workspace. 1.0+ only. |
| `session_id` | An exact session id, e.g. from an earlier response or the ambiguity note. |

Returns `credits_used` (session total), `agent_time` (completed turns),
`session_time`, `turns`, `source`, and `session_id`, plus `last_turn_credits` /
`last_turn_time` on 1.0+. `current_turn_time` appears while a turn is running.
A `note` field appears when a caveat applies.

## How It Works

Kiro moved session storage between 0.x and 1.0. KiroStats reads both and prefers
the newer layout, so a stale `globalStorage` tree left behind by an upgrade cannot
shadow live data.

### Kiro 1.0+

Verified unchanged through Kiro 1.2.56. An append-only event log under the user
home:

```
~/.kiro/sessions/{workspace-key}/sess_{uuid}/session.json
~/.kiro/sessions/{workspace-key}/sess_{uuid}/messages.jsonl
```

`session.json` carries `status`, `createdAt`, `modelId`, and `workspacePaths`.
Credits arrive in `messages.jsonl` as a `usage_summary` record, appended when a
turn completes:

```json
{"payload": {
  "type": "usage_summary",
  "promptTurnSummaries": [
    {"unit": "credit", "unitPlural": "credits", "usage": 18.5044}
  ],
  "elapsedTime": 387160,
  "status": "success"
}}
```

That record is what the chat footer renders: `18.5044` and `387160 ms` display as
`Est. Credits Used: 18.5 / Elapsed time: 6m 27s`.

Each turn is bracketed by `turn_start` and `turn_end` records. KiroStats counts
`turns` by `turn_end` (so aborted, unmetered turns still count) and uses an open
`turn_start` to report `current_turn_time` for the turn in progress.

Workflow steps (`agentMode` such as `wf-planner`) run as their own sessions and
share the parent chat's `workspacePaths`.

### Pre-1.0

One JSON blob per agent execution under the extension's globalStorage:

```
{globalStorage}/kiro.kiroagent/{ws-hash}/{session-hash}/{execution-hash}
```

Each carries `chatSessionId`, `startTime`/`endTime`, `status`, and a
`usageSummary[]` of per-response metering entries. KiroStats finds the execution
with `status == "running"` and aggregates its whole chat session.

## Limits

**Current-turn credits are unknowable on 1.0+.** Kiro holds the metering stream
in memory during a turn and flushes it only at turn completion, so a mid-turn
reading's credits cover through the last *completed* turn. The current turn's
elapsed time is reported as `current_turn_time`, and `note` says the turn is
still running. On a session's first turn, credits read 0 until it ends. Pre-1.0
could see in-flight usage, because the running execution file sat on disk with a
partial `usageSummary`.

**Session detection is best-effort.** Several Kiro windows mid-turn, or a chat
running workflow steps, leave more than one session live. A session counts as
live only if its status is `in_progress`/`waiting_on_user` and its log was
written in the last 30 minutes, so abandoned sessions stuck `in_progress` do not
win. KiroStats prefers live sessions and tiebreaks on recent activity. When
several are live, `note` lists each candidate with its `agentMode` (`wf-*` are
workflow steps); pass `session_id` to be exact. `session_id` always comes back so
you can verify.

## Manual Install

```bash
pip install -e .
python -c "import sys; print(sys.executable)"
```

Add to `~/.kiro/settings/mcp.json`, using that interpreter path:

```json
{
  "mcpServers": {
    "kiro-stats": {
      "command": "C:\\Path\\To\\python.exe",
      "args": ["-m", "kiro_stats_mcp.server"],
      "disabled": false,
      "autoApprove": ["get_session_stats"]
    }
  }
}
```

Copy `.kiro/steering/stats.md` to `~/.kiro/steering/`, saved **without a BOM** —
a BOM ahead of the `---` breaks front-matter parsing.

Use the absolute interpreter path and `-m` rather than the `kiro-stats-mcp`
console script. Kiro spawns MCP servers without a shell, and neither `Scripts` on
PATH nor a bare `python` is dependable there — on Windows 10/11 `python` often
resolves to the Microsoft Store alias stub.

## Platforms

| OS | Kiro 1.0+ | Pre-1.0 |
|----|-----------|---------|
| Windows | `~\.kiro\sessions\` | `%APPDATA%\Kiro\User\globalStorage\kiro.kiroagent\` |
| macOS | `~/.kiro/sessions/` | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/` |
| Linux | `~/.kiro/sessions/` | `~/.config/Kiro/User/globalStorage/kiro.kiroagent/` |

The 1.0+ path is home-relative everywhere, matching the extension's own
`path.join(os.homedir(), ".kiro", "sessions")`. Only Windows has been tested.

Requires Python 3.10+ and Kiro opened at least once.

## Sources

Kiro's storage layout is not publicly documented, and
[kirodotdev/Kiro](https://github.com/kirodotdev/Kiro) is docs and issue tracking
only — there is no source to read. These schemas were derived from the shipped
`kiro.kiro-agent` bundle and verified against live session data on 1.0.337 and
1.2.56.
Expect drift between releases.

## Uninstall

```powershell
pip uninstall kiro-stats-mcp
# remove "kiro-stats" from ~/.kiro/settings/mcp.json
# delete ~/.kiro/steering/stats.md
```

## License

MIT
