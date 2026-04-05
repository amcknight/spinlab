from pathlib import Path
from spinlab.condition_registry import ConditionRegistry, ConditionDef, Scope

def test_loads_single_game_scoped_condition(tmp_path: Path):
    yaml_path = tmp_path / "conditions.yaml"
    yaml_path.write_text(
        "conditions:\n"
        "  - name: powerup\n"
        "    address: 0x0019\n"
        "    size: 1\n"
        "    type: enum\n"
        "    values: { 0: small, 1: big, 2: cape, 3: fire }\n"
        "    scope: game\n"
    )
    reg = ConditionRegistry.from_yaml(yaml_path)
    assert len(reg.definitions) == 1
    d = reg.definitions[0]
    assert d.name == "powerup"
    assert d.address == 0x0019
    assert d.size == 1
    assert d.type == "enum"
    assert d.values == {0: "small", 1: "big", 2: "cape", 3: "fire"}
    assert d.scope == Scope.game()

def test_level_scoped_condition(tmp_path: Path):
    yaml_path = tmp_path / "conditions.yaml"
    yaml_path.write_text(
        "conditions:\n"
        "  - name: yellow_key\n"
        "    address: 0x7E1F2D\n"
        "    size: 1\n"
        "    type: bool\n"
        "    scope: { levels: [42, 17] }\n"
    )
    reg = ConditionRegistry.from_yaml(yaml_path)
    d = reg.definitions[0]
    assert d.scope.levels == [42, 17]
    assert d.scope.is_game_scope is False

def test_in_scope_filtering():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small"}, scope=Scope.game()),
        ConditionDef(name="yellow_key", address=0x7E1F2D, size=1, type="bool",
                     values=None, scope=Scope.levels([42])),
    ])
    assert [d.name for d in reg.in_scope(level=5)] == ["powerup"]
    assert [d.name for d in reg.in_scope(level=42)] == ["powerup", "yellow_key"]

def test_decode_enum():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small", 1: "big"}, scope=Scope.game()),
    ])
    assert reg.decode({"powerup": 1}, level=5) == {"powerup": "big"}

def test_decode_bool():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="on_yoshi", address=0x187A, size=1, type="bool",
                     values=None, scope=Scope.game()),
    ])
    assert reg.decode({"on_yoshi": 0}, level=5) == {"on_yoshi": False}
    assert reg.decode({"on_yoshi": 1}, level=5) == {"on_yoshi": True}

def test_decode_drops_out_of_scope():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small"}, scope=Scope.game()),
        ConditionDef(name="yellow_key", address=0x7E1F2D, size=1, type="bool",
                     values=None, scope=Scope.levels([42])),
    ])
    result = reg.decode({"powerup": 0, "yellow_key": 1}, level=5)
    assert result == {"powerup": "small"}
