import json
import math

import pytest
from fastapi.testclient import TestClient

from copper_rev208.drc import audit, load_rules
from copper_rev208.geometry import build_geometry
from copper_rev208.netcheck import NetlistError, Terminal, connectivity
from copper_rev208.parser import Parser
from copper_rev208.review import (compare_connectivity, layer_diff,
                                  match_revisions, region_detail,
                                  transform_geometry, transform_xy,
                                  validate_transform)
from main import app

client = TestClient(app)

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


# ---- 变换校验 ----

def test_transform_rotation_restricted():
    for bad in (45, -90, 1.5, float("nan")):
        with pytest.raises(NetlistError):
            validate_transform(bad, 0, 0)
    for good in (0, 90, 180, 270):
        assert validate_transform(good, 1.5, -2.5) == (good, 1.5, -2.5)
    with pytest.raises(NetlistError):
        validate_transform(0, float("inf"), 0)


def test_transform_xy_rotate_then_translate():
    # 先绕原点逆时针 90 度再平移
    assert transform_xy(1, 0, 90, 2, 3) == (2, 4)
    assert transform_xy(0, 1, 180, 1, 1) == (1, 0)
    assert transform_xy(2, 3, 270, 0, 0) == (3, -2)
    assert transform_xy(2, 3, 0, -1, 1) == (1, 4)


def test_transform_geometry_rotation():
    geom = geom_of(square(20000, 10000))  # (0,0)-(2,1)
    out = transform_geometry(geom, 90, 10, 0)
    assert out.bounds == pytest.approx((9.0, 0.0, 10.0, 2.0))


# ---- 铜层差分 ----

def test_layer_diff_areas_and_boundaries():
    old = geom_of(square(40000, 40000))
    new = geom_of(square(60000, 40000, 20000, 0))  # (2,0)-(6,4)
    diff = layer_diff(old, new)
    assert region_detail(diff["added"])["area_mm2"] == pytest.approx(8.0)
    assert region_detail(diff["removed"])["area_mm2"] == pytest.approx(8.0)
    assert region_detail(diff["common"])["area_mm2"] == pytest.approx(8.0)
    assert region_detail(diff["added"])["regions"] == 1
    ext = region_detail(diff["added"])["boundaries"][0]["exterior"]
    assert len(ext) >= 4


def test_layer_diff_keeps_holes_and_thin_copper():
    body = square(100000, 100000) + "\n%LPC*%\n" + square(50000, 50000, 40000, 40000)
    old = geom_of("%LPD*%\n" + body)  # 10x10 中间扣 1x1 孔
    new = geom_of(square(100000, 100000))
    diff = layer_diff(old, new)
    common = region_detail(diff["common"])
    assert common["boundaries"][0]["holes"]  # 孔洞保留
    added = region_detail(diff["added"])
    assert added["area_mm2"] == pytest.approx(1.0)  # 细铜不被吞掉


def test_layer_diff_empty_and_no_change():
    empty = geom_of("%ADD10C,0.5*%\nX0Y0D02*")
    diff = layer_diff(empty, empty)
    for key in ("added", "removed", "common"):
        detail = region_detail(diff[key])
        assert detail["area_mm2"] == 0.0
        assert detail["regions"] == 0
        assert detail["boundaries"] == []
    geom = geom_of(square(40000, 40000))
    diff = layer_diff(geom, geom)
    assert region_detail(diff["added"])["area_mm2"] == 0.0
    assert region_detail(diff["removed"])["area_mm2"] == 0.0
    assert region_detail(diff["common"])["area_mm2"] == pytest.approx(16.0)


# ---- 端子一致性 ----

def test_match_revisions_rejects_mismatch():
    old = [term("T1", "A", "top", 0, 0), term("T2", "B", "top", 1, 1)]
    match_revisions(old, [term("T1", "A", "top", 5, 5),
                          term("T2", "B", "top", 6, 6)])  # 位置可变
    with pytest.raises(NetlistError):  # 缺端子
        match_revisions(old, [term("T1", "A", "top", 0, 0)])
    with pytest.raises(NetlistError):  # 多端子
        match_revisions(old, old + [term("T3", "B", "top", 2, 2)])
    with pytest.raises(NetlistError):  # 网名不一致
        match_revisions(old, [term("T1", "A", "top", 0, 0),
                              term("T2", "C", "top", 1, 1)])
    with pytest.raises(NetlistError):  # 层不一致
        match_revisions(old, [term("T1", "A", "top", 0, 0),
                              term("T2", "B", "bottom", 1, 1)])


# ---- 连通变化 ----

def _conn(top_body, bottom_body, terms):
    top = geom_of(top_body)
    bottom = geom_of(bottom_body)
    return connectivity(top, bottom, terms, [], 0.01)


def test_compare_connectivity_pairs_and_landed_changes():
    terms = [term("T1", "A", "top", 1, 1), term("T2", "A", "top", 9, 1),
             term("T3", "B", "top", 5, 1)]
    # 旧版: 两独立铜块, T3 悬空
    old_conn = _conn(square(40000, 40000) + "\n" + square(120000, 40000, 80000, 0),
                     "%ADD10C,0.5*%\nX0Y0D02*", terms)
    # 新版: 一整块, 三端子全落铜且连通
    new_conn = _conn(square(120000, 40000), "%ADD10C,0.5*%\nX0Y0D02*", terms)
    changes = compare_connectivity(old_conn, new_conn)
    # 仅比较两版均落铜的 T1/T2
    assert changes["added_connections"] == [["T1", "T2"]]
    assert changes["lost_connections"] == []
    assert changes["landed_status_changes"]["gained_copper"] == ["T3"]
    assert changes["landed_status_changes"]["lost_copper"] == []
    # 反向: 旧版悬空不能冒充新断路
    reverse = compare_connectivity(new_conn, old_conn)
    assert reverse["lost_connections"] == [["T1", "T2"]]
    assert reverse["added_connections"] == []
    assert reverse["landed_status_changes"]["lost_copper"] == ["T3"]


# ---- 阈值修复: 部分名称对有覆盖时其余组合仍用默认 ----

def test_partial_override_keeps_default_for_other_pairs():
    top = geom_of(square(100000, 50000) + "\n" + square(203000, 50000, 103000, 0))
    bottom = geom_of("%ADD10C,0.5*%\nX0Y0D02*")
    terms = [term("T1", "A", "top", 2, 2.5),
             term("T2", "C", "top", 2, 4.5),
             term("T3", "B", "top", 15, 2.5)]
    conn = connectivity(top, bottom, terms, [], 0.01)
    # 实际网络 {A,C} 对 B: 仅 A-B 有覆盖 0.25, C-B 组合仍适用默认 0.4
    _, overrides, _ = load_rules(json.dumps({
        "default_clearance_mm": 0.4,
        "clearance_overrides": [
            {"nets": ["A", "B"], "clearance_mm": 0.25}]}).encode(),
        {"A", "B", "C"})
    result = audit(conn, terms, 0.4, overrides, [])
    assert len(result["clearance_violations"]) == 1
    assert result["clearance_violations"][0]["threshold_mm"] == 0.4


# ---- 端到端 /api/review ----

def _review_payload():
    files = {}
    for field, name in (("old_top", "rev_old_top.gbr"),
                        ("old_bottom", "rev_old_bottom.gbr"),
                        ("new_top", "rev_new_top.gbr"),
                        ("new_bottom", "rev_new_bottom.gbr")):
        files[field] = (name, open("samples/" + name, "rb"),
                        "application/octet-stream")
    for field, name in (("old_netlist", "rev_old_netlist.json"),
                        ("new_netlist", "rev_new_netlist.json"),
                        ("rules", "rev_rules.json")):
        files[field] = (name, open("samples/" + name, "rb"),
                        "application/json")
    data = {"rotation_deg": "0", "translate_x": "2.0",
            "translate_y": "0.0", "tolerance": "0.01"}
    return files, data


def test_review_endpoint():
    files, data = _review_payload()
    resp = client.post("/api/review", files=files, data=data)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    top = result["layers"]["top"]
    # 加铜 = 桥接走线本体 2x1(圆帽落在铜块内) + 新焊盘圆
    assert top["added"]["area_mm2"] == pytest.approx(
        2.0 + math.pi, abs=0.02)
    # 删铜 = 旧版 (8,6) 焊盘圆
    assert top["removed"]["area_mm2"] == pytest.approx(math.pi, abs=0.01)
    assert top["common"]["area_mm2"] == pytest.approx(32.0, abs=0.01)
    bottom = result["layers"]["bottom"]
    assert bottom["added"]["area_mm2"] == 0.0
    assert bottom["removed"]["area_mm2"] == pytest.approx(6.0)
    assert bottom["common"]["area_mm2"] == pytest.approx(12.0)
    changes = result["connectivity_changes"]
    assert changes["added_connections"] == [["T1", "T2"]]
    assert changes["lost_connections"] == []
    assert changes["landed_status_changes"]["lost_copper"] == ["T4"]
    assert result["old_revision"]["netcheck"]["opens"]  # 旧版 SIG 断开
    assert result["new_revision"]["netcheck"]["ok"]
    assert result["old_revision"]["drc"]["ok"]
    assert result["new_revision"]["drc"]["ok"]
    # SVG 下载: 三类区域颜色区分
    for layer in ("top", "bottom"):
        url = result["layers"][layer]["svg_url"]
        svg = client.get(url)
        assert svg.status_code == 200
        assert svg.headers["content-type"] == "image/svg+xml"
    top_svg = client.get(result["layers"]["top"]["svg_url"]).text
    assert "#2ca02c" in top_svg and "#d62728" in top_svg
    assert "#b87333" in top_svg


def test_review_rejects_bad_rotation_and_mismatch():
    files, data = _review_payload()
    data["rotation_deg"] = "45"
    resp = client.post("/api/review", files=files, data=data)
    assert resp.status_code == 422
    files, data = _review_payload()
    bad_netlist = json.dumps({"terminals": [
        {"id": "T1", "net": "SIG", "layer": "top", "x": -1.0, "y": 1.0}],
        "holes": []}).encode()
    files["new_netlist"] = ("rev_new_netlist.json", bad_netlist,
                            "application/json")
    resp = client.post("/api/review", files=files, data=data)
    assert resp.status_code == 422
    assert "缺少" in resp.json()["detail"]["error"]
