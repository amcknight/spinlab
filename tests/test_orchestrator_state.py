"""Tests for orchestrator state file writing."""
import json
import pytest
from pathlib import Path
from spinlab.orchestrator import write_state_file, clear_state_file


def test_write_state_file_creates_json(tmp_path):
    state_path = tmp_path / "orchestrator_state.json"
    write_state_file(
        state_path,
        session_id="abc123",
        started_at="2026-03-12T15:30:00Z",
        current_split_id="smw_cod:44:0:normal",
        queue=["smw_cod:56:1:normal", "smw_cod:58:0:key"],
    )
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert data["session_id"] == "abc123"
    assert data["started_at"] == "2026-03-12T15:30:00Z"
    assert data["current_split_id"] == "smw_cod:44:0:normal"
    assert len(data["queue"]) == 2
    assert "updated_at" in data


def test_write_state_file_atomic(tmp_path):
    """The .tmp file should not linger after a successful write."""
    state_path = tmp_path / "orchestrator_state.json"
    write_state_file(state_path, "s1", "2026-03-12T15:30:00Z", "split1", [])
    assert not (tmp_path / "orchestrator_state.json.tmp").exists()


def test_clear_state_file_removes(tmp_path):
    state_path = tmp_path / "orchestrator_state.json"
    write_state_file(state_path, "s1", "2026-03-12T15:30:00Z", "split1", [])
    clear_state_file(state_path)
    assert not state_path.exists()


def test_clear_state_file_noop_if_missing(tmp_path):
    state_path = tmp_path / "orchestrator_state.json"
    clear_state_file(state_path)  # should not raise
