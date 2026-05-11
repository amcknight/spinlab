"""Typed errors for the NCI client."""


class NCIError(Exception):
    """Base error for all NCI failures."""


class NCITimeout(NCIError):
    """UDP receive timed out before RetroArch responded."""


class NCIProtocolError(NCIError):
    """RetroArch responded but the reply did not match the expected protocol shape."""
