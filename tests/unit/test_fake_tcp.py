"""Meta-test: verify the fake_tcp fixture behavior."""
import pytest

from spinlab.protocol import ReferenceStartCmd


async def test_fake_tcp_records_commands(fake_tcp):
    await fake_tcp.send_command(ReferenceStartCmd(path="/tmp/foo.spinrec"))
    assert len(fake_tcp.sent_commands) == 1
    assert isinstance(fake_tcp.sent_commands[0], ReferenceStartCmd)
    assert fake_tcp.sent_commands[0].path == "/tmp/foo.spinrec"


async def test_fake_tcp_is_connected_default(fake_tcp):
    assert fake_tcp.is_connected is True


async def test_fake_tcp_can_simulate_disconnected(fake_tcp):
    fake_tcp.is_connected = False
    assert fake_tcp.is_connected is False
