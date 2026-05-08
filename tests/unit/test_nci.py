"""Unit tests for the NCI client (python/spinlab/retroarch/nci.py).

Covers fire-and-forget commands and request/reply commands by monkeypatching
the low-level _send / _send_no_reply methods so no socket is needed.
"""
import pytest

from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.exceptions import NCIProtocolError


# ---------------------------------------------------------------------------
# BSV fire-and-forget commands
# ---------------------------------------------------------------------------


def test_bsv_record_toggle_sends_command(monkeypatch):
    sent = []
    client = NCIClient()
    monkeypatch.setattr(client, "_send_no_reply", lambda cmd: sent.append(cmd))
    client.bsv_record_toggle()
    assert sent == ["BSV_RECORD_TOGGLE"]


def test_bsv_play_sends_command(monkeypatch):
    sent = []
    client = NCIClient()
    monkeypatch.setattr(client, "_send_no_reply", lambda cmd: sent.append(cmd))
    client.bsv_play()
    assert sent == ["MOVIE_PLAYBACK_TOGGLE"]


def test_bsv_stop_sends_command(monkeypatch):
    # MOVIE_PLAYBACK_TOGGLE again, since libretro models playback as a toggle
    sent = []
    client = NCIClient()
    monkeypatch.setattr(client, "_send_no_reply", lambda cmd: sent.append(cmd))
    client.bsv_stop()
    assert sent == ["MOVIE_PLAYBACK_TOGGLE"]


# ---------------------------------------------------------------------------
# GET_CONFIG_PARAM request/reply
# ---------------------------------------------------------------------------


def test_get_config_param_parses_reply(monkeypatch):
    client = NCIClient()
    monkeypatch.setattr(
        client,
        "_send",
        lambda cmd: 'GET_CONFIG_PARAM movie_directory "C:/RetroArch-Win64/states"',
    )
    assert client.get_config_param("movie_directory") == "C:/RetroArch-Win64/states"


def test_get_config_param_strips_quotes(monkeypatch):
    """Values returned by RA are quoted; the method strips them."""
    client = NCIClient()
    monkeypatch.setattr(
        client,
        "_send",
        lambda cmd: 'GET_CONFIG_PARAM savestate_directory "/home/user/saves"',
    )
    assert client.get_config_param("savestate_directory") == "/home/user/saves"


def test_get_config_param_unquoted_value(monkeypatch):
    """A value without quotes is returned as-is."""
    client = NCIClient()
    monkeypatch.setattr(
        client,
        "_send",
        lambda cmd: "GET_CONFIG_PARAM some_key somevalue",
    )
    assert client.get_config_param("some_key") == "somevalue"


def test_get_config_param_short_reply_raises(monkeypatch):
    """A reply with fewer than 3 parts raises NCIProtocolError."""
    client = NCIClient()
    monkeypatch.setattr(client, "_send", lambda cmd: "GET_CONFIG_PARAM only_two")
    with pytest.raises(NCIProtocolError, match="GET_CONFIG_PARAM reply too short"):
        client.get_config_param("only_two")


def test_get_config_param_sends_correct_command(monkeypatch):
    """The command sent to RA includes the key name."""
    sent = []
    client = NCIClient()

    def fake_send(cmd: str) -> str:
        sent.append(cmd)
        return f'GET_CONFIG_PARAM {cmd.split()[1]} "some_value"'

    monkeypatch.setattr(client, "_send", fake_send)
    client.get_config_param("movie_directory")
    assert sent == ["GET_CONFIG_PARAM movie_directory"]
