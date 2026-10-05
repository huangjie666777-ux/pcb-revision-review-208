import json

import pytest

from copper_rev208.geometry import build_geometry
from copper_rev208.netcheck import (Hole, NetlistError, Terminal, analyze,
                                    load_netlist)
from copper_rev208.parser import Parser

HEADER = "%FSLAX24Y24*%\n%MOMM*%\n"


def geom_of(body, tol=0.01):
    p = Parser(HEADER + body + "\nM02*\n")
    return build_geometry(p.parse(), p.apertures, tol)


def square(x2, y2, x1=0, y1=0):
    return ("G36*\nX%dY%dD02*\nX%dY%dD01*\nX%dY%dD01*\n"
            "X%dY%dD01*\nX%dY%dD01*\nG37*"
            % (x1, y1, x2, y1, x2, y2, x1, y2, x1, y1))


POUR = square(200000, 100000)


def term(tid, net, layer, x, y):
    return Terminal(tid, net, layer, x, y, "terminals")


def hole(hid, x, y, d, plated):
    return Hole(hid, x, y, d, plated, "holes")


# ---- 网表校验 ----

def test_duplicate_terminal_id_rejected():
    data = {"terminals": [
        {"id": "T1", "net": "A", "layer": "top", "x": 0, "y": 0},
        {"id": "T1", "net": "B", "layer": "top", "x": 1, "y": 1}],
        "holes": []}
    with pytest.raises(NetlistError) as e:
        load_netlist(json.dumps(data).encode())
    assert "重复" in str(e.value)
    assert e.value.position == "terminals[1]"


def test_duplicate_id_across_groups_rejected():
    data = {"terminals": [
        {"id": "X1", "net": "A", "layer": "top", "x": 0, "y": 0}],
        "holes": [{"id": "X1", "x": 0, "y": 0, "diameter": 1, "plated": True}]}
    with pytest.raises(NetlistError):
        load_netlist(json.dumps(data).encode())


def test_non_finite_value_rejected():
    data = {"terminals": [
        {"id": "T1", "net": "A", "layer": "top", "x": float("nan"), "y": 0}],
        "holes": []}
    with pytest.raises(NetlistError) as e:
        load_netlist(json.dumps(data).encode())
    assert "有限" in str(e.value)
    assert e.value.position == "terminals[0]"


def test_bad_layer_and_diameter_rejected():
    base = {"terminals": [], "holes": []}
    bad_layer = dict(base, terminals=[
        {"id": "T1", "net": "A", "layer": "inner", "x": 0, "y": 0}])
    with pytest.raises(NetlistError):
        load_netlist(json.dumps(bad_layer).encode())
    bad_hole = dict(base, holes=[
        {"id": "H1", "x": 0, "y": 0, "diameter": -1, "plated": True}])
    with pytest.raises(NetlistError):
        load_netlist(json.dumps(bad_hole).encode())


def test_overlapping_and_tangent_holes_rejected():
    overlap = {"terminals": [], "holes": [
        {"id": "H1", "x": 0, "y": 0, "diameter": 2, "plated": True},
        {"id": "H2", "x": 1.5, "y": 0, "diameter": 2, "plated": True}]}
    with pytest.raises(NetlistError) as e:
        load_netlist(json.dumps(overlap).encode())
    assert "重叠" in str(e.value)
    tangent = {"terminals": [], "holes": [
        {"id": "H1", "x": 0, "y": 0, "diameter": 2, "plated": True},
        {"id": "H2", "x": 2, "y": 0, "diameter": 2, "plated": True}]}
    with pytest.raises(NetlistError):
        load_netlist(json.dumps(tangent).encode())


def test_invalid_json_rejected():
    with pytest.raises(NetlistError):
        load_netlist(b"{not json")
    with pytest.raises(NetlistError):
        load_netlist(b"\xff\xfe")


# ---- 连通性 ----

def test_plated_hole_connects_layers():
    top = geom_of(POUR)
    bottom = geom_of(POUR)
    terms = [term("T1", "GND", "top", 2, 2),
             term("T2", "GND", "bottom", 2, 8)]
    holes = [hole("H1", 5, 5, 1.0, True)]
    report = analyze(top, bottom, terms, holes, 0.01)
    assert report["ok"]
    assert not report["shorts"] and not report["opens"]
    net = [n for n in report["networks"] if n["terminals"]]
    assert len(net) == 1
    assert net[0]["holes"] == ["H1"]
    assert sorted(net[0]["islands"]) == ["bottom#0", "top#0"]


def test_unplated_hole_does_not_connect():
    top = geom_of(POUR)
    bottom = geom_of(POUR)
    terms = [term("T1", "GND", "top", 2, 2),
             term("T2", "GND", "bottom", 2, 8)]
    holes = [hole("H1", 5, 5, 1.0, False)]
    report = analyze(top, bottom, terms, holes, 0.01)
    assert not report["ok"]
    assert report["opens"] == [{"net": "GND", "groups": [["T1"], ["T2"]]}]


def test_multi_hole_transitive_connection():
    # 三段铜: top 左 -H1- bottom 中 -H2- top 右
    top = geom_of(square(100000, 100000) + "\n"
                  + square(300000, 100000, 200000, 0))
    bottom = geom_of(square(300000, 100000, 100000, 0))
    terms = [term("T1", "A", "top", 5, 5),
             term("T2", "A", "top", 25, 5)]
    holes = [hole("H1", 10, 5, 1.0, True), hole("H2", 20, 5, 1.0, True)]
    report = analyze(top, bottom, terms, holes, 0.01)
    assert report["ok"]
    net = [n for n in report["networks"] if n["terminals"]][0]
    assert net["holes"] == ["H1", "H2"]
    assert len(net["islands"]) == 3


def test_point_touch_islands_not_connected():
    # 两个 10x10 方块仅角点 (10,10) 相接
    top = geom_of(square(100000, 100000) + "\n"
                  + square(200000, 200000, 100000, 100000))
    bottom = geom_of("%ADD10C,0.5*%\nX0Y0D02*")  # 空层
    terms = [term("T1", "A", "top", 5, 5), term("T2", "A", "top", 15, 15)]
    report = analyze(top, bottom, terms, [], 0.01)
    assert report["islands"]["top"] == 2
    assert report["opens"] == [{"net": "A", "groups": [["T1"], ["T2"]]}]


def test_short_report_with_chain():
    top = geom_of(POUR)
    bottom = geom_of(POUR)
    terms = [term("T1", "GND", "top", 2, 2),
             term("T2", "VCC", "bottom", 2, 8)]
    holes = [hole("H1", 5, 5, 1.0, True)]
    report = analyze(top, bottom, terms, holes, 0.01)
    assert not report["ok"]
    assert len(report["shorts"]) == 1
    short = report["shorts"][0]
    assert short["nets"] == ["GND", "VCC"]
    assert short["terminals"] == ["T1", "T2"]
    assert short["chain"] == ["top#0", "H1", "bottom#0"]


def test_unlanded_terminal_listed_separately():
    top = geom_of(POUR)
    bottom = geom_of(POUR)
    terms = [term("T1", "GND", "top", 2, 2),
             term("T9", "GND", "top", 100, 100)]
    report = analyze(top, bottom, terms, [], 0.01)
    assert [t["id"] for t in report["unconnected_terminals"]] == ["T9"]
    assert report["ok"]  # 未落铜端子不参与短/断路分组


def test_terminal_on_boundary_counts_as_copper():
    top = geom_of(POUR)
    bottom = geom_of(POUR)
    terms = [term("T1", "A", "top", 0, 5)]  # 边界 x=0
    report = analyze(top, bottom, terms, [], 0.01)
    assert not report["unconnected_terminals"]


def test_ambiguous_terminal_rejected():
    top = geom_of(square(100000, 100000) + "\n"
                  + square(200000, 200000, 100000, 100000))
    bottom = geom_of(POUR)
    terms = [term("T1", "A", "top", 10, 10)]  # 两岛交点
    with pytest.raises(NetlistError) as e:
        analyze(top, bottom, terms, [], 0.01)
    assert "歧义" in str(e.value)


def test_hole_subtraction_splits_island():
    # 大孔把顶层铜切成左右两岛, 同网端子断路
    top = geom_of(POUR)
    bottom = geom_of(POUR)
    terms = [term("T1", "A", "top", 2, 5), term("T2", "A", "top", 18, 5)]
    holes = [hole("H1", 10, 5, 12.0, False)]
    report = analyze(top, bottom, terms, holes, 0.01)
    assert report["islands"]["top"] == 2
    assert report["opens"] == [{"net": "A", "groups": [["T1"], ["T2"]]}]
