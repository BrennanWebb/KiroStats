"""Tests for both Kiro storage-layout readers.

Fixtures mirror the real on-disk shapes captured from Kiro 1.0.337, 1.2.56, and 0.12.
"""

import json
import time

import pytest

from kiro_stats_mcp import readers
from kiro_stats_mcp.server import get_session_stats


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _usage_record(usage, elapsed_ms, status="success", unit="credit"):
    return {
        "id": "exec-usage",
        "timestamp": "2026-08-31T20:04:52.627Z",
        "payload": {
            "type": "usage_summary",
            "promptTurnSummaries": [
                {"unit": unit, "unitPlural": unit + "s", "usage": usage,
                 "usedTools": ["read_files"]},
            ],
            "elapsedTime": elapsed_ms,
            "status": status,
            "executionId": "exec",
        },
    }


def _turn_marker(kind, timestamp, execution_id="exec", stop_reason="end_turn"):
    payload = {"type": kind, "executionId": execution_id}
    if kind == "turn_end":
        payload["stopReason"] = stop_reason
    return {"id": f"m-{kind}", "timestamp": timestamp, "payload": payload}


def _write_v1_session(root, ws_key, sess_id, *, status, created, turns,
                      workspace_paths=None, model="claude-opus-5",
                      agent_mode="vibe", markers=False, open_turn_at=None,
                      effort=None, workflow=None, root_conversation=None):
    """Write a session dir.

    ``turns`` is a list of ``(usage, elapsed_ms)``; ``usage=None`` writes a turn
    with no usage_summary (aborted/unmetered). ``markers`` brackets each turn
    with turn_start/turn_end. ``open_turn_at`` appends a trailing turn_start at
    that ISO timestamp with no turn_end. ``model``/``effort`` of None leave
    ``modelId``/``effortLevel`` out. ``workflow`` is written as
    ``_meta.kiro.workflow``, as on a workflow step session.
    """
    sess = root / ws_key / sess_id
    sess.mkdir(parents=True)
    data = {
        "schemaVersion": "1.0.0",
        "id": sess_id,
        "status": status,
        "createdAt": created,
        "agentMode": agent_mode,
        "workspacePaths": workspace_paths or [],
    }
    if model is not None:
        data["modelId"] = model
    if effort is not None:
        data["effortLevel"] = effort
    if root_conversation is not None:
        data["rootConversationId"] = root_conversation
    if workflow is not None:
        data["_meta"] = {"kiro": {"workflow": workflow, "modelId": model,
                                  "effortLevel": effort}}
    (sess / "session.json").write_text(json.dumps(data), encoding="utf-8")

    lines = []
    for i, (usage, elapsed) in enumerate(turns):
        exec_id = f"exec-{i}"
        if markers:
            lines.append(json.dumps(_turn_marker("turn_start", created, exec_id)))
        if usage is not None:
            lines.append(json.dumps(_usage_record(usage, elapsed)))
        # Interleave unrelated records; the reader must skip them.
        lines.append(json.dumps({
            "id": "m", "timestamp": created,
            "payload": {"type": "session_metadata", "key": "contextUsage",
                        "value": {"usagePercentage": 4.2}},
        }))
        if markers:
            lines.append(json.dumps(_turn_marker(
                "turn_end", created, exec_id,
                stop_reason="end_turn" if usage is not None else "aborted")))
    if open_turn_at is not None:
        lines.append(json.dumps(_turn_marker("turn_start", open_turn_at, "exec-open")))
        lines.append(json.dumps({
            "id": "a", "timestamp": open_turn_at,
            "payload": {"type": "assistant", "content": "working on turn_end soon"},
        }))
    (sess / "messages.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sess


def _write_legacy_execution(root, ws_hash, sess_hash, exec_hash, *,
                            chat_id, status, start, end, usages):
    d = root / ws_hash / sess_hash
    d.mkdir(parents=True, exist_ok=True)
    blob = {
        "chatSessionId": chat_id,
        "executionId": exec_hash,
        "status": status,
        "startTime": start,
        "usageSummary": [
            {"usedTools": ["execute_pwsh"], "usage": u,
             "unit": "credit", "unitPlural": "credits"}
            for u in usages
        ],
    }
    if end is not None:
        blob["endTime"] = end
    (d / exec_hash).write_text(json.dumps(blob), encoding="utf-8")


H32_A = "a" * 32
H32_B = "b" * 32
H32_C = "c" * 32


@pytest.fixture
def v1_root(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(readers, "v1_sessions_root", lambda: root)
    return root


@pytest.fixture
def legacy_root(tmp_path, monkeypatch):
    root = tmp_path / "kiro.kiroagent"
    root.mkdir()
    monkeypatch.setattr(readers, "legacy_agent_storage", lambda: root)
    return root


# --------------------------------------------------------------------------
# 1.0+ reader
# --------------------------------------------------------------------------

def test_v1_sums_credits_and_agent_time(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(1.5, 1000), (2.25, 2000)])

    got = readers.read_v1()
    assert got["source"] == "kiro-1.x"
    assert got["credits"] == pytest.approx(3.75)
    assert got["agent_ms"] == 3000
    assert got["turns"] == 2
    assert got["unit"] == "credits"


def test_v1_last_turn_matches_footer(v1_root):
    """The footer renders the most recent usage_summary, not the session total."""
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(1.0, 1000), (18.50437219761194, 387160)])

    got = readers.read_v1()
    assert got["last_turn_credits"] == pytest.approx(18.50437219761194)
    assert got["last_turn_ms"] == 387160
    assert got["credits"] == pytest.approx(19.50437219761194)


def test_v1_prefers_live_session_over_more_recent_idle(v1_root):
    live = _write_v1_session(v1_root, "ws01", "sess_live", status="in_progress",
                             created="2026-08-31T20:00:00.000Z", turns=[(5.0, 500)])
    idle = _write_v1_session(v1_root, "ws01", "sess_idle", status="idle",
                             created="2026-08-31T20:00:00.000Z", turns=[(9.0, 900)])
    # Make the idle session the most recently touched.
    later = time.time() + 60
    for p in (idle / "messages.jsonl", idle / "session.json"):
        import os
        os.utime(p, (later, later))

    got = readers.read_v1()
    assert got["session_id"] == "sess_live"
    assert got["turn_in_flight"] is True
    assert live.exists()


def test_v1_waiting_on_user_counts_as_live(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_wait", status="waiting_on_user",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])
    _write_v1_session(v1_root, "ws01", "sess_done", status="completed",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])

    got = readers.read_v1()
    assert got["session_id"] == "sess_wait"
    # Only in_progress means a turn is actively mid-flight.
    assert got["turn_in_flight"] is False


def test_v1_workspace_path_disambiguates_concurrent_sessions(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      workspace_paths=[r"c:\repos\alpha"])
    _write_v1_session(v1_root, "ws02", "sess_b", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[(2.0, 200)],
                      workspace_paths=[r"c:\repos\beta"])

    assert readers.read_v1(r"c:\repos\alpha")["session_id"] == "sess_a"
    assert readers.read_v1(r"c:\repos\beta")["session_id"] == "sess_b"


def test_v1_workspace_path_is_case_and_separator_insensitive(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      workspace_paths=[r"c:\repos\KiroStats"])

    assert readers.read_v1("C:\\Repos\\KiroStats\\")["session_id"] == "sess_a"


def test_v1_flags_ambiguity_only_without_workspace_path(v1_root):
    for key, sid, path in [("ws01", "sess_a", r"c:\a"), ("ws02", "sess_b", r"c:\b")]:
        _write_v1_session(v1_root, key, sid, status="in_progress",
                          created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                          workspace_paths=[path])

    assert readers.read_v1()["ambiguous"] is True
    assert readers.read_v1(r"c:\a")["ambiguous"] is False


def test_v1_single_live_session_is_not_ambiguous(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])
    _write_v1_session(v1_root, "ws01", "sess_b", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])

    assert readers.read_v1()["ambiguous"] is False


def test_v1_unknown_workspace_yields_nothing(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      workspace_paths=[r"c:\repos\alpha"])

    assert readers.read_v1(r"c:\nope") is None


def test_v1_session_with_no_completed_turns(v1_root):
    """First turn of a fresh session: log exists but holds no usage_summary yet."""
    sess = _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                             created="2026-08-31T20:00:00.000Z", turns=[])
    (sess / "messages.jsonl").write_text("", encoding="utf-8")

    got = readers.read_v1()
    assert got["credits"] == 0.0
    assert got["turns"] == 0
    assert got["last_turn_credits"] == 0.0
    assert got["turn_in_flight"] is True


def test_v1_tolerates_corrupt_lines_and_missing_files(v1_root):
    sess = _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                             created="2026-08-31T20:00:00.000Z", turns=[(4.0, 400)])
    with (sess / "messages.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"payload": {"type": "usage_summary"  BROKEN\n')
        fh.write("not json at all\n")

    got = readers.read_v1()
    assert got["credits"] == pytest.approx(4.0)
    assert got["turns"] == 1


def test_v1_ignores_session_dir_without_metadata(v1_root):
    (v1_root / "ws01" / "sess_orphan").mkdir(parents=True)
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])

    assert readers.read_v1()["session_id"] == "sess_a"


def test_v1_session_time_measured_from_created_at(v1_root, monkeypatch):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])
    # 2026-08-31T20:00:00Z plus 90s
    monkeypatch.setattr(readers, "_now_ms", lambda: 1788206400000 + 90_000)

    assert readers.read_v1()["session_ms"] == 90_000


def test_v1_missing_root_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "v1_sessions_root", lambda: tmp_path / "absent")
    assert readers.read_v1() is None


def test_v1_sessions_root_is_under_home():
    root = readers.v1_sessions_root()
    assert root.parts[-2:] == (".kiro", "sessions")


# 2026-08-31T20:00:00Z in epoch ms
T0_MS = 1788206400000


def test_v1_in_flight_turn_time_from_turn_start(v1_root, monkeypatch):
    """First turn of a fresh session: no credits yet, but elapsed time is known."""
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[],
                      open_turn_at="2026-08-31T20:05:00.000Z")
    monkeypatch.setattr(readers, "_now_ms", lambda: T0_MS + 300_000 + 42_000)

    got = readers.read_v1()
    assert got["turn_in_flight"] is True
    assert got["current_turn_ms"] == 42_000
    assert got["credits"] == 0.0
    assert got["turns"] == 0
    # agent_ms stays completed-turns-only.
    assert got["agent_ms"] == 0


def test_v1_in_flight_turn_after_completed_turns(v1_root, monkeypatch):
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(2.5, 1000), (1.5, 2000)], markers=True,
                      open_turn_at="2026-08-31T20:05:00.000Z")
    monkeypatch.setattr(readers, "_now_ms", lambda: T0_MS + 300_000 + 7_000)

    got = readers.read_v1()
    assert got["turns"] == 2
    assert got["credits"] == pytest.approx(4.0)
    assert got["agent_ms"] == 3000
    assert got["current_turn_ms"] == 7_000


def test_v1_closed_turn_not_in_flight_even_if_status_in_progress(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(1.0, 1000)], markers=True)

    got = readers.read_v1()
    assert got["turn_in_flight"] is False
    assert got["current_turn_ms"] is None
    assert got["turns"] == 1


def test_v1_turns_counts_turn_end_without_usage(v1_root):
    """An aborted turn with turn_end but no usage_summary still counts."""
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(1.0, 1000), (None, 0), (2.0, 500)], markers=True)

    got = readers.read_v1()
    assert got["turns"] == 3
    assert got["credits"] == pytest.approx(3.0)
    assert got["agent_ms"] == 1500


def _activity(kind, timestamp, execution_id):
    return {"id": f"m-{kind}", "timestamp": timestamp,
            "payload": {"type": kind, "executionId": execution_id}}


def _write_v1_log(root, sess_id, records, *, status="idle"):
    """Write a session whose messages.jsonl holds exactly ``records``."""
    sess = _write_v1_session(root, "ws01", sess_id, status=status,
                             created="2026-08-31T20:00:00.000Z", turns=[])
    (sess / "messages.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return sess


def _split_turn(exec_id="exec-x", usage=1.0234, elapsed=14_915):
    """One turn with Kiro's extra mid-turn `cancelled` turn_end (seen on 1.2.x)."""
    return [
        _turn_marker("turn_start", "2026-08-31T20:05:00.000Z", exec_id),
        _turn_marker("turn_end", "2026-08-31T20:05:03.000Z", exec_id,
                     stop_reason="cancelled"),
        _activity("tool_call", "2026-08-31T20:05:04.000Z", exec_id),
        _activity("assistant", "2026-08-31T20:05:10.000Z", exec_id),
        _usage_record(usage, elapsed),
        _turn_marker("turn_end", "2026-08-31T20:05:14.000Z", exec_id,
                     stop_reason="end_turn"),
    ]


def test_v1_cancelled_turn_end_inside_turn_counts_once(v1_root):
    _write_v1_log(v1_root, "sess_a", _split_turn())

    got = readers.read_v1()
    assert got["turns"] == 1
    assert got["credits"] == pytest.approx(1.0234)
    assert got["agent_ms"] == 14_915
    assert got["turn_in_flight"] is False
    assert got["current_turn_ms"] is None


def test_v1_turn_still_running_after_cancelled_turn_end(v1_root, monkeypatch):
    _write_v1_log(v1_root, "sess_a", [
        _turn_marker("turn_start", "2026-08-31T20:01:00.000Z", "exec-y"),
        _usage_record(0.5, 2_000),
        _turn_marker("turn_end", "2026-08-31T20:01:02.000Z", "exec-y"),
        *_split_turn()[:4],
    ], status="in_progress")
    monkeypatch.setattr(readers, "_now_ms", lambda: T0_MS + 300_000 + 42_000)

    got = readers.read_v1()
    assert got["turn_in_flight"] is True
    # Measured from the turn's own turn_start, not the cancelled turn_end.
    assert got["current_turn_ms"] == 42_000
    assert got["turns"] == 1
    assert got["credits"] == pytest.approx(0.5)


def test_v1_real_cancel_with_trailing_records_is_closed(v1_root):
    _write_v1_log(v1_root, "sess_a", _split_turn()[:3], status="idle")

    got = readers.read_v1()
    assert got["turn_in_flight"] is False
    assert got["current_turn_ms"] is None
    assert got["turns"] == 1


def test_v1_real_cancel_then_new_turn_counts_both(v1_root):
    _write_v1_log(v1_root, "sess_a", [
        *_split_turn("exec-x")[:3],
        *_split_turn("exec-y"),
    ], status="in_progress")

    got = readers.read_v1()
    assert got["turns"] == 2
    assert got["turn_in_flight"] is False


def test_v1_stale_in_progress_does_not_win(v1_root):
    import os
    stale = _write_v1_session(v1_root, "ws01", "sess_stale", status="in_progress",
                              created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])
    _write_v1_session(v1_root, "ws01", "sess_fresh", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(2.0, 200)])
    old = time.time() - 2 * 3600
    os.utime(stale / "messages.jsonl", (old, old))

    got = readers.read_v1()
    assert got["session_id"] == "sess_fresh"
    assert got["ambiguous"] is False
    assert got["candidates"] == []


def test_v1_ambiguous_with_workspace_path_when_workflow_step_live(v1_root):
    path = r"c:\repos\KiroStats"
    _write_v1_session(v1_root, "ws01", "sess_parent", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[(3.0, 300)],
                      workspace_paths=[path], agent_mode="vibe")
    _write_v1_session(v1_root, "ws01", "sess_step", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[],
                      workspace_paths=[path], agent_mode="wf-planner")

    got = readers.read_v1(path)
    assert got["ambiguous"] is True
    assert {(c["session_id"], c["agent_mode"]) for c in got["candidates"]} == {
        ("sess_parent", "vibe"), ("sess_step", "wf-planner"),
    }


def test_v1_session_id_targets_exact_session(v1_root):
    path = r"c:\repos\KiroStats"
    for sid, mode in [("sess_parent", "vibe"), ("sess_step", "wf-planner")]:
        _write_v1_session(v1_root, "ws01", sid, status="in_progress",
                          created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                          workspace_paths=[path], agent_mode=mode)

    got = readers.read_v1(session_id="sess_parent")
    assert got["session_id"] == "sess_parent"
    assert got["agent_mode"] == "vibe"
    assert got["ambiguous"] is False
    assert readers.read_v1(session_id="sess_nope") is None


# --------------------------------------------------------------------------
# pre-1.0 reader
# --------------------------------------------------------------------------

def test_legacy_aggregates_running_chat_session(legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=None, usages=[0.5, 0.25])

    got = readers.read_legacy()
    assert got["source"] == "kiro-0.x"
    assert got["session_id"] == "chat-1"
    assert got["credits"] == pytest.approx(0.75)
    assert got["turns"] == 2
    # No per-turn boundary existed pre-1.0.
    assert got["last_turn_credits"] is None


def test_legacy_spans_multiple_executions_of_same_chat(legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, "d" * 32,
                            chat_id="chat-1", status="succeed",
                            start=1_000_000, end=1_005_000, usages=[1.0])
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_010_000, end=1_012_000, usages=[2.0])

    got = readers.read_legacy()
    assert got["credits"] == pytest.approx(3.0)
    assert got["agent_ms"] == 5_000 + 2_000


def test_legacy_excludes_other_chat_sessions(legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=1_001_000, usages=[1.0])
    _write_legacy_execution(legacy_root, H32_A, H32_B, "d" * 32,
                            chat_id="chat-2", status="succeed",
                            start=1_000_000, end=1_001_000, usages=[99.0])

    assert readers.read_legacy()["credits"] == pytest.approx(1.0)


def test_legacy_counts_in_flight_execution_time(legacy_root, monkeypatch):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=None, usages=[1.0])
    monkeypatch.setattr(readers, "_now_ms", lambda: 1_007_000)

    got = readers.read_legacy()
    assert got["agent_ms"] == 7_000
    assert got["session_ms"] == 7_000


def test_legacy_without_running_execution_returns_none(legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="succeed",
                            start=1_000_000, end=1_001_000, usages=[1.0])

    assert readers.read_legacy() is None


def test_legacy_ignores_non_hash_directories(legacy_root):
    (legacy_root / "workspace-sessions").mkdir()
    (legacy_root / "profile.json").write_text("{}", encoding="utf-8")
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=None, usages=[1.0])

    assert readers.read_legacy()["session_id"] == "chat-1"


def test_legacy_missing_root_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "legacy_agent_storage", lambda: tmp_path / "absent")
    assert readers.read_legacy() is None


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def test_auto_prefers_v1_over_stale_legacy_tree(v1_root, legacy_root):
    """An upgrade leaves globalStorage behind; it must not shadow live data."""
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=None, usages=[42.0])

    assert readers.read_stats("auto")["source"] == "kiro-1.x"


def test_auto_falls_back_to_legacy(v1_root, legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=None, usages=[42.0])

    assert readers.read_stats("auto")["source"] == "kiro-0.x"


def test_forced_layouts_do_not_fall_back(v1_root, legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=None, usages=[42.0])

    assert readers.read_stats("v1") is None
    assert readers.read_stats("legacy")["source"] == "kiro-0.x"


def test_unknown_layout_rejected(v1_root):
    with pytest.raises(ValueError, match="unknown layout"):
        readers.read_stats("nope")


def test_auto_with_no_data_anywhere(v1_root, legacy_root):
    assert readers.read_stats("auto") is None


# --------------------------------------------------------------------------
# tool surface
# --------------------------------------------------------------------------

def test_tool_formats_durations(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(18.50437219761194, 387160)])

    got = get_session_stats()
    assert got["credits_used"] == 18.5044
    assert got["agent_time"] == "6m 27s"
    assert got["last_turn_time"] == "6m 27s"
    assert got["session_id"] == "sess_a"
    assert got["source"] == "kiro-1.x"
    assert "note" not in got


def test_tool_notes_in_flight_turn(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 1000)])

    assert "Current turn still running" in get_session_stats()["note"]


def test_tool_notes_ambiguity(v1_root):
    for key, sid, path in [("ws01", "sess_a", r"c:\a"), ("ws02", "sess_b", r"c:\b")]:
        _write_v1_session(v1_root, key, sid, status="in_progress",
                          created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                          workspace_paths=[path])

    assert "Multiple live sessions" in get_session_stats()["note"]
    assert "Multiple live sessions" not in get_session_stats(workspace_path=r"c:\a").get("note", "")


def test_tool_reports_current_turn_time(v1_root, monkeypatch):
    _write_v1_session(v1_root, "ws01", "sess_a", status="in_progress",
                      created="2026-08-31T20:00:00.000Z", turns=[],
                      open_turn_at="2026-08-31T20:05:00.000Z")
    monkeypatch.setattr(readers, "_now_ms", lambda: T0_MS + 300_000 + 42_000)

    got = get_session_stats()
    assert got["current_turn_time"] == "42s"
    assert got["credits_used"] == 0.0
    assert "Current turn still running (42s so far)" in got["note"]
    assert "credits post when it completes" in got["note"]


def test_tool_notes_workflow_step_candidates(v1_root):
    path = r"c:\repos\KiroStats"
    for sid, mode in [("sess_parent", "vibe"), ("sess_step", "wf-planner")]:
        _write_v1_session(v1_root, "ws01", sid, status="in_progress",
                          created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                          workspace_paths=[path], agent_mode=mode)

    note = get_session_stats(workspace_path=path)["note"]
    assert "Multiple live sessions" in note
    assert "sess_step (wf-planner)" in note
    assert "sess_parent (vibe)" in note
    assert "session_id" in note
    assert "Multiple live sessions" not in get_session_stats(
        session_id="sess_parent").get("note", "")


def test_tool_omits_per_turn_fields_on_legacy(v1_root, legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=1_001_000, usages=[1.0])

    got = get_session_stats()
    assert got["source"] == "kiro-0.x"
    assert "last_turn_credits" not in got
    assert "last_turn_time" not in got
    assert "current_turn_time" not in got


def test_tool_reports_no_session(v1_root, legacy_root):
    assert get_session_stats() == {"error": "No active Kiro chat session found."}


def test_tool_rejects_bad_layout(v1_root):
    assert "unknown layout" in get_session_stats(layout="nope")["error"]


def _write_workflow_steps(v1_root):
    path = r"c:\repos\KiroStats"
    _write_v1_session(v1_root, "ws01", "sess_one", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(1.0234, 14_915)], workspace_paths=[path],
                      agent_mode="wf-workflow-creator", markers=True)
    _write_v1_session(v1_root, "ws01", "sess_two", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(0.9383, 11_680), (0.5, 95_366)],
                      workspace_paths=[path], agent_mode="wf-coder", markers=True)


def test_tool_session_ids_array(v1_root):
    _write_workflow_steps(v1_root)

    got = get_session_stats(session_ids=["sess_two", "sess_one"])
    assert [s["session_id"] for s in got["sessions"]] == ["sess_two", "sess_one"]
    two, one = got["sessions"]
    # Each entry matches the single-id output exactly.
    assert two == get_session_stats(session_id="sess_two")
    assert one == get_session_stats(session_id="sess_one")
    assert got["total"] == {
        "credits_used": 2.4617,
        "agent_time": "2m 1s",
        "turns": 3,
        "sessions_counted": 2,
        "by_model": {
            "claude-opus-5": {"credits_used": 2.4617, "turns": 3, "sessions": 2},
        },
    }
    assert "note" not in got


@pytest.mark.parametrize("value", [
    "sess_one, sess_two",
    "sess_one,sess_two",
    "sess_one sess_two",
    '["sess_one", "sess_two"]',
    "sess_one, sess_two, sess_one",
    ["sess_one, sess_two"],
])
def test_tool_session_ids_string(v1_root, value):
    _write_workflow_steps(v1_root)

    got = get_session_stats(session_ids=value)
    assert [s["session_id"] for s in got["sessions"]] == ["sess_one", "sess_two"]
    assert got["total"]["credits_used"] == 2.4617
    assert got["total"]["sessions_counted"] == 2


def test_tool_session_ids_unknown_id_excluded_from_total(v1_root):
    _write_workflow_steps(v1_root)

    got = get_session_stats(session_ids="sess_one, sess_nope")
    assert got["sessions"][1] == {"session_id": "sess_nope", "error": "Session not found."}
    assert got["total"]["credits_used"] == 1.0234
    assert got["total"]["sessions_counted"] == 1
    assert "Not found, excluded from total: sess_nope." in got["note"]


def test_tool_session_ids_flags_running_turn(v1_root, monkeypatch):
    _write_workflow_steps(v1_root)
    _write_v1_session(v1_root, "ws01", "sess_run", status="in_progress",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(0.25, 1_000)], markers=True,
                      open_turn_at="2026-08-31T20:05:00.000Z")
    monkeypatch.setattr(readers, "_now_ms", lambda: T0_MS + 300_000 + 42_000)

    got = get_session_stats(session_ids=["sess_one", "sess_run"])
    run = got["sessions"][1]
    assert run["current_turn_time"] == "42s"
    assert "Current turn still running" in run["note"]
    # Completed turns are counted; the open turn is not.
    assert got["total"]["credits_used"] == 1.2734
    assert got["total"]["agent_time"] == "15s"
    assert "Turn still running in sess_run" in got["note"]


def test_tool_session_ids_total_sums_raw_values(v1_root):
    for sid in ("sess_a", "sess_b"):
        _write_v1_session(v1_root, "ws01", sid, status="idle",
                          created="2026-08-31T20:00:00.000Z",
                          turns=[(1.00004, 1_600)])

    got = get_session_stats(session_ids=["sess_a", "sess_b"])
    # Per-session entries stay rounded as before.
    assert [s["credits_used"] for s in got["sessions"]] == [1.0, 1.0]
    assert [s["agent_time"] for s in got["sessions"]] == ["1s", "1s"]
    # The total rounds the raw sums once: 2.00008 credits, 3,200 ms.
    assert got["total"]["credits_used"] == 2.0001
    assert got["total"]["agent_time"] == "3s"


def test_tool_session_ids_counts_split_turn_once(v1_root):
    _write_v1_log(v1_root, "sess_a", _split_turn("exec-a", 0.8389, 95_366))
    _write_v1_log(v1_root, "sess_b", _split_turn("exec-b", 0.9029, 134_849))

    got = get_session_stats(session_ids=["sess_a", "sess_b"])
    assert [s["turns"] for s in got["sessions"]] == [1, 1]
    assert [s["credits_used"] for s in got["sessions"]] == [0.8389, 0.9029]
    assert got["total"]["turns"] == 2
    assert got["total"]["credits_used"] == 1.7418
    assert got["total"]["agent_time"] == "3m 50s"


def test_tool_session_ids_rejects_bad_input(v1_root):
    _write_workflow_steps(v1_root)

    assert "not both" in get_session_stats(
        session_id="sess_one", session_ids=["sess_two"])["error"]
    assert "no session ids" in get_session_stats(session_ids=" , ")["error"]
    assert "no session ids" in get_session_stats(session_ids=[])["error"]
    assert "unknown layout" in get_session_stats(
        layout="nope", session_ids=["sess_one"])["error"]


# --------------------------------------------------------------------------
# model, effort, agent mode and workflow step
# --------------------------------------------------------------------------

WF = "wf_0123456789abcdef"
REPO = r"c:\repos\KiroStats"


def _step(node_id, *loop, workflow_id=WF, iteration=None):
    """``_meta.kiro.workflow`` as Kiro writes it for a step session."""
    meta = {"workflowId": workflow_id, "workflowName": "demo",
            "nodeId": node_id, "nodePath": [workflow_id, *loop, node_id],
            "type": "step"}
    if iteration is not None:
        meta["iteration"] = iteration
    return meta


def test_v1_reports_model_effort_mode_and_step(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      model="claude-opus-5.5", effort="low", agent_mode="wf-coder",
                      workflow=_step("setup"))

    got = readers.read_v1()
    assert got["model"] == "claude-opus-5.5"
    assert got["effort_level"] == "low"
    assert got["agent_mode"] == "wf-coder"
    assert got["step"] == "setup"
    assert got["step_path"] is None


def test_tool_shows_model_effort_and_agent_mode(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      model="claude-opus-5.5", effort="high")

    got = get_session_stats()
    assert got["model"] == "claude-opus-5.5"
    assert got["effort_level"] == "high"
    assert got["agent_mode"] == "vibe"
    # A chat that is not a workflow step has no step fields.
    assert "step" not in got
    assert "step_path" not in got
    # Existing figures are unchanged.
    assert got["credits_used"] == 1.0
    assert got["turns"] == 1


def test_tool_reports_auto_and_leaves_out_unrecorded_fields(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_auto", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      model="auto")
    _write_v1_session(v1_root, "ws01", "sess_bare", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      model=None, agent_mode=None)

    auto = get_session_stats(session_id="sess_auto")
    assert auto["model"] == "auto"
    assert "effort_level" not in auto
    bare = get_session_stats(session_id="sess_bare")
    for key in ("model", "effort_level", "agent_mode", "step", "step_path"):
        assert key not in bare


def test_tool_omits_model_and_step_fields_on_legacy(v1_root, legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=1_001_000, usages=[1.0])

    got = get_session_stats()
    assert got["source"] == "kiro-0.x"
    for key in ("model", "effort_level", "agent_mode", "step", "step_path"):
        assert key not in got


def test_readers_return_the_same_keys(v1_root, legacy_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)])
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=None, usages=[1.0])

    keys = set(readers._RESULT_DEFAULTS)
    assert set(readers.read_v1()) == keys
    assert set(readers.read_legacy()) == keys


def test_result_rejects_unknown_keys():
    with pytest.raises(KeyError, match="credit"):
        readers._result(credit=1.0)
    # Mutable defaults are fresh per result.
    one = readers._result()
    one["candidates"].append("x")
    assert readers._result()["candidates"] == []


def test_tool_step_path_tells_loop_iterations_apart(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_c0", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      agent_mode="wf-coder",
                      workflow=_step("code", "build-loop", "iter-0", iteration=0))
    _write_v1_session(v1_root, "ws01", "sess_c1", status="idle",
                      created="2026-08-31T20:00:00.000Z", turns=[(1.0, 100)],
                      agent_mode="wf-coder",
                      workflow=_step("code", "build-loop", "iter-1", iteration=1))

    c0 = get_session_stats(session_id="sess_c0")
    c1 = get_session_stats(session_id="sess_c1")
    assert (c0["step"], c0["step_path"]) == ("code", "build-loop/iter-0/code")
    assert (c1["step"], c1["step_path"]) == ("code", "build-loop/iter-1/code")
    assert c0["agent_mode"] == "wf-coder"


def test_tool_session_ids_total_by_model(v1_root):
    _write_v1_session(v1_root, "ws01", "sess_a", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(1.00004, 1_000), (2.0, 1_000)], model="claude-opus-5.5")
    _write_v1_session(v1_root, "ws01", "sess_b", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(0.5, 1_000)], model="claude-haiku-4.5")
    _write_v1_session(v1_root, "ws01", "sess_c", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(1.00004, 1_000)], model="claude-opus-5.5")
    _write_v1_session(v1_root, "ws01", "sess_d", status="idle",
                      created="2026-08-31T20:00:00.000Z",
                      turns=[(0.25, 1_000)], model=None)

    got = get_session_stats(session_ids="sess_a, sess_b, sess_c, sess_d, sess_nope")
    by_model = got["total"]["by_model"]
    assert list(by_model) == ["claude-opus-5.5", "claude-haiku-4.5", "unknown"]
    # Raw sums rounded once, as for the total.
    assert by_model["claude-opus-5.5"] == {"credits_used": 4.0001, "turns": 3, "sessions": 2}
    assert by_model["claude-haiku-4.5"] == {"credits_used": 0.5, "turns": 1, "sessions": 1}
    assert by_model["unknown"] == {"credits_used": 0.25, "turns": 1, "sessions": 1}
    assert got["total"]["credits_used"] == 4.7501
    assert got["total"]["sessions_counted"] == 4


def test_tool_session_ids_by_model_empty_when_none_found(v1_root):
    got = get_session_stats(session_ids="sess_nope")
    assert got["total"]["by_model"] == {}
    assert got["total"]["sessions_counted"] == 0


def _write_workflow_run(v1_root):
    """A main chat, its five step sessions, and a step of another run.

    Directory names sort differently from createdAt to prove the ordering.
    """
    _write_v1_session(v1_root, "ws01", "sess_main", status="in_progress",
                      created="2026-08-31T19:59:00.000Z", turns=[(9.0, 9_000)],
                      workspace_paths=[REPO], model="claude-opus-5.5", effort="high")
    steps = [
        ("sess_e", "20:00:00", _step("create-workflow"), "wf-workflow-creator", "high", (1.0, 1_000)),
        ("sess_d", "20:01:00", _step("setup"), "wf-coder", "low", (0.5, 2_000)),
        ("sess_c", "20:02:00", _step("code", "build-loop", "iter-0", iteration=0), "wf-coder", "high", (3.0, 3_000)),
        ("sess_b", "20:03:00", _step("review", "build-loop", "iter-0", iteration=0), "wf-code-reviewer", "high", (2.0, 4_000)),
        ("sess_a", "20:04:00", _step("code", "build-loop", "iter-1", iteration=1), "wf-coder", "medium", (1.5, 5_000)),
    ]
    for sid, at, workflow, mode, effort, turn in steps:
        _write_v1_session(v1_root, "ws01", sid, status="idle",
                          created=f"2026-08-31T{at}.000Z", turns=[turn],
                          workspace_paths=[REPO], model="claude-opus-5.5",
                          effort=effort, agent_mode=mode, workflow=workflow,
                          root_conversation="sess_main", markers=True)
    _write_v1_session(v1_root, "ws01", "sess_other", status="idle",
                      created="2026-08-31T20:00:30.000Z", turns=[(7.0, 7_000)],
                      workspace_paths=[REPO], agent_mode="wf-coder",
                      workflow=_step("plan", workflow_id="wf_ffffffffffffffff"),
                      root_conversation="sess_main")
    # A chat whose title names the run must not match on text alone.
    meta = v1_root / "ws01" / "sess_main" / "session.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["title"] = f"launch {WF}"
    meta.write_text(json.dumps(data), encoding="utf-8")
    return [s[0] for s in steps]


def test_v1_workflow_session_ids_in_run_order(v1_root):
    expected = _write_workflow_run(v1_root)

    assert readers.v1_workflow_session_ids(WF) == expected
    assert readers.workflow_session_ids(WF) == expected
    assert readers.workflow_session_ids(WF, "legacy") == []
    with pytest.raises(ValueError, match="unknown layout"):
        readers.workflow_session_ids(WF, "nope")


def test_tool_workflow_id_matches_session_ids(v1_root):
    expected = _write_workflow_run(v1_root)

    got = get_session_stats(workspace_path=REPO, workflow_id=WF)
    assert [s["session_id"] for s in got["sessions"]] == expected
    # Same output as passing the step ids by hand; the main chat is left out.
    assert got == get_session_stats(workspace_path=REPO, session_ids=expected)
    assert [(s["step"], s.get("step_path")) for s in got["sessions"]] == [
        ("create-workflow", None), ("setup", None),
        ("code", "build-loop/iter-0/code"), ("review", "build-loop/iter-0/review"),
        ("code", "build-loop/iter-1/code"),
    ]
    assert [s["effort_level"] for s in got["sessions"]] == [
        "high", "low", "high", "high", "medium"]
    assert got["sessions"][2]["agent_mode"] == "wf-coder"
    assert got["total"] == {
        "credits_used": 8.0,
        "agent_time": "15s",
        "turns": 5,
        "sessions_counted": 5,
        "by_model": {"claude-opus-5.5": {"credits_used": 8.0, "turns": 5, "sessions": 5}},
    }


def test_tool_workflow_id_strips_whitespace_and_filters_workspace(v1_root):
    expected = _write_workflow_run(v1_root)

    got = get_session_stats(workflow_id=f"  {WF}\n")
    assert [s["session_id"] for s in got["sessions"]] == expected
    assert get_session_stats(workspace_path=r"c:\elsewhere", workflow_id=WF) == {
        "error": f"No step sessions found for workflow {WF}."}


def test_tool_workflow_id_unknown_run(v1_root):
    _write_workflow_run(v1_root)

    assert get_session_stats(workflow_id="wf_nope") == {
        "error": "No step sessions found for workflow wf_nope."}


def test_tool_workflow_id_rejects_bad_input(v1_root):
    _write_workflow_run(v1_root)

    alone = "Pass workflow_id alone, not with session_id or session_ids."
    assert get_session_stats(workflow_id=WF, session_id="sess_a") == {"error": alone}
    assert get_session_stats(workflow_id=WF, session_ids=["sess_a"]) == {"error": alone}
    assert get_session_stats(workflow_id=" ") == {"error": "workflow_id is empty."}
    assert "unknown layout" in get_session_stats(layout="nope", workflow_id=WF)["error"]
    # Pre-1.0 did not record workflows.
    assert "No step sessions" in get_session_stats(layout="legacy", workflow_id=WF)["error"]


# --------------------------------------------------------------------------
# installer
# --------------------------------------------------------------------------

def test_installer_copies_repo_steering_file():
    """install.ps1 writes the repo's stats.md rather than an embedded copy."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    script = (repo / "install.ps1").read_text(encoding="utf-8")
    steering = (repo / ".kiro" / "steering" / "stats.md").read_bytes()

    assert r'".kiro\steering\stats.md"' in script
    assert "TrimStart([char]0xFEFF)" in script
    assert "inclusion: manual" not in script
    # The source must carry no BOM and open with front matter.
    assert steering.startswith(b"---")
    assert b"workflow_id" in steering


@pytest.mark.parametrize("ms,expected", [
    (0, "0s"), (999, "0s"), (1000, "1s"), (59_000, "59s"),
    (60_000, "1m 0s"), (387_160, "6m 27s"), (3_599_000, "59m 59s"),
    (3_600_000, "1h 0m"), (12_240_000, "3h 24m"),
])
def test_duration_formatting(ms, expected):
    from kiro_stats_mcp.server import _fmt
    assert _fmt(ms) == expected


def test_duration_formatting_passes_through_none():
    from kiro_stats_mcp.server import _fmt
    assert _fmt(None) is None
