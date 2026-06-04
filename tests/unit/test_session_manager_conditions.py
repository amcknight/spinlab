"""Tests for condition registry loading and backend command wired into SessionManager.

Uses real SQLite Database + FakeEmuBackend so the SessionManager constructor
isn't being threaded through MagicMock objects whose call patterns drift.
"""
import pytest
from tests.conftest import FakeEmuBackend

from spinlab.db import Database
from spinlab.protocol import RomInfoEvent, SetConditionsCmd
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "cond.db")


@pytest.fixture
def emu():
    return FakeEmuBackend(connected=True)


def _make_sm(db, emu, **kwargs) -> SessionManager:
    defaults = dict(db=db, emu=emu, rom_dir=None, default_category="any%")
    defaults.update(kwargs)
    return SessionManager(**defaults)


class TestInstallConditionRegistry:
    async def test_loads_registry_and_sets_on_capture(self, db, emu, tmp_path):
        """The loaded YAML registry is installed on the capture controller."""
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

        sm = _make_sm(db, emu)
        from spinlab.condition_registry import load_registry_for_game
        registry = load_registry_for_game("abc123", games_root=tmp_path / "games")
        sm.capture.set_condition_registry(registry)

        assert len(sm.capture.condition_registry.definitions) == 1
        assert sm.capture.condition_registry.definitions[0].name == "powerup"
        # set_condition_registry must propagate the same instance to the
        # recorder, otherwise a game-switch leaves the recorder decoding
        # against a stale registry.
        assert sm.capture.recorder._condition_registry is registry

    async def test_install_condition_registry_sends_set_conditions_when_connected(
        self, db, emu, tmp_path, monkeypatch,
    ):
        """install_condition_registry sends SetConditionsCmd when emu is connected."""
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

        sm = _make_sm(db, emu)

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry.from_yaml(
                tmp_path / "games" / gid / "conditions.yaml"
            ),
        )

        await sm.install_condition_registry("mygame")

        assert len(sm.capture.condition_registry.definitions) == 1

        cond_cmds = [c for c in emu.sent_commands if isinstance(c, SetConditionsCmd)]
        assert len(cond_cmds) == 1
        payload = cond_cmds[0].definitions
        assert len(payload) == 1
        assert payload[0].name == "powerup"
        assert payload[0].address == 0x19
        assert payload[0].size == 1

    async def test_install_condition_registry_no_send_when_empty(
        self, db, emu, tmp_path, monkeypatch,
    ):
        """No backend send when the registry is empty (even if connected)."""
        sm = _make_sm(db, emu)

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry(definitions=[]),
        )

        await sm.install_condition_registry("unknown_game")

        assert not any(isinstance(c, SetConditionsCmd) for c in emu.sent_commands)

    async def test_install_condition_registry_no_send_when_disconnected(
        self, db, emu, tmp_path, monkeypatch,
    ):
        """Backend send is skipped when emu is disconnected; registry still installed."""
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

        emu.is_connected = False
        sm = _make_sm(db, emu)

        import spinlab.condition_registry as cr_mod
        monkeypatch.setattr(
            cr_mod,
            "load_registry_for_game",
            lambda gid, games_root=None: cr_mod.ConditionRegistry.from_yaml(
                tmp_path / "games" / gid / "conditions.yaml"
            ),
        )

        await sm.install_condition_registry("mygame")

        assert len(sm.capture.condition_registry.definitions) == 1
        assert not any(isinstance(c, SetConditionsCmd) for c in emu.sent_commands)

    async def test_rom_info_triggers_install(self, db, emu, tmp_path, monkeypatch):
        """rom_info event calls install_condition_registry for the resolved game_id."""
        rom_file = tmp_path / "roms" / "test.sfc"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_bytes(b"\x00" * 512)

        sm = _make_sm(db, emu, rom_dir=tmp_path / "roms")

        installed = []

        async def fake_install(game_id: str) -> None:
            installed.append(game_id)

        monkeypatch.setattr(sm, "install_condition_registry", fake_install)

        await sm.route_event(RomInfoEvent(filename="test.sfc"))

        assert len(installed) == 1
        # game_id derived from rom_info should match what SessionManager resolved.
        assert sm.game_id == installed[0]


def test_set_condition_registry_raises_if_called_while_recording(tmp_path):
    """The install invariant says: install_condition_registry runs BEFORE
    recording starts (route_event serializes event handlers, so ROM-load
    always lands before reference/start). Replacing the registry mid-record
    would silently mismatch decode shape vs. recorder buffer state.
    Assert to surface a future code path that violates this."""
    from spinlab.capture.reference import ReferenceController
    from spinlab.condition_registry import ConditionRegistry
    from spinlab.db import Database

    db = Database(tmp_path / "ref.db")
    db.upsert_game("g", "Game", "any%")
    rc = ReferenceController(db=db, emu=None)  # type: ignore[arg-type]
    # Simulate active recording state.
    rc.recorder.capture_run_id = "active-run"
    new_registry = ConditionRegistry()  # empty, distinct instance

    import pytest as _pytest
    with _pytest.raises(AssertionError, match="ConditionRegistry"):
        rc.set_condition_registry(new_registry)
