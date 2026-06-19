# KiroStats

Real-time credit usage and session tracking for Kiro IDE — as an MCP server.

No auth. No network. No cloud dependency. Reads directly from Kiro's local execution files.

## What You Get

Three metrics, live from the same source that powers Kiro's "Est. Credits Used" display:

- **Credits Used** — exact per-turn credits from execution files
- **Agent Time** — cumulative time Kiro spent processing
- **Session Time** — total time since the chat started

## Usage

Type `/stats` in any Kiro chat.

## How It Works

Kiro persists every agent execution to disk as JSON files containing `usageSummary[]` arrays with exact per-turn credit values. KiroStats reads these files directly.

**Data source:**
```
%APPDATA%/Kiro/User/globalStorage/kiro.kiroagent/{workspace-hash}/{session-hash}/{execution-hash}
```

The single tool (`get_session_stats`) finds the running execution, groups all executions by `chatSessionId`, and returns aggregated credits and timing.

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

Add to `~/.kiro/settings/mcp.json`:
```json
{
  "mcpServers": {
    "kiro-stats": {
      "command": "kiro-stats-mcp",
      "autoApprove": ["get_session_stats"]
    }
  }
}
```

Copy `.kiro/steering/stats.md` from this repo to `~/.kiro/steering/`.

## Tool

| Tool | Returns |
|------|---------|
| `get_session_stats` | `credits_used`, `agent_time`, `session_time` |

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
# Delete ~/.kiro/steering/stats.md
```

## License

MIT
