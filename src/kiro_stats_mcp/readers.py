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

Kiro sometimes logs an extra ``turn_end`` with ``stopReason: "cancelled"``
partway through a turn that then carries on under the same ``executionId``
and ends with a normal ``turn_end``. Turns are therefore counted by distinct
``executionId``, and a turn closed by ``cancelled`` is treated as running
again when later records of that execution appear.

Workflow steps (``agentMode`` such as ``wf-planner``) run as separate sessions
that share the parent chat's ``workspacePaths``. Session selection therefore
reports every live candidate with its ``agentMode`` and accepts an exact
``session_id``. A session counts as live only when its status says so *and* its
log was written recently, so abandoned ``in_progress`` sessions cannot shadow
the real one. A step's ``session.json`` names its run and step under
``_meta.kiro.workflow`` (``workflowId``, ``nodeId``, ``nodePath``), which is how
a workflow run's step sessions are found. ``modelId`` and ``effortLevel`` there
are the session's current setting; the model can change mid-session, but step
sessions are short and run on one model in practice.
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

LAYOUTS = ("auto", "v1", "legacy")

# Every reader returns exactly these keys. A reader sets only what its layout
# records; the rest keep these defaults. None means "not recorded", and the
# tool leaves such fields out of its output.
_RESULT_DEFAULTS = {
    "source": None,
    "session_id": None,
    "status": None,
    "model": None,
    "effort_level": None,
    "agent_mode": None,
    # Workflow step name (nodeId), and its full path when that adds anything,
    # e.g. "build-loop/iter-1/code" for a repeat-loop iteration.
    "step": None,
    "step_path": None,
    "ambiguous": False,
    "candidates": (),
    "credits": 0.0,
    "last_turn_credits": None,
    "last_turn_ms": None,
    "turns": 0,
    "unit": "credits",
    "agent_ms": 0,
    "current_turn_ms": None,
    "session_ms": 0,
    "workspace_paths": (),
    "turn_in_flight": False,
}


def _result(**fields) -> dict:
    """Build a reader result from ``_RESULT_DEFAULTS`` plus ``fields``."""
    unknown = fields.keys() - _RESULT_DEFAULTS.keys()
    if unknown:
        raise KeyError(f"unknown reader result keys: {sorted(unknown)}")
    result = dict(_RESULT_DEFAULTS, **fields)
    result["candidates"] = list(result["candidates"])
    result["workspace_paths"] = list(result["workspace_paths"])
    return result


def _check_layout(layout: str) -> None:
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; expected auto, v1, or legacy")


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
        "reopened": False,
    }


def _v1_read_usage(messages: Path) -> dict:
    """Aggregate turn and usage records in a messages.jsonl log.

    Streams line by line and pre-filters on substrings so large logs stay
    cheap: only candidate lines are handed to the JSON parser.

    ``turns`` counts distinct ``executionId`` values across ``turn_end``
    records, so an extra ``cancelled`` turn_end inside one turn is not
    counted twice. It falls back to the number of ``usage_summary`` records
    for logs that carry no turn markers.
    ``turn_open`` is True/False when turn markers were seen, else None.
    ``reopened`` is True when the open turn was closed by a ``cancelled``
    turn_end and later records of the same execution showed it carrying on;
    that turn is still included in ``turns``.
    """
    total = 0.0
    agent_ms = 0
    turns = 0
    unit = None
    last_turn_credits = 0.0
    last_turn_ms = 0
    ended_execs = set()
    unkeyed_ends = 0            # turn_end records without an executionId
    open_turn_start_ms = None   # turn_start with no turn_end yet
    last_event_ms = None
    turn_open = None
    reopened = False
    start_exec = None           # executionId and start of the latest turn_start
    start_ms = None
    cancelled_exec = None       # execution last closed by a cancelled turn_end
    cancelled_start_ms = None

    if not messages.is_file():
        return _v1_empty_usage()

    try:
        handle = messages.open(encoding="utf-8", errors="replace")
    except OSError:
        return _v1_empty_usage()

    with handle:
        for line in handle:
            # After a cancelled turn_end, lines naming that execution are
            # parsed too: they show the turn is still going.
            resumes = cancelled_exec is not None and cancelled_exec in line
            if not resumes and not any(m in line for m in _V1_MARKERS):
                continue
            try:
                rec = json.loads(line)
                payload = rec.get("payload") or {}
                kind = payload.get("type")
                exec_id = payload.get("executionId")
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if kind not in _V1_MARKERS:
                if resumes and exec_id == cancelled_exec:
                    turn_open = True
                    reopened = True
                    open_turn_start_ms = cancelled_start_ms
                    cancelled_exec = None
                continue

            ts = rec.get("timestamp")
            ts = _iso_to_ms(ts) if isinstance(ts, str) else None
            if ts is not None:
                last_event_ms = max(last_event_ms or 0, ts)

            if kind == "turn_start":
                turn_open = True
                reopened = False
                open_turn_start_ms = ts
                start_exec, start_ms = exec_id, ts
                cancelled_exec = None
                continue
            if kind == "turn_end":
                turn_open = False
                reopened = False
                open_turn_start_ms = None
                if exec_id:
                    ended_execs.add(exec_id)
                else:
                    unkeyed_ends += 1
                if exec_id and payload.get("stopReason") == "cancelled":
                    cancelled_exec = exec_id
                    cancelled_start_ms = start_ms if exec_id == start_exec else None
                else:
                    cancelled_exec = None
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
        # Turns that logged a turn_end, one per execution; falls back to the
        # usage count for marker-less logs.
        "turns": (len(ended_execs) + unkeyed_ends) or turns,
        "unit": unit,
        "last_turn_credits": last_turn_credits,
        "last_turn_ms": last_turn_ms,
        "open_turn_start_ms": open_turn_start_ms,
        "last_event_ms": last_event_ms,
        "turn_open": turn_open,
        "reopened": reopened,
    }


def _v1_in_workspace(data: dict, workspace_path: str) -> bool:
    target = _norm(workspace_path)
    return target in [_norm(p) for p in (data.get("workspacePaths") or [])]


def _v1_workflow_meta(data: dict) -> dict:
    """``_meta.kiro.workflow`` of a step session, or {} for any other session."""
    meta = data.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    workflow = kiro.get("workflow") if isinstance(kiro, dict) else None
    return workflow if isinstance(workflow, dict) else {}


def _v1_step_path(workflow: dict) -> str | None:
    """The step's ``nodePath`` below the run, when it says more than ``nodeId``.

    Repeat-loop iterations and parallel branches share a ``nodeId`` (``code``
    twice) but not a path (``build-loop/iter-0/code``, ``build-loop/iter-1/code``).
    """
    path = workflow.get("nodePath")
    if not isinstance(path, list) or not path:
        return None
    if path[0] == workflow.get("workflowId"):
        path = path[1:]
    joined = "/".join(str(p) for p in path)
    return joined if joined and joined != workflow.get("nodeId") else None


def v1_workflow_session_ids(workflow_id: str,
                            workspace_path: str | None = None) -> list[str]:
    """Ids of every step session of one workflow run, oldest first.

    Matches ``session.json`` ``_meta.kiro.workflow.workflowId``. The chat that
    launched the run has no workflow block, so it is not included. Steps are
    ordered by ``createdAt``, which is the order they ran in.
    """
    root = v1_sessions_root()
    if not root.exists():
        return []

    found = []
    for sess, meta in _v1_iter_session_dirs(root):
        try:
            text = meta.read_text(encoding="utf-8")
            # Cheap pre-filter: most sessions do not name this run at all.
            if workflow_id not in text:
                continue
            data = json.loads(text)
        except (json.JSONDecodeError, OSError):
            continue
        if _v1_workflow_meta(data).get("workflowId") != workflow_id:
            continue
        if workspace_path and not _v1_in_workspace(data, workspace_path):
            continue
        sid = data.get("id") or sess.name
        found.append((_iso_to_ms(data.get("createdAt")) or 0, sid))

    found.sort()
    return [sid for _, sid in found]


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

        if workspace_path and not _v1_in_workspace(data, workspace_path):
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
    turns = usage["turns"]
    if usage["reopened"]:
        # Activity after a cancelled turn_end. A real cancellation can leave
        # trailing records too, so a live status must confirm it is running.
        turn_in_flight = status == "in_progress"
        if turn_in_flight:
            # Its cancelled turn_end was counted; the turn has not completed.
            turns -= 1
    current_turn_ms = (max(now - open_start, 0)
                       if turn_in_flight and open_start is not None else None)

    workflow = _v1_workflow_meta(meta)

    return _result(
        source="kiro-1.x",
        session_id=meta.get("id"),
        status=status,
        # The session's current setting; "auto" stays "auto", since the model
        # the server picked is not recorded.
        model=meta.get("modelId") or None,
        effort_level=meta.get("effortLevel") or None,
        agent_mode=meta.get("agentMode") or None,
        step=workflow.get("nodeId") or None,
        step_path=_v1_step_path(workflow),
        # >1 live session (several windows mid-turn, or a chat plus its
        # workflow step sessions) means the pick was a heuristic.
        ambiguous=live_count > 1,
        candidates=live_candidates,
        credits=usage["credits"],
        last_turn_credits=usage["last_turn_credits"],
        last_turn_ms=usage["last_turn_ms"],
        turns=turns,
        unit=usage["unit"] or "credits",
        # Completed turns only; the in-flight turn is in current_turn_ms.
        agent_ms=usage["agent_ms"],
        current_turn_ms=current_turn_ms,
        session_ms=(now - created) if created else 0,
        workspace_paths=meta.get("workspacePaths") or [],
        # Mid-turn the extension holds metering in memory; the on-disk credit
        # total only advances when the turn completes.
        turn_in_flight=turn_in_flight,
    )


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

    # Left at their defaults: model and effort (recorded only patchily, on
    # reasoning entries), agent mode and workflow step (not recorded),
    # per-turn figures (metering was per model response, so there is no turn
    # boundary), current_turn_ms (agent_ms already includes the running
    # execution's elapsed time) and workspace paths.
    return _result(
        source="kiro-0.x",
        session_id=chat_id,
        status="running",
        credits=agg["credits"],
        turns=agg["turns"],
        unit=agg["unit"] or "credits",
        agent_ms=agg["agent_ms"],
        session_ms=agg["session_ms"],
    )


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
    _check_layout(layout)
    if layout == "v1":
        return read_v1(workspace_path, session_id)
    if layout == "legacy":
        return read_legacy(session_id)

    return read_v1(workspace_path, session_id) or read_legacy(session_id)


def workflow_session_ids(workflow_id: str, layout: str = "auto",
                         workspace_path: str | None = None) -> list[str]:
    """Step session ids of a workflow run, oldest first.

    Only the 1.0+ layout records workflows, so ``legacy`` finds none.
    """
    _check_layout(layout)
    if layout == "legacy":
        return []
    return v1_workflow_session_ids(workflow_id, workspace_path)
