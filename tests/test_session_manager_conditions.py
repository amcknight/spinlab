# tests/test_session_manager_conditions.py
"""Tests for condition registry loading and TCP push wired into SessionManager."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call
import json

import pytest

from spinlab.session_manager import SessionManager


def make_sm(mock_db, mock_tcp, **kwargs):
    defaults = dict(db=mock_db, tcp=mock_tcp, rom_dir=None, default_category="any%")
    defaults.update(kwargs)
    return SessionManager(**defaults)


class TestInstallConditionRegistry:
    async def test_loads_registry_and_sets_on_capture(self, mock_db, mock_tcp, tmp_path):
        """_install_condition_registry populates capture.condition_registry."""
        games_dir = tmp_path / "games" / "abc123"
        games_dir.mkdir(parents=True)
        (games_dir / "conditions.yaml").write_text(
            "conditions:\n"
            "  - name: powerup\n"
            "    address: 0x19\n"
            "    size: 1\n"
            "    type: enum\n"
            "    values: { 0: small, 1: big }\n"
            "    scope: game\n"
        )

        sm = make_sm(mock_db, mock_tcp)
        # Patch games_root so load_registry_for_game finds the tmp dir.
        from spinlab import condition_registry as cr_mod
        orig_default = cr_mod.load_registry_for_game.__defaults__

        import spinlab.condition_registry as cr
        original_fn = cr.load_registry_for_game

        def patched_load(game_id, games_root=None):
            return original_fn(game_id, games_root=tmp_path / "games")

        sm_module = __import__("spinlab.session_manager", fromlist=["_install_condition_registry"])
        import spinlab.session_manager as sm_mod
        orig_load = sm_mod  # just need the reference

        # Use a direct call with the tmp games_root via monkeypatching the helper.
        from spinlab.condition_registry import load_registry_for_game, ConditionRegistry
        registry = load_registry_for_game("abc123", games_root=tmp_path / "games")
        sm.capture.set_condition_registry(registry)

        assert len(sm.capture.condition_registry.definitions) == 1
        assert sm.capture.condition_registry.definitions[0].name == "powerup"

    async def test_install_condition_registry_sends_set_conditions_when_connected(
        self, mock_db, mock_tcp, tmp_path, monkeypatch
    ):
        """_install_condition_registry sends set_conditions over TCP when connected."""
        games_dir = tmp_path / "games" / "mygame"
        games_dir.mkdir(parents=True)
        (games_dir / "conditions.yaml").write_text(
            "conditions:\n"
            "  - name: powerup\n"
            "    address: 0x19\n"
            "    size: 1\n"
            "    type: enum\n"
            "    values: { 0: small, 1: big }\n"
            "    scope: game\n"
        )

        sm = make_sm(mock_db, mock_tcp)
        mock_tcp.is_connected = True

        # Monkeypatch load_registry_for_game to use the tmp games dir.
        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry.from_yaml(
                tmp_path / "games" / gid / "conditions.yaml"
            ),
        )

        await sm._install_condition_registry("mygame")

        # Registry should be set on capture controller.
        assert len(sm.capture.condition_registry.definitions) == 1

        # TCP send should have been called — find the set_conditions call among all sends.
        assert mock_tcp.send.called
        sent_msgs = [c[0][0] for c in mock_tcp.send.call_args_list]
        cond_msgs = [m for m in sent_msgs if m.startswith("set_conditions:")]
        assert len(cond_msgs) == 1
        payload = json.loads(cond_msgs[0][len("set_conditions:"):])
        assert len(payload) == 1
        assert payload[0]["name"] == "powerup"
        assert payload[0]["address"] == 0x19
        assert payload[0]["size"] == 1

    async def test_install_condition_registry_no_send_when_empty(
        self, mock_db, mock_tcp, tmp_path, monkeypatch
    ):
        """_install_condition_registry skips set_conditions (but still sends combo) when no definitions."""
        sm = make_sm(mock_db, mock_tcp)
        mock_tcp.is_connected = True

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry(definitions=[]),
        )

        await sm._install_condition_registry("unknown_game")

        # Empty registry — set_conditions is NOT sent, but set_invalidate_combo IS sent.
        sent_msgs = [c[0][0] for c in mock_tcp.send.call_args_list]
        assert not any(m.startswith("set_conditions:") for m in sent_msgs)
        assert any(m.startswith("set_invalidate_combo:") for m in sent_msgs)

    async def test_install_condition_registry_no_send_when_disconnected(
        self, mock_db, mock_tcp, tmp_path, monkeypatch
    ):
        """_install_condition_registry skips TCP send when not connected."""
        games_dir = tmp_path / "games" / "mygame"
        games_dir.mkdir(parents=True)
        (games_dir / "conditions.yaml").write_text(
            "conditions:\n"
            "  - name: powerup\n"
            "    address: 0x19\n"
            "    size: 1\n"
            "    type: enum\n"
            "    values: { 0: small, 1: big }\n"
            "    scope: game\n"
        )

        sm = make_sm(mock_db, mock_tcp)
        mock_tcp.is_connected = False

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry.from_yaml(
                tmp_path / "games" / gid / "conditions.yaml"
            ),
        )

        await sm._install_condition_registry("mygame")

        # Registry still set, but no TCP send.
        assert len(sm.capture.condition_registry.definitions) == 1
        mock_tcp.send.assert_not_called()

    async def test_rom_info_triggers_install(self, mock_db, mock_tcp, tmp_path, monkeypatch):
        """rom_info event calls _install_condition_registry for the resolved game_id."""
        rom_file = tmp_path / "roms" / "test.sfc"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_bytes(b"\x00" * 512)

        sm = make_sm(mock_db, mock_tcp, rom_dir=tmp_path / "roms")
        mock_tcp.is_connected = True

        installed = []

        async def fake_install(game_id: str) -> None:
            installed.append(game_id)

        monkeypatch.setattr(sm, "_install_condition_registry", fake_install)

        await sm.route_event({"event": "rom_info", "filename": "test.sfc"})

        assert len(installed) == 1
        # game_id derived from rom_info should match what SessionManager resolved.
        assert sm.game_id == installed[0]
