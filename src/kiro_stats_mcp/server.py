"""KiroStats MCP Server — exposes live session metrics.

Supports both Kiro storage layouts: the pre-1.0 execution files under
globalStorage and the 1.0+ session logs under ``~/.kiro/sessions``. See
``readers.py`` for the details of each.
"""

import re

from fastmcp import FastMCP

from .readers import read_stats

mcp = FastMCP(name="kiro-stats")

# Separators accepted between ids in a session_ids string: commas, whitespace,
# and the brackets/quotes of a JSON array passed as a string.
_ID_SEPARATORS = re.compile(r"[\s,\[\]\"']+")


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


def _parse_ids(value: list[str] | str) -> list[str]:
    """Split ``"id1, id2"`` or ``["id1", "id2"]`` into unique ids, in order."""
    items = [value] if isinstance(value, str) else value
    ids = []
    for item in items:
        for part in _ID_SEPARATORS.split(str(item)):
            if part and part not in ids:
                ids.append(part)
    return ids


def _format(data: dict) -> dict:
    """Shape one reader result into the tool's per-session output."""
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

    # Elapsed time of the turn still running, from its on-disk turn_start.
    if data.get("current_turn_ms") is not None:
        result["current_turn_time"] = _fmt(data["current_turn_ms"])

    notes = []
    if data["turn_in_flight"]:
        so_far = result.get("current_turn_time")
        running = f" ({so_far} so far)" if so_far else ""
        notes.append(
            f"Current turn still running{running}; its credits post when it completes."
        )
    if data["ambiguous"]:
        # Several sessions are live (other windows, or workflow step sessions
        # sharing this workspace), so the session was chosen heuristically.
        listed = ", ".join(
            f"{c['session_id']} ({c.get('agent_mode') or 'unknown'})"
            for c in data.get("candidates") or []
        )
        notes.append(
            f"Multiple live sessions: {listed}. agentMode wf-* sessions are "
            "workflow steps. Pass session_id to target one. "
            f"Reporting on {data['session_id']} ({data.get('agent_mode') or 'unknown'}) "
            f"in {data['workspace_paths']}."
        )
    if notes:
        result["note"] = " ".join(notes)

    return result


def _many_session_stats(layout: str, workspace_path: str | None,
                        session_ids: list[str]) -> dict:
    """Report each requested session plus a total across the ones found.

    Totals sum the per-session figures as displayed (credits rounded to 4 dp,
    agent time truncated to whole seconds), so they equal the sum of the rows.
    ``session_time`` is not totaled: each is time since that session was
    created, so step sessions overlap and keep growing after they finish.
    """
    sessions = []
    credits = 0.0
    agent_ms = 0
    turns = 0
    missing = []
    running = []

    for sid in session_ids:
        data = read_stats(layout, workspace_path, sid)
        if not data:
            missing.append(sid)
            sessions.append({"session_id": sid, "error": "Session not found."})
            continue
        sessions.append(_format(data))
        credits += round(data["credits"], 4)
        agent_ms += (data["agent_ms"] // 1000) * 1000
        turns += data["turns"]
        if data["turn_in_flight"]:
            running.append(sid)

    result = {
        "sessions": sessions,
        "total": {
            "credits_used": round(credits, 4),
            "agent_time": _fmt(agent_ms),
            "turns": turns,
            "sessions_counted": len(session_ids) - len(missing),
        },
    }

    notes = []
    if running:
        notes.append(
            f"Turn still running in {', '.join(running)}; its credits and time "
            "are not in the total until it completes. Call again after it finishes."
        )
    if missing:
        notes.append(f"Not found, excluded from total: {', '.join(missing)}.")
    if notes:
        result["note"] = " ".join(notes)

    return result


@mcp.tool(description=(
    "Get session credits and timing. For a workflow, after its steps finish, "
    "pass the step session ids from inspect_workflow as session_ids for "
    "per-step stats and a total."
))
def get_session_stats(layout: str = "auto", workspace_path: str | None = None,
                      session_id: str | None = None,
                      session_ids: list[str] | str | None = None) -> dict:
    """Return credits and timing for the active Kiro chat session.

    Args:
        layout: Storage layout to read. ``auto`` (default) prefers the Kiro 1.0+
            layout and falls back to pre-1.0. Force one with ``v1`` or ``legacy``.
        workspace_path: Optional absolute path. When several sessions are open,
            restricts the search to sessions bound to this workspace. Kiro 1.0+
            only, since pre-1.0 execution files did not record workspace paths.
        session_id: Optional exact session id (e.g. from an earlier response or
            the ambiguity note). Reports on that session only.
        session_ids: Optional several exact session ids, as a list or one
            string such as ``"id1, id2, id3"``. Returns ``sessions`` (one entry
            per id, in order) and a ``total``. Use from the orchestrating chat
            once workflow steps have finished, since a turn's credits are
            written only when that turn ends.
    """
    if session_ids is not None:
        if session_id is not None:
            return {"error": "Pass session_id or session_ids, not both."}
        ids = _parse_ids(session_ids)
        if not ids:
            return {"error": "session_ids contained no session ids."}
        try:
            return _many_session_stats(layout, workspace_path, ids)
        except ValueError as exc:
            return {"error": str(exc)}

    try:
        data = read_stats(layout, workspace_path, session_id)
    except ValueError as exc:
        return {"error": str(exc)}

    if not data:
        return {"error": "No active Kiro chat session found."}

    return _format(data)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
