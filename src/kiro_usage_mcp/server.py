"""
KiroStats MCP Server

Reads live session data from Kiro's execution files:
- Est. Credits Used (sum of usageSummary[].usage)
- Thinking Time (sum of execution durations)
- Wall Clock Time (time since session start)
"""

import json
import os
import platform
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP(
    name="kiro-usage",
    instructions=(
        "Tracks Kiro session credit usage and timing. "
        "Call start_session once per chat. Call get_session_stats for live metrics."
    ),
)

_sessions: dict = {}
_active_session_id: Optional[str] = None


def _agent_storage() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    else:
        config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(config) / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"


def _find_running() -> Optional[tuple[str, Path]]:
    """Find the running execution's chatSessionId and its workspace dir."""
    root = _agent_storage()
    if not root.exists():
        return None
    for ws in sorted(
        [d for d in root.iterdir() if d.is_dir() and re.match(r'^[a-f0-9]{32}$', d.name)],
        key=lambda d: d.stat().st_mtime, reverse=True,
    ):
        for sd in [d for d in ws.iterdir() if d.is_dir() and re.match(r'^[a-f0-9]{32}$', d.name)]:
            for f in sorted(sd.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if data.get("status") == "running" and "chatSessionId" in data:
                        return data["chatSessionId"], ws
                except (json.JSONDecodeError, OSError):
                    continue
    return None


def _get_session_data(chat_session_id: str, workspace: Path) -> dict:
    """Read all executions for a chat session. Returns credits, thinking time."""
    total_credits = 0.0
    thinking_ms = 0
    turns = 0

    for sd in [d for d in workspace.iterdir() if d.is_dir() and re.match(r'^[a-f0-9]{32}$', d.name)]:
        for f in sd.iterdir():
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("chatSessionId") != chat_session_id:
                continue
            if "usageSummary" not in data:
                continue

            # Credits
            for entry in data["usageSummary"]:
                total_credits += entry.get("usage", 0)
                turns += 1

            # Thinking time = endTime - startTime per execution
            start = data.get("startTime")
            end = data.get("endTime")
            if start and end:
                thinking_ms += end - start
            elif start and data.get("status") == "running":
                thinking_ms += int(time.time() * 1000) - start

    return {
        "credits": round(total_credits, 4),
        "thinking_ms": thinking_ms,
        "turns": turns,
    }


def _fmt(ms: int) -> str:
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


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

    # If we already have an active session, just return it (idempotent)
    if _active_session_id and _active_session_id in _sessions:
        session = _sessions[_active_session_id]
        return {
            "session_id": _active_session_id,
            "started_at": datetime.fromtimestamp(session["start_time"], tz=timezone.utc).isoformat(),
            "already_active": True,
            "message": "Session already active.",
        }

    session_id = str(uuid.uuid4())[:8]
    now = time.time()

    result = _find_running()
    chat_id, workspace = result if result else (None, None)

    _sessions[session_id] = {
        "start_time": now,
        "chat_session_id": chat_id,
        "workspace": workspace,
    }
    _active_session_id = session_id

    return {
        "session_id": session_id,
        "started_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "starting_credits_used": 0,
        "message": "Session tracking started. Call get_session_stats at any time for metrics.",
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
    sid = session_id or _active_session_id
    if not sid or sid not in _sessions:
        return {"error": "No active session. Call start_session first."}

    session = _sessions[sid]

    # Retry finding chat session if not captured at start
    chat_id = session.get("chat_session_id")
    ws = session.get("workspace")
    if not chat_id:
        result = _find_running()
        if result:
            chat_id, ws = result
            session["chat_session_id"] = chat_id
            session["workspace"] = ws

    if not chat_id or not ws:
        return {"error": "Cannot find active Kiro chat session."}

    data = _get_session_data(chat_id, ws)
    wall_ms = int((time.time() - session["start_time"]) * 1000)

    return {
        "credits_used": data["credits"],
        "thinking_time": _fmt(data["thinking_ms"]),
        "wall_clock": _fmt(wall_ms),
    }


def main():
    mcp.run()


if __name__ == "__main__":
    main()
