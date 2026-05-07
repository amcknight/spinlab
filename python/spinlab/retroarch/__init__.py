"""RetroArch integration: NCI client, polling, savestate I/O, BSV adapter."""

from spinlab.retroarch.exceptions import (
    NCICoreFrozen,
    NCIError,
    NCIProtocolError,
    NCITimeout,
)

__all__ = ["NCICoreFrozen", "NCIError", "NCIProtocolError", "NCITimeout"]
