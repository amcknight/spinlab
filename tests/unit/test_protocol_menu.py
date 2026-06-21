"""The R-menu protocol surface: command + armed events."""
from spinlab.protocol import (
    ControllerCommandEvent,
    ControllerMenuArmedEvent,
)


def test_controller_command_defaults_and_field():
    assert ControllerCommandEvent().command == ""
    assert ControllerCommandEvent(command="pause").command == "pause"


def test_controller_menu_armed_event():
    assert ControllerMenuArmedEvent(armed=True).armed is True
    assert ControllerMenuArmedEvent(armed=False).armed is False
