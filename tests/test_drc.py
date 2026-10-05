import json

import pytest

from copper_rev208.drc import RuleError, audit, load_rules
from copper_rev208.errors import GerberError
from copper_rev208.geometry import build_geometry
from copper_rev208.netcheck import Hole, Terminal, connectivity
from copper_rev208.parser import Parser

HEADER = "%FSLAX24Y24*%\n%MOMM*%\n"


def geom_of(body, tol=0.01):
    p = Parser(HEADER + body + "\nM02*\n")
    return build_geometry(p.parse(), p.apertures, tol)


def square(x2, y2, x1=0, y1=0):
    return ("G36*\nX%dY%dD02*\nX%dY%dD01*\nX%dY%dD01*\n"
            "X%dY%dD01*\nX%dY%dD01*\nG37*"
            % (x1, y1, x2, y1, x2, y2, x1, y2, x1, y1))


def term(tid, net, layer, x, y):
    return Terminal(tid, net, layer, x, y, "terminals")


def hole(hid, x, y, d, plated):
    return Hole(hid, x, y, d, plated, "holes")


def rules(obj, known=("A", "B", "C")):
    return load_rules(json.dumps(obj).encode(), set(known))


def conn_of(top, bottom, terms, holes=(), tol=0.01):
    return connectivity(top, bottom, list(terms), list(holes), tol)


# 顶层两个 10x5 铜块, 间隙 0.3mm
TOP_TWO = square(100000, 50000) + "\n" + square(203000, 50000, 103000, 0)
EMPTY = "%ADD10C,0.5*%\nX0Y0D02*"


# ---- 规则校验 ----

def test_default_clearance_must_be_positive_finite():
    with pytest.raises(RuleError):
        rules({"default_clearance_mm": 0})
    with pytest.raises(RuleError):
        rules({"default_clearance_mm": -0.1})
    with pytest.raises(RuleError):
        rules({"default_clearance_mm": float("nan")})
    with pytest.raises(RuleError):
        rules({})


def test_duplicate_name_pair_rejected_unordered():
    data = {"default_clearance_mm": 0.2, "clearance_overrides": [
        {"nets": ["A", "B"], "clearance_mm": 0.5},
        {"nets": ["B", "A"], "clearance_mm": 0.6}]}
    with pytest.raises(RuleError) as e:
        rules(data)
    assert "重复" in str(e.value)
    assert e.value.position == "clearance_overrides[1]"


def test_unknown_net_name_rejected():
    data = {"default_clearance_mm": 0.2, "clearance_overrides": [
        {"nets": ["A", "ZZZ"], "clearance_mm": 0.5}]}
    with pytest.raises(RuleError) as e:
        rules(data)
    assert "未知网络名" in str(e.value)


def test_override_threshold_must_be_positive_finite():
    for bad in (0, -1, float("inf"), "0.5"):
        data = {"default_clearance_mm": 0.2, "clearance_overrides": [
            {"nets": ["A", "B"], "clearance_mm": bad}]}
        with pytest.raises(RuleError):
            rules(data)


def test_keepout_id_layer_and_polygon_validated():
    base = {"default_clearance_mm": 0.2}
    with pytest.raises(RuleError):  # 重复 ID
        rules(dict(base, keepouts=[
            {"id": "K1", "layer": "top",
             "polygon": [[0, 0], [1, 0], [1, 1]]},
            {"id": "K1", "layer": "bottom",
             "polygon": [[0, 0], [1, 0], [1, 1]]}]))
    with pytest.raises(RuleError):  # 非法层
        rules(dict(base, keepouts=[
            {"id": "K1", "layer": "inner",
             "polygon": [[0, 0], [1, 0], [1, 1]]}]))
    with pytest.raises(RuleError) as e:  # 自交
        rules(dict(base, keepouts=[
            {"id": "K1", "layer": "top",
             "polygon": [[0, 0], [2, 2], [0, 2], [2, 0]]}]))
    assert "自交" in str(e.value)
    with pytest.raises(RuleError) as e:  # 零面积(共线)
        rules(dict(base, keepouts=[
            {"id": "K1", "layer": "top",
             "polygon": [[0, 0], [1, 0], [2, 0]]}]))
    assert "零" in str(e.value)
    with pytest.raises(RuleError):  # 非有限坐标
        rules(dict(base, keepouts=[
            {"id": "K1", "layer": "top",
             "polygon": [[0, 0], [1, 0], [float("nan"), 1]]}]))


def test_concave_keepout_accepted():
    default, overrides, keepouts = rules({
        "default_clearance_mm": 0.2,
        "keepouts": [{"id": "K1", "layer": "top", "polygon": [
            [0, 0], [4, 0], [4, 4], [2, 4], [2, 1], [0, 1]]}]})
    assert default == 0.2 and not overrides
    assert keepouts[0]["polygon"].area == pytest.approx(10.0)


# ---- 铜间距 ----

def test_clearance_violation_below_threshold():
    top = geom_of(TOP_TWO)
    bottom = geom_of(EMPTY)
    terms = [term("T1", "A", "top", 2, 2.5), term("T2", "B", "top", 15, 2.5)]
    conn = conn_of(top, bottom, terms)
    result = audit(conn, terms, 0.5, {}, [])
    assert not result["ok"]
    assert len(result["clearance_violations"]) == 1
    v = result["clearance_violations"][0]
    assert v["layer"] == "top"
    assert v["islands"] == ["top#0", "top#1"]
    assert v["design_nets"] == [["A"], ["B"]]
    assert v["threshold_mm"] == 0.5
    assert v["distance_mm"] == pytest.approx(0.3, abs=1e-6)
    pa, pb = v["nearest_points"]
    assert pa[0] == pytest.approx(10.0, abs=1e-6)
    assert pb[0] == pytest.approx(10.3, abs=1e-6)
    assert pa[1] == pytest.approx(pb[1], abs=1e-6)


def test_clearance_equal_to_threshold_allowed():
    top = geom_of(TOP_TWO)
    bottom = geom_of(EMPTY)
    terms = [term("T1", "A", "top", 2, 2.5), term("T2", "B", "top", 15, 2.5)]
    conn = conn_of(top, bottom, terms)
    result = audit(conn, terms, 0.3, {}, [])
    assert result["ok"]
    assert not result["clearance_violations"]


def test_same_actual_network_exempt():
    # 同网两岛经镀铜孔跨层连通, 同层间隙再小也不报
    top = geom_of(TOP_TWO)
    bottom = geom_of(square(203000, 50000))
    terms = [term("T1", "A", "top", 2, 2.5), term("T2", "A", "top", 15, 2.5)]
    holes = [hole("H1", 5, 2.5, 1.0, True), hole("H2", 15, 2.5, 1.0, True)]
    conn = conn_of(top, bottom, terms, holes)
    result = audit(conn, terms, 5.0, {}, [])
    assert result["ok"]


def test_override_replaces_default_unordered():
    top = geom_of(TOP_TWO)
    bottom = geom_of(EMPTY)
    terms = [term("T1", "A", "top", 2, 2.5), term("T2", "B", "top", 15, 2.5)]
    conn = conn_of(top, bottom, terms)
    _, overrides, _ = rules({
        "default_clearance_mm": 0.2,
        "clearance_overrides": [{"nets": ["B", "A"], "clearance_mm": 0.5}]})
    result = audit(conn, terms, 0.2, overrides, [])
    assert len(result["clearance_violations"]) == 1
    assert result["clearance_violations"][0]["threshold_mm"] == 0.5
    # 覆盖值小于默认时间距 0.3 达标
    _, overrides, _ = rules({
        "default_clearance_mm": 0.2,
        "clearance_overrides": [{"nets": ["A", "B"], "clearance_mm": 0.25}]})
    result = audit(conn, terms, 0.4, overrides, [])
    assert result["ok"]


def test_multi_name_network_takes_max_threshold():
    # 实际网络含 A/C 两个设计网络(短路), 对 B 取 max(A-B, C-B)
    top = geom_of(TOP_TWO)
    bottom = geom_of(EMPTY)
    terms = [term("T1", "A", "top", 2, 2.5),
             term("T2", "C", "top", 2, 4.5),
             term("T3", "B", "top", 15, 2.5)]
    conn = conn_of(top, bottom, terms)
    _, overrides, _ = rules({
        "default_clearance_mm": 0.2,
        "clearance_overrides": [
            {"nets": ["A", "B"], "clearance_mm": 0.25},
            {"nets": ["C", "B"], "clearance_mm": 0.5}]})
    result = audit(conn, terms, 0.2, overrides, [])
    assert len(result["clearance_violations"]) == 1
    v = result["clearance_violations"][0]
    assert v["threshold_mm"] == 0.5
    assert sorted(v["design_nets"][0]) == ["A", "C"]


def test_network_without_terminals_uses_default():
    # 浮铜所在实际网络无设计网络名, 覆盖值不适用
    top = geom_of(TOP_TWO)
    bottom = geom_of(EMPTY)
    terms = [term("T1", "A", "top", 2, 2.5)]  # top#1 为浮铜
    conn = conn_of(top, bottom, terms)
    _, overrides, _ = rules({
        "default_clearance_mm": 0.2,
        "clearance_overrides": [{"nets": ["A", "B"], "clearance_mm": 5.0}]})
    result = audit(conn, terms, 0.2, overrides, [])
    assert result["ok"]  # 0.3 >= 默认 0.2
    result = audit(conn, terms, 0.5, overrides, [])
    assert len(result["clearance_violations"]) == 1
    assert result["clearance_violations"][0]["design_nets"][1] == []


def test_empty_layer_and_no_violation_ok():
    top = geom_of(EMPTY)
    bottom = geom_of(EMPTY)
    conn = conn_of(top, bottom, [])
    result = audit(conn, [], 0.2, {}, [
        {"id": "K1", "layer": "top",
         "polygon": __import__("shapely").geometry.Polygon(
             [(0, 0), (1, 0), (1, 1)])}])
    assert result == {"clearance_violations": [],
                      "keepout_violations": [], "ok": True}


# ---- 禁铜区 ----

def _keepout(kid, layer, pts):
    from shapely.geometry import Polygon
    return {"id": kid, "layer": layer, "polygon": Polygon(pts)}


def test_keepout_contact_violation_including_floating():
    top = geom_of(EMPTY)
    bottom = geom_of(square(50000, 50000))  # 浮铜
    conn = conn_of(top, bottom, [])
    zones = [_keepout("K1", "bottom", [(1, 1), (2, 1), (2, 2), (1, 2)])]
    result = audit(conn, [], 0.2, {}, zones)
    assert not result["ok"]
    assert len(result["keepout_violations"]) == 1
    v = result["keepout_violations"][0]
    assert v["keepout_id"] == "K1"
    assert v["layer"] == "bottom"
    assert v["island"] == "bottom#0"
    assert v["design_nets"] == []
    assert 1 <= v["contact_point"][0] <= 2
    assert 1 <= v["contact_point"][1] <= 2


def test_keepout_boundary_touch_is_violation():
    top = geom_of(square(100000, 100000))
    bottom = geom_of(EMPTY)
    conn = conn_of(top, bottom, [])
    zones = [_keepout("K1", "top", [(10, 0), (12, 0), (12, 2), (10, 2)])]
    result = audit(conn, [], 0.2, {}, zones)
    assert len(result["keepout_violations"]) == 1


def test_keepout_inside_hole_passes():
    top = geom_of(square(200000, 200000))
    bottom = geom_of(EMPTY)
    holes = [hole("H1", 10, 10, 4.0, False)]  # 孔盘 8..12
    conn = conn_of(top, bottom, [], holes)
    zones = [_keepout("K1", "top", [(9, 9), (11, 9), (11, 11), (9, 11)])]
    result = audit(conn, [], 0.2, {}, zones)
    assert result["ok"]
    assert not result["keepout_violations"]


# ---- 解析修复: 末尾 M02 缺星号 ----

def test_m02_without_star_rejected():
    with pytest.raises(GerberError) as e:
        Parser(HEADER + "%ADD10C,0.5*%\nD10*\nX0Y0D03*\nM02\n").parse()
    assert "结束符" in str(e.value)
    assert e.value.line is not None


def test_m02_with_star_still_accepted():
    p = Parser(HEADER + "%ADD10C,0.5*%\nD10*\nX0Y0D03*\nM02*\n")
    p.parse()
