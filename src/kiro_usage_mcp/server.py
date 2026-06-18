"""
Kiro Usage MCP Server

A local MCP server that reads Kiro IDE credit/usage data from the local
SQLite state cache and provides session-level tracking for agents.

Features:
- Plan-level credit usage (monthly limit, used, remaining, overage)
- Per-session tracking (credits consumed, thinking time, wall-clock time)
- Machine/OS agnostic (Windows, macOS, Linux)
- Zero auth, zero network — purely local file reads

Usage:
  1. Agent calls `start_session` at the beginning of a chat
  2. Agent calls `get_session_stats` at any point to see session metrics
  3. Agent calls `get_plan_usage` for overall billing cycle info
"""

import json
import os
import platform
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP(
    name="kiro-usage",
    instructions=(
        "Provides Kiro IDE credit usage and session cost tracking. "
        "IMPORTANT: Call start_session at the very beginning of every chat session "
        "to begin tracking. Then call get_session_stats whenever the user asks about "
        "credits, cost, time, or session metrics. Call get_plan_usage for billing cycle info."
    ),
)

# In-memory session store (persists for the lifetime of the MCP server process)
_sessions: dict = {}
_active_session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return os.path.join(config_home, "Kiro", "User", "globalStorage", "state.vscdb")
    else:
        raise OSError(f"Unsupported platform: {system}")


def _read_current_usage() -> tuple[float, str]:
    """Read the current credit usage value and sync timestamp from Kiro's SQLite cache.
    
    Returns:
        Tuple of (current_usage_credits, last_synced_iso_timestamp)
    """
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

    breakdowns = usage_state.get("usageBreakdowns", [])
    if not breakdowns:
        return 0.0, ""

    ts = usage_state.get("timestamp", 0)
    last_synced = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()

    return breakdowns[0].get("currentUsage", 0.0), last_synced


def _read_usage_state() -> dict:
    """Read the full usage state from Kiro's SQLite cache."""
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


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.0f}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def start_session() -> dict:
    """
    Start tracking a new chat session. Call this ONCE at the beginning of
    every chat session.

    Captures a snapshot of current plan credits and wall-clock time so that
    subsequent calls to get_session_stats can calculate deltas.

    Returns the session_id and starting credit balance.
    """
    global _active_session_id

    session_id = str(uuid.uuid4())[:8]
    start_credits, start_synced = _read_current_usage()
    now = time.time()

    _sessions[session_id] = {
        "session_id": session_id,
        "start_time": now,
        "start_credits": start_credits,
        "start_synced": start_synced,
        "interactions": [],  # list of {start, end} for thinking-time tracking
    }
    _active_session_id = session_id

    start_dt = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    return {
        "session_id": session_id,
        "started_at": start_dt,
        "starting_credits_used": round(start_credits, 2),
        "cache_synced_at": start_synced,
        "message": "Session tracking started. Call get_session_stats at any time for metrics.",
    }


@mcp.tool()
def log_interaction(session_id: Optional[str] = None, thinking_seconds: Optional[float] = None) -> dict:
    """
    Log an agent interaction (turn) within the session.

    Call this after each agent turn to record thinking time.
    If thinking_seconds is not provided, records a zero-duration ping
    (useful for just updating the 'last activity' timestamp).

    Parameters:
        session_id: The session to log to. Uses active session if not provided.
        thinking_seconds: How many seconds the agent spent processing this turn.
    """
    global _active_session_id

    sid = session_id or _active_session_id
    if not sid or sid not in _sessions:
        return {"error": "No active session. Call start_session first."}

    session = _sessions[sid]
    now = time.time()

    interaction = {
        "timestamp": now,
        "thinking_seconds": thinking_seconds or 0.0,
    }
    session["interactions"].append(interaction)

    return {
        "session_id": sid,
        "interaction_logged": True,
        "total_interactions": len(session["interactions"]),
    }


@mcp.tool()
def get_session_stats(session_id: Optional[str] = None) -> dict:
    """
    Get current metrics for a chat session.

    Returns:
    - session_credits: Credits consumed during this session (delta from start)
    - total_thinking_time: Cumulative agent processing time
    - wall_clock_time: Total time since session started
    - interaction_count: Number of agent turns logged

    Parameters:
        session_id: Which session to query. Uses active session if not provided.
    """
    global _active_session_id

    sid = session_id or _active_session_id
    if not sid or sid not in _sessions:
        return {"error": "No active session. Call start_session first."}

    session = _sessions[sid]
    now = time.time()

    # Credit delta
    current_credits, last_synced = _read_current_usage()
    session_credits = current_credits - session["start_credits"]

    # Detect if the cache synced AFTER the session started (meaningful delta)
    # Parse last_synced to compare with session start
    sync_is_fresh = False
    if last_synced:
        try:
            sync_dt = datetime.fromisoformat(last_synced)
            session_start_dt = datetime.fromtimestamp(session["start_time"], tz=timezone.utc)
            sync_is_fresh = sync_dt > session_start_dt
        except (ValueError, TypeError):
            pass

    # Thinking time (sum of all logged interactions)
    total_thinking = sum(i["thinking_seconds"] for i in session["interactions"])

    # Wall clock
    wall_clock = now - session["start_time"]

    start_dt = datetime.fromtimestamp(session["start_time"], tz=timezone.utc).isoformat()

    result = {
        "session_id": sid,
        "started_at": start_dt,
        "session_credits_used": round(max(0, session_credits), 2),
        "total_thinking_time_seconds": round(total_thinking, 1),
        "total_thinking_time_display": _format_duration(total_thinking),
        "wall_clock_seconds": round(wall_clock, 1),
        "wall_clock_display": _format_duration(wall_clock),
        "interaction_count": len(session["interactions"]),
        "current_plan_credits_used": round(current_credits, 2),
        "last_synced": last_synced,
    }

    if not sync_is_fresh:
        result["note"] = (
            "Credit delta may be inaccurate — the usage cache has not synced "
            "since this session started. It typically refreshes every few minutes."
        )

    return result


@mcp.tool()
def get_plan_usage() -> dict:
    """
    Get overall Kiro plan usage for the current billing cycle.

    Returns plan limit, total credits used, remaining, overage details,
    and reset date. This is the full-cycle view (not session-specific).
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
    }


@mcp.tool()
def get_session_summary(session_id: Optional[str] = None) -> dict:
    """
    Get a formatted summary suitable for pasting into Jira comments,
    PR descriptions, or other documentation.

    Parameters:
        session_id: Which session to summarize. Uses active session if not provided.
    """
    global _active_session_id

    sid = session_id or _active_session_id
    if not sid or sid not in _sessions:
        return {"error": "No active session. Call start_session first."}

    stats = get_session_stats(sid)
    plan = get_plan_usage()

    plan_info = plan["usage"][0] if plan.get("usage") else {}

    summary = (
        f"**Kiro Session Summary**\n"
        f"- Session ID: {sid}\n"
        f"- Credits this session: {stats['session_credits_used']}\n"
        f"- Agent thinking time: {stats['total_thinking_time_display']}\n"
        f"- Wall clock time: {stats['wall_clock_display']}\n"
        f"- Interactions: {stats['interaction_count']}\n"
        f"- Plan usage: {plan_info.get('used', 'N/A')}/{plan_info.get('plan_limit', 'N/A')} "
        f"({plan_info.get('percent_used', 'N/A')}%)\n"
    )

    if plan_info.get("overage"):
        summary += f"- Overage charges: {plan_info['overage']['currency']}{plan_info['overage']['charges']}\n"

    return {
        "session_id": sid,
        "markdown_summary": summary,
        "raw_stats": stats,
    }


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
