"""Tests for CLI dispatch."""
import pytest

from spinlab.cli import _write_ports_file, main


def test_unknown_subcommand_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["notacommand"])
    assert exc.value.code != 0


def test_dashboard_subcommand_imports():
    """Dashboard subcommand is registered and dashboard module is importable."""
    from spinlab import dashboard
    assert hasattr(dashboard, "create_app")


def test_ports_file_includes_vite_port(tmp_path):
    _write_ports_file(tmp_path, dashboard_port=15483, vite_port=5173)
    content = (tmp_path / ".spinlab-ports").read_text()
    assert "vite_port=5173" in content
    assert "dashboard_port=15483" in content
