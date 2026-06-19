"""
KiroStats MCP Server

Reads live session data from Kiro's execution files:
- Est. Credits Used (sum of usageSummary[].usage)
- Agent Time (sum of execution durations)
- Session Time (first execution start to now)
"""

import json
import os
import platform
import re
import time
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP(
    name="kiro-stats",
    instructions="Call get_session_stats for live credit and timing metrics.",
)


def _agent_storage() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    else:
        config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(config) / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"


def _find_running() -> tuple[str, Path] | None:
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
    """Read all executions for a chat session."""
    total_credits = 0.0
    agent_ms = 0
    turns = 0
    first_start = None

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

            for entry in data["usageSummary"]:
                total_credits += entry.get("usage", 0)
                turns += 1

            start = data.get("startTime")
            if start and (first_start is None or start < first_start):
                first_start = start

            end = data.get("endTime")
            if start and end:
                agent_ms += end - start
            elif start and data.get("status") == "running":
                agent_ms += int(time.time() * 1000) - start

    session_ms = (int(time.time() * 1000) - first_start) if first_start else 0

    return {
        "credits": round(total_credits, 4),
        "agent_ms": agent_ms,
        "session_ms": session_ms,
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
def get_session_stats() -> dict:
    """
    Get live metrics for the current Kiro chat session.

    Returns credits_used, agent_time, and session_time.
    """
    result = _find_running()
    if not result:
        return {"error": "No active Kiro chat session found."}

    chat_id, ws = result
    data = _get_session_data(chat_id, ws)

    return {
        "credits_used": data["credits"],
        "agent_time": _fmt(data["agent_ms"]),
        "session_time": _fmt(data["session_ms"]),
    }


def main():
    mcp.run()


if __name__ == "__main__":
    main()
