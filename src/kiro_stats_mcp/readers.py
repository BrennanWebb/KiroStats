"""Storage readers for Kiro IDE session metrics.

Kiro moved its session persistence between 0.x and 1.0, so two readers live here.

Pre-1.0 ("legacy") — one JSON blob per agent execution, keyed by opaque hashes
under the extension's globalStorage:

    <globalStorage>/kiro.kiroagent/{ws-32hex}/{session-32hex}/{exec-32hex}

Each blob carries ``chatSessionId``, ``startTime``/``endTime``, ``status`` and a
``usageSummary[]`` array of per-response metering entries.

1.0+ ("v1") — an append-only event log per chat session under the user home:

    ~/.kiro/sessions/{ws-16hex}/sess_{uuid}/session.json
    ~/.kiro/sessions/{ws-16hex}/sess_{uuid}/messages.jsonl

Kiro 1.2.x (verified on 1.2.56) still writes this same layout.

Each turn in ``messages.jsonl`` is bracketed by ``turn_start`` and ``turn_end``
records, with a ``usage_summary`` record (credits and ``elapsedTime``) appended
just before ``turn_end``. Mid-turn the metering stream is held in memory by the
extension, so the current turn's credits are unknowable until it ends. Its
elapsed time is not: an open ``turn_start`` gives the in-flight turn's start, so
``current_turn_ms`` is reported alongside the completed-turn totals.

Workflow steps (``agentMode`` such as ``wf-planner``) run as separate sessions
that share the parent chat's ``workspacePaths``. Session selection therefore
reports every live candidate with its ``agentMode`` and accepts an exact
``session_id``. A session counts as live only when its status says so *and* its
log was written recently, so abandoned ``in_progress`` sessions cannot shadow
the real one.
"""

import json
import os
import platform
import re
import time
from datetime import datetime, timezone
from pathlib import Path

# session.json statuses that mean "this session is the one currently talking"
LIVE_STATUSES = frozenset({"in_progress", "waiting_on_user"})

# A live status older than this (by messages.jsonl mtime) is treated as stale:
# crashed or abandoned windows leave sessions stuck in_progress indefinitely.
STALE_LIVE_MS = 30 * 60 * 1000

# Substrings that mark the only messages.jsonl records the reader parses.
_V1_MARKERS = ("usage_summary", "turn_start", "turn_end")

_HEX32 = re.compile(r"^[a-f0-9]{32}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_to_ms(value: str | None) -> int | None:
    """Parse Kiro's ISO 8601 timestamps (always UTC, 'Z'-suffixed)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


# --------------------------------------------------------------------------
# Kiro 1.0+
# --------------------------------------------------------------------------

def v1_sessions_root() -> Path:
    """Sessions live under the user home on every platform.

    Mirrors the extension's own default:
        nodePath.join(os.homedir(), ".kiro", "sessions")
    """
    return Path.home() / ".kiro" / "sessions"


def _v1_iter_session_dirs(root: Path):
    """Yield (session_dir, session_json) for every session on disk."""
    try:
        workspace_dirs = list(root.iterdir())
    except OSError:
        return
    for ws in workspace_dirs:
        if not ws.is_dir():
            continue
        try:
            entries = list(ws.iterdir())
        except OSError:
            continue
        for sess in entries:
            meta = sess / "session.json"
            if sess.is_dir() and meta.is_file():
                yield sess, meta


def _v1_empty_usage() -> dict:
    return {
        "credits": 0.0, "agent_ms": 0, "turns": 0, "unit": None,
        "last_turn_credits": 0.0, "last_turn_ms": 0,
        "open_turn_start_ms": None, "last_event_ms": None, "turn_open": None,
    }


def _v1_read_usage(messages: Path) -> dict:
    """Aggregate turn and usage records in a messages.jsonl log.

    Streams line by line and pre-filters on substrings so large logs stay
    cheap: only candidate lines are handed to the JSON parser.

    ``turns`` counts ``turn_end`` records, falling back to the number of
    ``usage_summary`` records for logs that carry no turn markers.
    ``turn_open`` is True/False when turn markers were seen, else None.
    """
    total = 0.0
    agent_ms = 0
    turns = 0
    unit = None
    last_turn_credits = 0.0
    last_turn_ms = 0
    completed_turns = 0
    open_turn_start_ms = None   # turn_start with no turn_end yet
    last_event_ms = None
    turn_open = None

    if not messages.is_file():
        return _v1_empty_usage()

    try:
        handle = messages.open(encoding="utf-8", errors="replace")
    except OSError:
        return _v1_empty_usage()

    with handle:
        for line in handle:
            if not any(m in line for m in _V1_MARKERS):
                continue
            try:
                rec = json.loads(line)
                payload = rec.get("payload") or {}
                kind = payload.get("type")
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if kind not in _V1_MARKERS:
                continue

            ts = rec.get("timestamp")
            ts = _iso_to_ms(ts) if isinstance(ts, str) else None
            if ts is not None:
                last_event_ms = max(last_event_ms or 0, ts)

            if kind == "turn_start":
                turn_open = True
                open_turn_start_ms = ts
                continue
            if kind == "turn_end":
                turn_open = False
                open_turn_start_ms = None
                completed_turns += 1
                continue

            turns += 1
            elapsed = payload.get("elapsedTime") or 0
            agent_ms += elapsed
            last_turn_ms = elapsed

            turn_credits = 0.0
            for summary in payload.get("promptTurnSummaries") or []:
                turn_credits += summary.get("usage") or 0.0
                unit = unit or summary.get("unitPlural") or summary.get("unit")
            total += turn_credits
            last_turn_credits = turn_credits

    return {
        "credits": total,
        "agent_ms": agent_ms,
        # turn_end count; falls back to the usage count for marker-less logs.
        "turns": completed_turns or turns,
        "unit": unit,
        "last_turn_credits": last_turn_credits,
        "last_turn_ms": last_turn_ms,
        "open_turn_start_ms": open_turn_start_ms,
        "last_event_ms": last_event_ms,
        "turn_open": turn_open,
    }


def _v1_pick_session(root: Path, workspace_path: str | None,
                     session_id: str | None = None):
    """Choose the session to report on.

    A live session wins, since that is by definition the one invoking this
    tool. Live means a status in ``LIVE_STATUSES`` *and* a ``messages.jsonl``
    written within ``STALE_LIVE_MS``; abandoned windows leave sessions stuck
    ``in_progress`` forever. Ties break on the most recently appended
    ``messages.jsonl``, which tracks real turn activity more closely than
    ``session.json``.

    Auto-selection is best-effort: several windows mid-turn, or a chat running
    workflow steps (separate sessions sharing its workspace), leave more than
    one session genuinely live. The live candidates come back with their
    ``agentMode`` so callers can retry with an exact ``session_id``.

    Returns ``(session_dir, session_json, live_count, live_candidates)``.
    """
    now = _now_ms()
    candidates = []
    for sess, meta in _v1_iter_session_dirs(root):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if session_id and session_id not in (data.get("id"), sess.name):
            continue

        if workspace_path:
            target = _norm(workspace_path)
            paths = [_norm(p) for p in (data.get("workspacePaths") or [])]
            if target not in paths:
                continue

        messages = sess / "messages.jsonl"
        try:
            activity = (messages if messages.is_file() else meta).stat().st_mtime
        except OSError:
            continue

        live = (data.get("status") in LIVE_STATUSES
                and (now - activity * 1000) < STALE_LIVE_MS)
        candidates.append((live, activity, sess, data))

    if not candidates:
        return None

    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    live_candidates = [
        {"session_id": data.get("id") or sess.name, "agent_mode": data.get("agentMode")}
        for live, _, sess, data in candidates if live
    ]
    _, _, sess, data = candidates[0]
    return sess, data, len(live_candidates), live_candidates


def read_v1(workspace_path: str | None = None,
            session_id: str | None = None) -> dict | None:
    """Read session stats from the Kiro 1.0+ layout.

    ``session_id`` targets one session exactly (by ``session.json`` id or
    directory name); ``workspace_path`` narrows the search to one workspace.
    """
    root = v1_sessions_root()
    if not root.exists():
        return None

    picked = _v1_pick_session(root, workspace_path, session_id)
    if not picked:
        return None
    sess, meta, live_count, live_candidates = picked

    usage = _v1_read_usage(sess / "messages.jsonl")
    created = _iso_to_ms(meta.get("createdAt"))
    status = meta.get("status")
    now = _now_ms()

    open_start = usage["open_turn_start_ms"]
    if usage["turn_open"] is not None:
        # Turn markers are authoritative: an open turn_start means mid-turn.
        turn_in_flight = usage["turn_open"]
    else:
        # Marker-less log: fall back to the session status.
        turn_in_flight = status == "in_progress"
    current_turn_ms = (max(now - open_start, 0)
                       if turn_in_flight and open_start is not None else None)

    return {
        "source": "kiro-1.x",
        "session_id": meta.get("id"),
        "status": status,
        "model": meta.get("modelId"),
        "agent_mode": meta.get("agentMode"),
        # >1 live session (several windows mid-turn, or a chat plus its
        # workflow step sessions) means the pick was a heuristic.
        "ambiguous": live_count > 1,
        "candidates": live_candidates,
        "credits": usage["credits"],
        "last_turn_credits": usage["last_turn_credits"],
        "last_turn_ms": usage["last_turn_ms"],
        "turns": usage["turns"],
        "unit": usage["unit"] or "credits",
        # Completed turns only; the in-flight turn is in current_turn_ms.
        "agent_ms": usage["agent_ms"],
        "current_turn_ms": current_turn_ms,
        "session_ms": (now - created) if created else 0,
        "workspace_paths": meta.get("workspacePaths") or [],
        # Mid-turn the extension holds metering in memory; the on-disk credit
        # total only advances when the turn completes.
        "turn_in_flight": turn_in_flight,
    }


# --------------------------------------------------------------------------
# Kiro pre-1.0
# --------------------------------------------------------------------------

def legacy_agent_storage() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    if system == "Darwin":
        return (Path.home() / "Library" / "Application Support" / "Kiro"
                / "User" / "globalStorage" / "kiro.kiroagent")
    config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(config) / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"


def _legacy_find_running(root: Path) -> tuple[str, Path] | None:
    """Find the running execution's chatSessionId and its workspace dir."""
    try:
        workspaces = [d for d in root.iterdir() if d.is_dir() and _HEX32.match(d.name)]
    except OSError:
        return None

    for ws in sorted(workspaces, key=lambda d: d.stat().st_mtime, reverse=True):
        try:
            session_dirs = [d for d in ws.iterdir() if d.is_dir() and _HEX32.match(d.name)]
        except OSError:
            continue
        for sd in session_dirs:
            try:
                recent = sorted(sd.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:5]
            except OSError:
                continue
            for f in recent:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if data.get("status") == "running" and "chatSessionId" in data:
                    return data["chatSessionId"], ws
    return None


def _legacy_aggregate(chat_session_id: str, workspace: Path) -> dict:
    """Read every execution belonging to one chat session."""
    total = 0.0
    agent_ms = 0
    turns = 0
    unit = None
    first_start = None

    try:
        session_dirs = [d for d in workspace.iterdir() if d.is_dir() and _HEX32.match(d.name)]
    except OSError:
        session_dirs = []

    for sd in session_dirs:
        try:
            files = list(sd.iterdir())
        except OSError:
            continue
        for f in files:
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("chatSessionId") != chat_session_id or "usageSummary" not in data:
                continue

            for entry in data["usageSummary"]:
                total += entry.get("usage") or 0.0
                unit = unit or entry.get("unitPlural") or entry.get("unit")
                turns += 1

            start = data.get("startTime")
            if start and (first_start is None or start < first_start):
                first_start = start

            end = data.get("endTime")
            if start and end:
                agent_ms += end - start
            elif start and data.get("status") == "running":
                agent_ms += _now_ms() - start

    return {
        "credits": total,
        "agent_ms": agent_ms,
        "turns": turns,
        "unit": unit,
        "session_ms": (_now_ms() - first_start) if first_start else 0,
    }


def read_legacy(session_id: str | None = None) -> dict | None:
    """Read session stats from the pre-1.0 execution-file layout.

    With ``session_id``, returns None unless the running chat session matches.
    """
    root = legacy_agent_storage()
    if not root.exists():
        return None

    found = _legacy_find_running(root)
    if not found:
        return None

    chat_id, ws = found
    if session_id and chat_id != session_id:
        return None
    agg = _legacy_aggregate(chat_id, ws)

    return {
        "source": "kiro-0.x",
        "session_id": chat_id,
        "status": "running",
        "model": None,
        "agent_mode": None,
        "ambiguous": False,
        "candidates": [],
        "credits": agg["credits"],
        # Pre-1.0 metering was per model response, not per turn, so there is no
        # turn boundary to report.
        "last_turn_credits": None,
        "last_turn_ms": None,
        "turns": agg["turns"],
        "unit": agg["unit"] or "credits",
        "agent_ms": agg["agent_ms"],
        # agent_ms already includes the running execution's elapsed time.
        "current_turn_ms": None,
        "session_ms": agg["session_ms"],
        "workspace_paths": [],
        "turn_in_flight": False,
    }


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

READERS = {"v1": read_v1, "legacy": read_legacy}


def read_stats(layout: str = "auto", workspace_path: str | None = None,
               session_id: str | None = None) -> dict | None:
    """Read session stats, preferring the 1.0+ layout.

    ``layout`` accepts ``auto`` (default), ``v1``, or ``legacy``. Under ``auto``
    the 1.0+ layout is tried first and the pre-1.0 layout is used only when it
    yields nothing, so a stale globalStorage tree left behind by an upgrade
    never shadows live data.
    """
    if layout == "v1":
        return read_v1(workspace_path, session_id)
    if layout == "legacy":
        return read_legacy(session_id)
    if layout != "auto":
        raise ValueError(f"unknown layout {layout!r}; expected auto, v1, or legacy")

    return read_v1(workspace_path, session_id) or read_legacy(session_id)
