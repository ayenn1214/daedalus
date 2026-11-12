"""Icarus. Imports are lazy: the tool layer loads the CAD kernel, which the inventory-only window never needs."""


def __getattr__(name):
    if name in ("TOOLS", "ToolExecutor"):
        from . import tools
        return getattr(tools, name)
    if name == "Copilot":
        from .copilot import Copilot
        return Copilot
    raise AttributeError(name)


__all__ = ["TOOLS", "ToolExecutor", "Copilot"]
