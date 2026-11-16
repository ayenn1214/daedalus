"""Parts with the same name but different part numbers are different parts (mirror parts, twin weldments)."""
from daedalus.inventory import model as M


def test_same_part_rules():
    a = M.new_part(name="FLAT, SCISSOR, WING", partNumber="M10-0050", type="manufactured")
    assert M.same_part(a, "flat, scissor, wing", "")                    # no number given: name decides
    assert M.same_part(a, "other name", "m10-0050")                      # number decides
    assert not M.same_part(a, "FLAT, SCISSOR, WING", "M10-0051")         # different numbers: different parts
    assert not M.pn_differs("", "M10-0051") and M.pn_differs("M10-0050", "M10-0051")


def test_duplicate_groups_skip_different_numbers():
    inv = M.Inventory()
    inv.parts = [M.new_part(id="a", name="WING", partNumber="M10-0050"),
                 M.new_part(id="b", name="WING", partNumber="M10-0051"),
                 M.new_part(id="c", name="Bolt"), M.new_part(id="d", name="bolt"),
                 M.new_part(id="e", name="Tab", partNumber="M10-0005"), M.new_part(id="f", name="Tab")]
    groups = [{p["id"] for p in g} for g in inv.duplicate_groups()]
    assert {"c", "d"} in groups and {"e", "f"} in groups
    assert not any("a" in g and "b" in g for g in groups)


def test_import_keeps_both_when_numbers_differ():
    inv = M.Inventory()
    rows = [{"Name": "WLDMT, SCISSOR ASSM", "Type": "manufactured", "Part Number": "M30-0008"},
            {"Name": "WLDMT, SCISSOR ASSM", "Type": "manufactured", "Part Number": "M30-0009"},
            {"Name": "WING", "Type": "manufactured", "Part Number": "M10-0050"},
            {"Name": "Frame", "Type": "manufactured", "Part Number": "M30-0010", "Weldment": "yes",
             "Components": "M10-0050 x2"}]
    added, updated = inv.import_rows(rows)
    assert added == 4 and len([p for p in inv.parts if p["name"] == "WLDMT, SCISSOR ASSM"]) == 2
    fr = next(p for p in inv.parts if p["name"] == "Frame")
    assert fr["components"][0]["partId"] == next(p["id"] for p in inv.parts if p["partNumber"] == "M10-0050")
    # same number again updates instead of adding
    added, updated = inv.import_rows([{"Name": "WLDMT, SCISSOR ASSM (renamed)", "Type": "manufactured",
                                       "Part Number": "M30-0008"}])
    assert added == 0 and updated == 1
