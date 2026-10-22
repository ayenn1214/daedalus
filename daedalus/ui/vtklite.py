"""Light VTK namespace: loads only the vtkmodules Daedalus uses (``import vtk`` loads all ~100 of them,
which costs most of a second at startup). Unknown names fall back to the full ``vtk`` package."""
from __future__ import annotations

from vtkmodules.vtkCommonCore import *  # noqa: F401,F403
from vtkmodules.vtkCommonDataModel import *  # noqa: F401,F403
from vtkmodules.vtkCommonMath import *  # noqa: F401,F403
from vtkmodules.vtkCommonTransforms import *  # noqa: F401,F403
from vtkmodules.vtkFiltersCore import *  # noqa: F401,F403
from vtkmodules.vtkFiltersGeneral import *  # noqa: F401,F403
from vtkmodules.vtkFiltersGeometry import *  # noqa: F401,F403
from vtkmodules.vtkFiltersSources import *  # noqa: F401,F403
from vtkmodules.vtkIOImage import *  # noqa: F401,F403
from vtkmodules.vtkInteractionStyle import *  # noqa: F401,F403
from vtkmodules.vtkInteractionWidgets import *  # noqa: F401,F403
from vtkmodules.vtkRenderingAnnotation import *  # noqa: F401,F403
from vtkmodules.vtkRenderingCore import *  # noqa: F401,F403
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401  (render backend)
import vtkmodules.vtkRenderingFreeType  # noqa: F401  (text)


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    import vtk as _full
    val = getattr(_full, name)
    globals()[name] = val
    return val
