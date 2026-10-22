from build123d import Box

from daedalus.core.selectors import at_expr, select_edges, select_face


def test_at_edge_and_face():
    b = Box(20, 10, 5)
    e = select_edges(b, "at(10,0,2.5)")
    assert len(e) == 1 and abs(e[0].length - 10) < 1e-9
    assert len(select_edges(b, at_expr([(10, 0, 2.5), (-10, 5, 0)]))) == 2
    assert abs(select_face(b, "at(0,0,2.5)").center().Z - 2.5) < 1e-9


def test_fillet_and_shell_from_picks():
    from daedalus.core.document import Document
    import tempfile, os
    from build123d import export_step
    p = os.path.join(tempfile.mkdtemp(), "b.step")
    export_step(Box(60, 40, 20), p)
    d = Document()
    d.import_file(p)
    v0 = d.result.bodies[d.active_object].volume
    d.add("fillet", {"edges": at_expr([(-30, -20, 0), (30, -20, 0)]), "radius": 3})
    assert not d.result.errors
    v1 = d.result.bodies[d.active_object].volume
    assert v1 < v0
    d.add("shell", {"open_faces": "at(0,0,10),at(0,-20,0)", "thickness": 2})
    assert not d.result.errors
