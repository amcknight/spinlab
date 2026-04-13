"""Tests for Vite subprocess management."""
import socket
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from spinlab.vite import wait_for_port, spawn_vite, ViteStartupError


def test_wait_for_port_succeeds_when_listening():
    """wait_for_port returns True when the port accepts connections."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert wait_for_port(port, timeout=2) is True
    finally:
        srv.close()


def test_wait_for_port_fails_on_timeout():
    """wait_for_port returns False when nothing is listening."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    assert wait_for_port(port, timeout=0.3) is False


def test_spawn_vite_starts_subprocess(tmp_path):
    """spawn_vite calls Popen with the right command and cwd."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("spinlab.vite.subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch("spinlab.vite.wait_for_port", return_value=True):
        proc = spawn_vite(tmp_path)

    assert proc is mock_proc
    call_args = mock_popen.call_args
    assert "npm" in call_args[0][0][0]
    assert str(tmp_path) == call_args[1]["cwd"]


def test_spawn_vite_raises_on_port_timeout(tmp_path):
    """spawn_vite raises ViteStartupError when port never opens."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("spinlab.vite.subprocess.Popen", return_value=mock_proc), \
         patch("spinlab.vite.wait_for_port", return_value=False):
        with pytest.raises(ViteStartupError, match="did not start"):
            spawn_vite(tmp_path)

    mock_proc.terminate.assert_called_once()


def test_spawn_vite_raises_on_early_exit(tmp_path):
    """spawn_vite raises ViteStartupError when process exits immediately."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = b"error"

    with patch("spinlab.vite.subprocess.Popen", return_value=mock_proc):
        with pytest.raises(ViteStartupError, match="exited"):
            spawn_vite(tmp_path)
