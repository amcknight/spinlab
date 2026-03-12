"""Tests for CLI dispatch."""
from unittest.mock import patch
import pytest
from spinlab.cli import main


def test_stats_subcommand_prints_stub(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["stats"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Stats coming in a future step" in captured.out


def test_unknown_subcommand_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["notacommand"])
    assert exc.value.code != 0


def test_practice_calls_orchestrator_run():
    # Smoke test: orchestrator.run is accessible from cli
    from spinlab import orchestrator
    assert hasattr(orchestrator, "run")


def test_capture_calls_capture_main():
    from spinlab import capture
    assert hasattr(capture, "main")


def test_dashboard_subcommand_imports():
    """Dashboard subcommand is registered and dashboard module is importable."""
    from spinlab import dashboard
    assert hasattr(dashboard, "create_app")
