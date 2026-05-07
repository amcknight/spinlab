import pytest

from spinlab.retroarch.conditions import ConditionRegistry, ConditionSpec
from spinlab.retroarch.nci import NCIClient


def test_register_and_read(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM 100 1", "READ_CORE_RAM 100 0e\n")
    fake_nci_server.handle("READ_CORE_RAM 200 2", "READ_CORE_RAM 200 fe ca\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    reg = ConditionRegistry()
    reg.set([
        ConditionSpec(name="game_mode", address=0x100, size=1),
        ConditionSpec(name="counter", address=0x200, size=2),
    ])

    values = reg.read_all(client)
    assert values == {"game_mode": 0x0E, "counter": 0xCAFE}


def test_replacing_set_overrides_previous(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM 100 1", "READ_CORE_RAM 100 01\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    reg = ConditionRegistry()
    reg.set([ConditionSpec(name="a", address=0x999, size=1)])
    reg.set([ConditionSpec(name="b", address=0x100, size=1)])
    assert reg.read_all(client) == {"b": 0x01}


def test_unsupported_size_raises():
    reg = ConditionRegistry()
    with pytest.raises(ValueError, match="size"):
        reg.set([ConditionSpec(name="bad", address=0x100, size=4)])


def test_empty_registry_returns_empty(fake_nci_server):
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    reg = ConditionRegistry()
    assert reg.read_all(client) == {}
