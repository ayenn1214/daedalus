"""Exceptions shared by every module (importing this never loads the CAD kernel)."""


class FeatureError(RuntimeError):
    """A modelling / document operation failed with a message for the user."""
