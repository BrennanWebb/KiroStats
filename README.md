# KiroStats

Real-time credit usage and session tracking for Kiro IDE — as an MCP server.

No auth. No network. No cloud dependency. Reads directly from Kiro's local session files.

Supports both storage layouts: **Kiro 1.0+** and **pre-1.0**.

## What You Get

Live from the same source that powers Kiro's "Est. Credits Used" footer:

- **Credits Used** — session total, summed across completed turns
- **Last Turn Credits** — the figure Kiro shows in the chat footer (1.0+ only)
- **Agent Time** — cumulative time Kiro spent processing
- **Session Time** — total time since the chat started
- **Turns** — number of completed turns

## Usage

Type `/stats` in any Kiro chat.

## How It Works

Kiro changed where it persists sessions between 0.x and 1.0. KiroStats reads both
and prefers the newer layout.

### Kiro 1.0+

Sessions are an append-only event log under the user home:

```
~/.kiro/sessions/{workspace-key}/sess_{uuid}/session.json
~/.kiro/sessions/{workspace-key}/sess_{uuid}/messages.jsonl
```

`session.json` holds `status`, `createdAt`, `modelId`, and `workspacePaths`.
Credits land in `messages.jsonl` as a `usage_summary` record appended when a turn
completes:

```json
{
  "payload": {
    "type": "usage_summary",
    "promptTurnSummaries": [
      { "unit": "credit", "unitPlural": "credits", "usage": 18.5044,
        "usedTools": ["read_files", "grep_search"] }
    ],
    "elapsedTime": 387160,
    "status": "success"
  }
}
```

That record is exactly what the chat footer renders — `18.5044` credits and
`387160 ms` display as `Est. Credits Used: 18.5 / Elapsed time: 6m 27s`.

### Pre-1.0

One JSON blob per agent execution, keyed by opaque hashes under the extension's
globalStorage:

```
{globalStorage}/kiro.kiroagent/{ws-hash}/{session-hash}/{execution-hash}
```

Each blob carries `chatSessionId`, `startTime`/`endTime`, `status`, and a
`usageSummary[]` array of per-response metering entries. KiroStats finds the
execution with `status == "running"` and aggregates its whole chat session.

## Known Limits

**Mid-turn credits aren't observable on 1.0+.** During a turn the extension holds
the metering stream (`meteringEvent`) in memory and flushes it to disk only at
turn completion. So a reading taken mid-turn covers everything through the *last
completed* turn, and the response carries a `note` saying so. Pre-1.0 could see
in-flight usage because the running execution file was on disk with a partial
`usageSummary`.

**Session auto-detection is best-effort.** With several Kiro windows mid-turn at
once, more than one session is genuinely `in_progress`. KiroStats prefers live
sessions and breaks ties on most recent activity, but pass `workspace_path` to be
exact. When the pick was a heuristic the response says so, and `session_id` is
always returned so you can verify.

## Install

```powershell
git clone https://github.com/BrennanWebb/KiroStats.git
cd KiroStats
.\install.ps1
```

The installer:
1. Pip-installs the MCP server package
2. Registers it in `~/.kiro/settings/mcp.json`
3. Creates a `/stats` steering file for manual invocation

**Restart Kiro after install.**

## Manual Install

```bash
pip install -e .
```

Find the absolute path of the interpreter you just installed into:

```bash
python -c "import sys; print(sys.executable)"
```

Add to `~/.kiro/settings/mcp.json`, using that path:

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

Copy `.kiro/steering/stats.md` from this repo to `~/.kiro/steering/`, and make
sure it is saved **without a BOM** — a BOM ahead of the `---` breaks front-matter
parsing.

Two reasons for the absolute path and `-m` form rather than the `kiro-stats-mcp`
console script:

- pip installs that script into a `Scripts` directory that is often not on PATH,
  and Kiro launches MCP servers without a shell
- on Windows 10/11, bare `python` frequently resolves to the Microsoft Store app
  execution alias in `WindowsApps`, a stub that can open the Store instead of
  running Python

`sys.executable` sidesteps both. The console script still works if its directory
is on your PATH.

## Tool

`get_session_stats(layout="auto", workspace_path=None)`

| Arg | Purpose |
|-----|---------|
| `layout` | `auto` (default) prefers 1.0+ and falls back to pre-1.0. Force with `v1` or `legacy`. |
| `workspace_path` | Absolute path to a workspace root. Disambiguates concurrent sessions. 1.0+ only. |

Returns `credits_used`, `agent_time`, `session_time`, `turns`, `source`,
`session_id`, plus `last_turn_credits` / `last_turn_time` on 1.0+ and a `note`
when a caveat applies.

## Supported Platforms

| OS | Kiro 1.0+ | Pre-1.0 |
|----|-----------|---------|
| Windows | `~\.kiro\sessions\` | `%APPDATA%\Kiro\User\globalStorage\kiro.kiroagent\` |
| macOS | `~/.kiro/sessions/` | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/` |
| Linux | `~/.kiro/sessions/` | `~/.config/Kiro/User/globalStorage/kiro.kiroagent/` |

The 1.0+ path is the user home on every platform, matching the extension's own
default of `path.join(os.homedir(), ".kiro", "sessions")`.

## Requirements

- Python 3.10+
- Kiro IDE installed and opened at least once

## A Note On Sources

Kiro's storage layout is not documented publicly, and
[kirodotdev/Kiro](https://github.com/kirodotdev/Kiro) is docs and issue tracking
only — there is no source to read. The schemas above were derived from the shipped
`kiro.kiro-agent` extension bundle and verified against live session data.
Expect them to drift between releases.

## Uninstall

```powershell
pip uninstall kiro-stats-mcp
# Remove "kiro-stats" from ~/.kiro/settings/mcp.json
# Delete ~/.kiro/steering/stats.md
```

## License

MIT
