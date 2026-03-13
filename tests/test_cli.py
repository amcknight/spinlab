"""Tests for CLI dispatch."""
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


def test_dashboard_subcommand_imports():
    """Dashboard subcommand is registered and dashboard module is importable."""
    from spinlab import dashboard
    assert hasattr(dashboard, "create_app")
