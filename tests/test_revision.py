import math

import pytest
from shapely.geometry import Point

from copper_rev208.drc import audit
from copper_rev208.geometry import build_geometry
from copper_rev208.netcheck import Hole, Terminal, connectivity
from copper_rev208.parser import Parser
from copper_rev208.revision import (RevisionError, review_revision,
                                    transform_geometry, transform_holes,
                                    transform_terminals,
                                    validate_matching_netlists)
from copper_rev208.revision_svg import copper_changes_to_svg

HEADER = "%FSLAX24Y24*%\n%MOMM*%\n"


def geom_of(body):
    parser = Parser(HEADER + body + "\nM02*\n")
    return build_geometry(parser.parse(), parser.apertures, 0.001)


def square(x1, y1, x2, y2):
    vals = (x1 * 10000, y1 * 10000, x2 * 10000, y1 * 10000,
            x2 * 10000, y2 * 10000, x1 * 10000, y2 * 10000,
            x1 * 10000, y1 * 10000)
    return ("G36*\nX%dY%dD02*\nX%dY%dD01*\nX%dY%dD01*\n"
            "X%dY%dD01*\nX%dY%dD01*\nG37*" % vals)


def term(tid, net, x, y, layer="top"):
    return Terminal(tid, net, layer, x, y, "terminals")


def hle(hid, x, y, diameter=0.5):
    return Hole(hid, x, y, diameter, False, "holes")


def layers(top, bottom=None):
    return {"top": top, "bottom": bottom if bottom is not None else geom_of("")}


def test_rotate_then_translate_copper_terminals_and_holes():
    moved = transform_geometry(Point(1, 0).buffer(0.1), 2, 3, 90)
    assert moved.centroid.x == pytest.approx(2)
    assert moved.centroid.y == pytest.approx(4)
    assert transform_terminals([term("T1", "A", 1, 0)], 2, 3, 90)[0].y == 4
    assert transform_holes([hle("H1", 1, 0)], 2, 3, 270)[0].y == 2


def test_id_net_and_layer_mismatch_rejects_whole_order():
    old = [term("T1", "A", 0, 0), term("T2", "B", 1, 1)]
    with pytest.raises(RevisionError):
        validate_matching_netlists(old, [term("T1", "A", 0, 0)])
    with pytest.raises(RevisionError):
        validate_matching_netlists(old, [term("T1", "C", 0, 0),
                                         term("T2", "B", 1, 1)])
    with pytest.raises(RevisionError):
        validate_matching_netlists(
            [term("T1", "A", 0, 0)], [term("T1", "A", 0, 0, "bottom")])


def test_copper_difference_area_regions_boundaries_holes_empty_layer():
    copper_old = geom_of(square(0, 0, 10, 10))
    copper_new = geom_of(square(5, 0, 15, 10))
    result = review_revision(layers(copper_old), layers(copper_new),
                             ([], []), ([], []), (0.2, {}, []),
                             0, 0, 0, 0.001)
    top = result["copper_changes"]["top"]
    assert top["added_copper"]["area_mm2"] == 50
    assert top["removed_copper"]["region_count"] == 1
    assert top["common_copper"]["area_mm2"] == 50
    assert top["added_copper"]["boundaries"][0]["interiors"] == []
    assert result["copper_changes"]["bottom"]["removed_copper"]["area_mm2"] == 0
    svg = copper_changes_to_svg(result["_svg_layers"])
    assert "#2ca02c" in svg and "#d62728" in svg and "#7f7f7f" in svg


def test_rotation_aligns_without_layer_swap():
    result = review_revision(
        layers(geom_of(square(0, 0, 10, 2))),
        layers(geom_of(square(0, -10, 2, 0))),
        ([], []), ([], []), (0.2, {}, []), 0, 0, 90, 0.001)
    top = result["copper_changes"]["top"]
    assert top["added_copper"]["area_mm2"] == 0
    assert top["removed_copper"]["area_mm2"] == 0
    assert top["common_copper"]["area_mm2"] == 20


def test_connection_and_landing_changes_use_stable_terminal_ids():
    old_terms = [term("T1", "A", 1, 1), term("T2", "A", 29, 1),
                 term("T3", "A", 25, 1)]
    new_terms = [term("T1", "A", 1, 1), term("T2", "A", 29, 1),
                 term("T3", "A", 100, 100)]
    old_top = geom_of(square(0, 0, 10, 10) + "\n" + square(20, 0, 30, 10))
    new_top = geom_of(square(0, 0, 30, 10))
    result = review_revision(layers(old_top), layers(new_top),
                             (old_terms, []), (new_terms, []),
                             (0.2, {}, []), 0, 0, 0, 0.001)
    changes = result["connectivity_changes"]
    assert changes["added_connections"] == [["T1", "T2"]]
    assert changes["lost_connections"] == []
    assert changes["landing_status_changes"] == {
        "gained_copper": [], "lost_copper": ["T3"]}
    assert changes["compared_terminals"] == ["T1", "T2"]


def test_old_floating_terminal_is_not_new_disconnection():
    copper = geom_of(square(0, 0, 10, 10))
    terms = [term("T1", "A", 1, 1), term("T2", "A", 100, 1)]
    result = review_revision(layers(copper), layers(copper),
                             (terms, []), (terms, []), (0.2, {}, []),
                             0, 0, 0, 0.001)
    changes = result["connectivity_changes"]
    assert changes["added_connections"] == []
    assert changes["lost_connections"] == []
    assert changes["compared_terminals"] == ["T1"]


def test_transformed_holes_are_subtracted_and_preserved_in_boundaries():
    copper = geom_of(square(0, 0, 10, 10))
    native_new_copper = geom_of(square(0, -10, 10, 0))
    terms = [term("T1", "A", 1, 1)]
    result = review_revision(
        layers(copper), layers(native_new_copper),
        (terms, [hle("H1", 5, 5, 2)]),
        (terms, [hle("H1", 5, -5, 2)]), (0.2, {}, []), 0, 0, 90, 0.001)
    common = result["copper_changes"]["top"]["common_copper"]
    assert common["area_mm2"] == pytest.approx(100 - math.pi, abs=0.002)
    assert common["boundaries"][0]["interiors"]


def test_multi_name_uncovered_pair_uses_default_threshold():
    top = geom_of(square(0, 0, 10, 10) + "\n" + square(10.3, 0, 20, 10))
    terms = [term("T1", "A", 1, 1), term("T2", "C", 1, 8),
             term("T3", "B", 19, 1), term("T4", "D", 19, 8)]
    conn = connectivity(top, geom_of(""), terms, [], 0.001)
    result = audit(conn, terms, 0.5,
                   {frozenset(("A", "B")): 0.2}, [])
    assert result["clearance_violations"][0]["threshold_mm"] == 0.5
