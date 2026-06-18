# KiroStats

Real-time credit usage and session tracking for Kiro IDE — as an MCP server.

No auth. No network. No cloud dependency. Reads directly from Kiro's local state on your machine.

## What You Get

Once installed, Kiro automatically tracks every chat session:

- **Session credits** — how many credits this conversation has consumed
- **Thinking time** — cumulative seconds the agent spent processing
- **Wall-clock time** — how long the chat has been open
- **Plan usage** — monthly limit, used, remaining, overage charges
- **Jira-ready summaries** — markdown output you can paste or auto-attach to tickets

Ask at any point:
> "What's my credit usage this session?"
> "How long has Kiro been thinking?"
> "How long has this chat been open?"
> "Give me a session summary for Jira."

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
4. Adds a steering file so the agent knows how to use the tools

**Restart Kiro after install.** Session tracking begins on your next chat.

## Manual Install (if you prefer)

```bash
pip install -e .
```

Add to `~/.kiro/settings/mcp.json`:
```json
{
  "mcpServers": {
    "kiro-usage": {
      "command": "kiro-usage-mcp",
      "autoApprove": ["start_session", "log_interaction", "get_session_stats", "get_plan_usage", "get_session_summary"]
    }
  }
}
```

Then copy the hook and steering files from the install script, or create them manually.

## Tools

| Tool | Description |
|------|-------------|
| `start_session` | Snapshots current credits and starts the clock. Called automatically by hook. |
| `log_interaction` | Records thinking time for an agent turn. |
| `get_session_stats` | Returns session credits, thinking time, wall-clock time, interaction count. |
| `get_plan_usage` | Returns full billing cycle info (limit, used, remaining, overage). |
| `get_session_summary` | Markdown-formatted summary for Jira/PR comments. |

## Supported Platforms

| OS | Kiro State Path |
|----|-----------------|
| Windows | `%APPDATA%\Kiro\User\globalStorage\state.vscdb` |
| macOS | `~/Library/Application Support/Kiro/User/globalStorage/state.vscdb` |
| Linux | `~/.config/Kiro/User/globalStorage/state.vscdb` |

## Requirements

- Python 3.10+
- Kiro IDE installed and opened at least once

## How It Works

Kiro stores plan usage in a local SQLite database. The UI renders "Est. Credits Used" from this data after every response. KiroStats reads the same cache and exposes it to agents via MCP tools.

Session tracking works by snapshotting the `currentUsage` value at chat start, then computing the delta on each query. Thinking time is logged per-turn by the agent. Wall-clock time is the elapsed time since `start_session` was called.

## Uninstall

```powershell
pip uninstall kiro-usage-mcp
# Remove from ~/.kiro/settings/mcp.json
# Delete ~/.kiro/hooks/kiro-stats-session.json
# Delete ~/.kiro/steering/kiro-stats.md
```

## License

MIT
