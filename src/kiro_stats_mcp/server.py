"""KiroStats MCP Server — exposes live session metrics.

Supports both Kiro storage layouts: the pre-1.0 execution files under
globalStorage and the 1.0+ session logs under ``~/.kiro/sessions``. See
``readers.py`` for the details of each.
"""

from fastmcp import FastMCP

from .readers import read_stats

mcp = FastMCP(name="kiro-stats")


def _fmt(ms: int | None) -> str | None:
    if ms is None:
        return None
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


@mcp.tool(description="Get current session credits and timing.")
def get_session_stats(layout: str = "auto", workspace_path: str | None = None) -> dict:
    """Return credits and timing for the active Kiro chat session.

    Args:
        layout: Storage layout to read. ``auto`` (default) prefers the Kiro 1.0+
            layout and falls back to pre-1.0. Force one with ``v1`` or ``legacy``.
        workspace_path: Optional absolute path. When several sessions are open,
            restricts the search to sessions bound to this workspace. Kiro 1.0+
            only, since pre-1.0 execution files did not record workspace paths.
    """
    try:
        data = read_stats(layout, workspace_path)
    except ValueError as exc:
        return {"error": str(exc)}

    if not data:
        return {"error": "No active Kiro chat session found."}

    result = {
        "credits_used": round(data["credits"], 4),
        "agent_time": _fmt(data["agent_ms"]),
        "session_time": _fmt(data["session_ms"]),
        "turns": data["turns"],
        "source": data["source"],
        "session_id": data["session_id"],
    }

    # Per-turn figures exist only in 1.0+, where they match the number Kiro
    # shows in the chat footer for the last completed turn.
    if data["last_turn_credits"] is not None:
        result["last_turn_credits"] = round(data["last_turn_credits"], 4)
        result["last_turn_time"] = _fmt(data["last_turn_ms"])

    notes = []
    if data["turn_in_flight"]:
        notes.append("Current turn still running; totals cover completed turns only.")
    if data["ambiguous"]:
        # Several windows were mid-turn, so the session was chosen heuristically.
        notes.append(
            "Multiple live sessions; pass workspace_path to target this one. "
            f"Reporting on {data['workspace_paths']}."
        )
    if notes:
        result["note"] = " ".join(notes)

    return result


def main():
    mcp.run()


if __name__ == "__main__":
    main()
