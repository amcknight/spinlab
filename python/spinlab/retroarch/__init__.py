"""RetroArch integration: RAClient (NCI + state + movie + hotkey) + Poller."""

from spinlab.retroarch.exceptions import (
    NCIError,
    NCIProtocolError,
    NCITimeout,
)
from spinlab.retroarch.responses import StatusInfo

__all__ = [
    "NCIError",
    "NCIProtocolError",
    "NCITimeout",
    "StatusInfo",
]
