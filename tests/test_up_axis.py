import os
import tempfile

import pytest
from build123d import Box, export_step

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _doc():
    from daedalus.core.document import Document
    p = os.path.join(tempfile.mkdtemp(), "b.step")
    export_step(Box(10, 20, 40), p)
    d = Document()
    d.import_file(p)
    return d


@pytest.mark.parametrize("up,ext", [("+Z", 40), ("-Z", 40), ("+Y", 20), ("-Y", 20), ("+X", 10), ("-X", 10)])
def test_up_axis(up, ext):
    d = _doc()
    n = d.active_object
    d.set_up_axis([n], up, floor=True)
    W = d.object_world_points(n)
    assert W[:, 2].min() == pytest.approx(0, abs=1e-6)
    assert W[:, 2].max() == pytest.approx(ext, abs=1e-6)
    d.undo()
    assert d.object_world_points(n)[:, 2].min() == pytest.approx(-20, abs=1e-6)


def test_source_program(tmp_path):
    from daedalus.ui.up_axis import source_program, suggest
    p = tmp_path / "a.step"
    p.write_text("ISO-10303-21;\nHEADER;\nFILE_NAME('a.STEP','2026',('x'),(''),'SwSTEP 2.0','SolidWorks 2024','');\n"
                 "ENDSEC;\n")
    assert source_program(p) == "solidworks"

    class S(dict):
        def value(self, k, d=None):
            return self.get(k, d)
    assert suggest(S(), "solidworks") == "+Y" and suggest(S(), "build123d") == "+Z"
    assert suggest(S({"import/up/solidworks": "-X"}), "solidworks") == "-X"
