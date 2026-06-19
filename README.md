# KiroStats

Real-time credit usage and session tracking for Kiro IDE — as an MCP server.

No auth. No network. No cloud dependency. Reads directly from Kiro's local execution files on your machine.

## What You Get

Once installed, Kiro automatically tracks every chat session:

- **Session credits** — exact per-turn credits from Kiro's execution files (same source as the UI)
- **Thinking time** — cumulative seconds the agent spent processing
- **Wall-clock time** — how long the chat has been open
- **Tool cost breakdown** — which tools are consuming the most credits
- **Plan usage** — monthly limit, used, remaining, overage charges
- **Jira-ready summaries** — markdown output you can paste or auto-attach to tickets

Ask at any point:
> "What's my credit usage this session?"
> "How long has Kiro been thinking?"
> "How long has this chat been open?"
> "Give me a session summary for Jira."

## How It Works

Kiro persists every agent execution to disk as JSON files containing `usageSummary[]` arrays with exact per-turn credit values. KiroStats reads these files directly — the same data that feeds the "Est. Credits Used" display in the UI.

**Data sources:**
- **Per-turn credits:** `%APPDATA%/Kiro/User/globalStorage/kiro.kiroagent/{workspace-hash}/{session-hash}/{execution-hash}`
- **Plan totals:** `%APPDATA%/Kiro/User/globalStorage/state.vscdb`

Session tracking works by:
1. Finding the currently "running" execution file
2. Identifying the `chatSessionId` to group all executions in this chat
3. Summing `usageSummary[].usage` across all matching executions
4. Reporting elapsed time from `startTime` of the first execution

## Install (One Command)

```powershell
git clone https://github.com/BrennanWebb/KiroStats.git
cd KiroStats
.\install.ps1
```

The installer:
1. Pip-installs the MCP server package
2. Registers it in your user-level `~/.kiro/settings/mcp.json`
3. Creates a hook that auto-starts session tracking on every chat
4. Creates a steering file so the agent knows how to use the tools

**Restart Kiro after install.** Session tracking begins on your next chat.

## Manual Install (if you prefer)

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

Then copy the hook and steering files from the install script, or create them manually.

## Tools

| Tool | Description |
|------|-------------|
| `start_session` | Finds the running execution, captures chatSessionId. Called automatically by hook. |
| `log_interaction` | Records thinking time for an agent turn. |
| `get_session_stats` | Returns session credits (from execution files), thinking time, wall-clock time, tool breakdown. |
| `get_plan_usage` | Returns full billing cycle info (limit, used, remaining, overage). |
| `get_session_summary` | Markdown-formatted summary for Jira/PR comments. |

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
# Remove from ~/.kiro/settings/mcp.json
# Delete ~/.kiro/steering/start.md
# Delete ~/.kiro/steering/credits.md
```

## License

MIT
