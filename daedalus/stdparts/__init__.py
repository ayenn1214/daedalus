"""Standard parts library (see library.py)."""
from .library import KINDS, FIELDS, LibraryError, StandardLibrary, default_root, object_document  # noqa: F401


def get_library() -> StandardLibrary:
    """The library folder from settings (Standard parts ▸ Library folder…), else the default."""
    path = None
    try:
        from PySide6.QtCore import QSettings
        path = QSettings("Daedalus", "Daedalus").value("stdparts/path") or None
    except Exception:  # noqa: BLE001
        pass
    return StandardLibrary(path)
