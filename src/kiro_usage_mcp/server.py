"""
Kiro Usage MCP Server

A local MCP server that reads Kiro IDE credit/usage data from the local
SQLite state cache and exposes it to agents via a single tool.

Works on any Windows machine with Kiro installed — no auth, no network,
purely local file reads.
"""

import json
import os
import platform
import sqlite3
from datetime import datetime, timezone

from fastmcp import FastMCP

mcp = FastMCP(
    name="kiro-usage",
    instructions=(
        "Provides real-time Kiro IDE credit usage information. "
        "Call get_usage to see credits consumed, plan limits, overage charges, "
        "and reset date for the current billing cycle."
    ),
)


def _get_db_path() -> str:
    """Resolve the Kiro state database path for the current OS."""
    system = platform.system()

    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            raise FileNotFoundError("APPDATA environment variable not set")
        return os.path.join(appdata, "Kiro", "User", "globalStorage", "state.vscdb")
    elif system == "Darwin":
        home = os.path.expanduser("~")
        return os.path.join(
            home, "Library", "Application Support", "Kiro", "User", "globalStorage", "state.vscdb"
        )
    elif system == "Linux":
        # XDG_CONFIG_HOME or ~/.config
        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return os.path.join(config_home, "Kiro", "User", "globalStorage", "state.vscdb")
    else:
        raise OSError(f"Unsupported platform: {system}")


def _read_usage_state() -> dict:
    """Read and parse usage state from the Kiro SQLite cache."""
    db_path = _get_db_path()

    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"Kiro state database not found at: {db_path}\n"
            "Make sure Kiro IDE is installed and has been opened at least once."
        )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = 'kiro.kiroAgent'")
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise ValueError("No kiro.kiroAgent key found in state database.")

    data = json.loads(row[0])
    usage_state = data.get("kiro.resourceNotifications.usageState")

    if not usage_state:
        raise ValueError(
            "No usage state found. Kiro may not have fetched usage data yet — "
            "open the IDE and wait a moment, then retry."
        )

    return usage_state


@mcp.tool()
def get_usage() -> dict:
    """
    Get current Kiro IDE credit usage for this machine.

    Returns plan limit, credits used, remaining credits, overage details,
    and the billing cycle reset date. Data comes from Kiro's local cache
    and reflects the last time the IDE synced usage (usually every few minutes).
    """
    usage_state = _read_usage_state()

    ts = usage_state.get("timestamp", 0)
    last_updated = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()

    results = []
    for breakdown in usage_state.get("usageBreakdowns", []):
        current = breakdown.get("currentUsage", 0)
        limit = breakdown.get("usageLimit", 0)
        remaining = max(0, limit - current)
        pct = breakdown.get("percentageUsed", 0)
        reset_date = breakdown.get("resetDate", "unknown")
        overage = breakdown.get("currentOverages", 0)
        overage_charges = breakdown.get("overageCharges", 0)
        overage_rate = breakdown.get("overageRate", 0)
        overage_cap = breakdown.get("overageCap", 0)
        currency = breakdown.get("currency", {})

        # Days until reset
        days_until_reset = None
        try:
            reset_dt = datetime.fromisoformat(reset_date.replace("Z", "+00:00"))
            days_until_reset = (reset_dt - datetime.now(timezone.utc)).days
        except (ValueError, AttributeError):
            pass

        entry = {
            "type": breakdown.get("type", "CREDIT"),
            "plan_limit": limit,
            "used": round(current, 2),
            "remaining": round(remaining, 2),
            "percent_used": round(pct, 2),
            "reset_date": reset_date,
            "days_until_reset": days_until_reset,
        }

        if overage > 0:
            entry["overage"] = {
                "credits_over_limit": round(overage, 2),
                "rate_per_credit": overage_rate,
                "charges": round(overage_charges, 2),
                "cap": overage_cap,
                "currency": currency.get("symbol", "$"),
            }

        results.append(entry)

    return {
        "usage": results,
        "last_synced": last_updated,
        "db_path": _get_db_path(),
    }


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
