# kiro-usage-mcp

A local MCP server that exposes your Kiro IDE credit usage to AI agents.

No auth, no network calls — reads directly from Kiro's local SQLite cache on your machine.

## What it does

Provides a single tool `get_usage` that returns:
- Plan credit limit
- Credits used this billing cycle
- Remaining credits
- Percent used
- Overage details (if over limit): charges, rate, cap
- Days until reset

## Install

### Option A: uvx (recommended, no install needed)

Add to your `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "kiro-usage": {
      "command": "uvx",
      "args": ["kiro-usage-mcp"]
    }
  }
}
```

### Option B: pip install + run directly

```bash
pip install kiro-usage-mcp
```

Then in `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "kiro-usage": {
      "command": "kiro-usage-mcp"
    }
  }
}
```

### Option C: Run from source (development)

```bash
git clone https://github.com/BrennanWebb/KiroStats.git
cd KiroStats
pip install -e .
```

Then in `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "kiro-usage": {
      "command": "python",
      "args": ["-m", "kiro_usage_mcp.server"]
    }
  }
}
```

## Supported Platforms

| OS | State DB Path |
|----|---------------|
| Windows | `%APPDATA%\Kiro\User\globalStorage\state.vscdb` |
| macOS | `~/Library/Application Support/Kiro/User/globalStorage/state.vscdb` |
| Linux | `~/.config/Kiro/User/globalStorage/state.vscdb` |

## Requirements

- Python 3.10+
- Kiro IDE installed and opened at least once (so the state DB exists)

## How it works

Kiro stores usage data in a local SQLite database (`state.vscdb`) under the key `kiro.kiroAgent`. This includes a `resourceNotifications.usageState` object that the IDE UI uses to render the "Est. Credits Used" badge. This MCP server reads that same data and exposes it as a tool agents can call.

The data refreshes whenever Kiro syncs with AWS (typically every few minutes while the IDE is open).

## License

MIT
