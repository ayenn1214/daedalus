"""Every theme loads, applies and has readable text on its panels."""
import os
import subprocess
import sys

import pytest

from daedalus.ui import theme as T


@pytest.mark.parametrize("mode", list(T.THEMES))
def test_theme_loads(mode):
    code = ("from PySide6.QtWidgets import QApplication; a = QApplication([]);"
            "from daedalus.ui import theme as t; t.apply(a);"
            "from PySide6.QtGui import QColor;"
            "lum = lambda c: QColor(c).lightnessF();"
            "assert t.MODE == %r, t.MODE;"
            "assert abs(lum(t.TEXT) - lum(t.PANEL)) > 0.5 and abs(lum(t.ON_ACCENT) - lum(t.ACCENT)) > 0.4;"
            "assert t.SK_FREE == t.ACCENT_LINE; print('ok')" % mode)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=root, timeout=120,
                         env={**os.environ, "DAEDALUS_THEME": mode, "QT_QPA_PLATFORM": "offscreen"})
    assert out.stdout.strip().endswith("ok"), out.stderr[-2000:]
