"""Tests for CLI startup-error handling.

Catches the failure mode where a dashboard launched via AHK (minimized window)
crashes during config parsing and the error vanishes from view. The bootstrap
path must (a) print to stderr with an actionable message and (b) write the
details to a stable temp file regardless of how the process was started.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from spinlab import cli


def test_load_config_or_die_missing_file_exits_with_helpful_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bogus = tmp_path / "nonexistent.yaml"
    with pytest.raises(SystemExit) as exc_info:
        cli._load_config_or_die(str(bogus))

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "Config not found" in captured.err
    # The absolute path must appear so the user can copy-paste it.
    assert str(bogus.resolve()) in captured.err
    # The hint mentions copying the example.
    assert "config.example.yaml" in captured.err
    # And points at the temp-log path.
    assert "Details also written to:" in captured.err


def test_load_config_or_die_writes_temp_log_on_missing(tmp_path: Path) -> None:
    bogus = tmp_path / "nonexistent.yaml"
    with pytest.raises(SystemExit):
        cli._load_config_or_die(str(bogus))

    # At least one spinlab-startup-* file should have been created in temp.
    temp_dir = Path(tempfile.gettempdir())
    matches = list(temp_dir.glob("spinlab-startup-*.log"))
    assert matches, "no startup-error log was written to temp dir"

    # The most-recent one should mention the missing config.
    most_recent = max(matches, key=lambda p: p.stat().st_mtime)
    body = most_recent.read_text(encoding="utf-8")
    assert "config not found" in body
    assert str(bogus.resolve()) in body or str(bogus) in body


def test_write_startup_error_includes_traceback(tmp_path: Path) -> None:
    """The dump must include a traceback so post-mortem debugging works."""
    try:
        raise RuntimeError("intentional test failure")
    except RuntimeError as exc:
        path = cli._write_startup_error(exc, "unit test")

    body = path.read_text(encoding="utf-8")
    assert "intentional test failure" in body
    assert "RuntimeError" in body
    assert "Traceback" in body
    assert "argv" in body  # captured for context
    assert "cwd" in body


def test_write_startup_error_swallows_temp_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: even if the temp write fails, _write_startup_error must not raise.

    We're already in an error path; raising again would mask the original cause.
    """
    def fake_open(*_args, **_kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(Path, "open", fake_open)
    try:
        raise ValueError("primary")
    except ValueError as exc:
        # Must not raise.
        result = cli._write_startup_error(exc, "boom")

    # Returns *something* (the sentinel path, which we don't strictly check).
    assert result is not None
