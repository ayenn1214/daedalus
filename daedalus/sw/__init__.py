"""SolidWorks Link: Daedalus subsystems (inventory, standard parts, fasteners, copilot, δ hotbar, analysis)
working on the open SolidWorks session. 3D modelling stays in SolidWorks."""
from .backend import ComBackend, FakeBackend, SWError, connect  # noqa: F401
from .link import SWLink  # noqa: F401
