from pathlib import Path
from spinlab.condition_registry import ConditionRegistry, load_registry_for_game


def test_loads_registry_from_games_directory(tmp_path: Path):
    games_dir = tmp_path / "games" / "g1"
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
    reg = load_registry_for_game("g1", games_root=tmp_path / "games")
    assert len(reg.definitions) == 1
    assert reg.definitions[0].name == "powerup"


def test_missing_registry_returns_empty(tmp_path: Path):
    reg = load_registry_for_game("nonexistent", games_root=tmp_path / "empty")
    assert reg.definitions == []
