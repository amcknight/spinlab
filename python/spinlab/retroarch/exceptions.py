"""Typed errors for the NCI client."""


class NCIError(Exception):
    """Base error for all NCI failures."""


class NCITimeout(NCIError):
    """UDP receive timed out before RetroArch responded."""


class NCIProtocolError(NCIError):
    """RetroArch responded but the reply did not match the expected protocol shape."""


class NCICoreFrozen(NCIError):
    """NCI service is responsive but the emulator core thread isn't advancing frames.

    Detected by the running-state probe (NCIClient.is_core_running). Surfaces
    the spike-discovered deep-pause state where READ_CORE_RAM continues to work
    but the game is frozen.
    """
