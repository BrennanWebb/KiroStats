"""Tests for both Kiro storage-layout readers.

Fixtures mirror the real on-disk shapes captured from Kiro 1.0.337 and 0.12.
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


def _write_v1_session(root, ws_key, sess_id, *, status, created, turns,
                      workspace_paths=None, model="claude-opus-5"):
    sess = root / ws_key / sess_id
    sess.mkdir(parents=True)
    (sess / "session.json").write_text(json.dumps({
        "schemaVersion": "1.0.0",
        "id": sess_id,
        "status": status,
        "createdAt": created,
        "modelId": model,
        "agentMode": "vibe",
        "workspacePaths": workspace_paths or [],
    }), encoding="utf-8")

    lines = []
    for usage, elapsed in turns:
        lines.append(json.dumps(_usage_record(usage, elapsed)))
        # Interleave unrelated records; the reader must skip them.
        lines.append(json.dumps({
            "id": "m", "timestamp": created,
            "payload": {"type": "session_metadata", "key": "contextUsage",
                        "value": {"usagePercentage": 4.2}},
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


def test_tool_omits_per_turn_fields_on_legacy(v1_root, legacy_root):
    _write_legacy_execution(legacy_root, H32_A, H32_B, H32_C,
                            chat_id="chat-1", status="running",
                            start=1_000_000, end=1_001_000, usages=[1.0])

    got = get_session_stats()
    assert got["source"] == "kiro-0.x"
    assert "last_turn_credits" not in got
    assert "last_turn_time" not in got


def test_tool_reports_no_session(v1_root, legacy_root):
    assert get_session_stats() == {"error": "No active Kiro chat session found."}


def test_tool_rejects_bad_layout(v1_root):
    assert "unknown layout" in get_session_stats(layout="nope")["error"]


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
