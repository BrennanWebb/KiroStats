# KiroStats

Real-time credit usage and session tracking for Kiro IDE — as an MCP server.

No auth. No network. No cloud dependency. Reads directly from Kiro's local execution files on your machine.

## What You Get

Three metrics, live from the same source that powers Kiro's "Est. Credits Used" display:

- **Credits Used** — exact per-turn credits from execution files
- **Agent Time** — cumulative time Kiro spent processing (sum of execution durations)
- **Session Time** — wall-clock time since you started tracking

## How It Works

Kiro persists every agent execution to disk as JSON files containing `usageSummary[]` arrays with exact per-turn credit values. KiroStats reads these files directly.

**Data source:**
```
%APPDATA%/Kiro/User/globalStorage/kiro.kiroagent/{workspace-hash}/{session-hash}/{execution-hash}
```

Session tracking works by:
1. Finding the currently "running" execution file
2. Identifying the `chatSessionId` to group all executions in this chat
3. Summing `usageSummary[].usage` across all matching executions
4. Computing agent time from `startTime`/`endTime` of each execution

## Install

```powershell
git clone https://github.com/BrennanWebb/KiroStats.git
cd KiroStats
.\install.ps1
```

The installer:
1. Pip-installs the MCP server package
2. Registers it in your user-level `~/.kiro/settings/mcp.json`
3. Creates steering files (`#start` and `#credits`) for manual invocation

**Restart Kiro after install.**

## Manual Install

```bash
pip install -e .
```

Add to `~/.kiro/settings/mcp.json`:
```json
{
  "mcpServers": {
    "kiro-stats": {
      "command": "kiro-stats-mcp",
      "autoApprove": ["start_session", "get_session_stats"]
    }
  }
}
```

Copy `.kiro/steering/start.md` and `.kiro/steering/credits.md` from this repo to `~/.kiro/steering/`.

## Usage

In any Kiro chat:
- **`#start`** — begins session tracking (call once per chat)
- **`#credits`** — reports credits used, agent time, session time

## Tools

| Tool | Description |
|------|-------------|
| `start_session` | Finds the running execution, captures chatSessionId. Idempotent. |
| `get_session_stats` | Returns credits_used, agent_time, session_time. |

## Supported Platforms

| OS | Kiro Data Path |
|----|----------------|
| Windows | `%APPDATA%\Kiro\User\globalStorage\kiro.kiroagent\` |
| macOS | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/` |
| Linux | `~/.config/Kiro/User/globalStorage/kiro.kiroagent/` |

## Requirements

- Python 3.10+
- Kiro IDE installed and opened at least once

## Uninstall

```powershell
pip uninstall kiro-stats-mcp
# Remove "kiro-stats" from ~/.kiro/settings/mcp.json
# Delete ~/.kiro/steering/start.md
# Delete ~/.kiro/steering/credits.md
```

## License

MIT
