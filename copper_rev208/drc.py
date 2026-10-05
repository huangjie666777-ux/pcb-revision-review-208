"""制造规则审查: 同层铜间距与分层禁铜区(只报告, 不裁铜或修板)。

复用 netcheck 的扣孔后铜岛与镀铜孔实际网络连通图:
- 只量同层、不同实际网络铜岛的最短实体距离(Shapely 实体距离,
  非包围盒/端子距离), 同实际网络免检; 距离小于阈值才违规, 等于允许。
- 阈值 = 默认铜间距, 无序网络名对覆盖值替代默认; 多名称实际网络
  取全部组合适用阈值的最大值, 无端子实际网络用默认。
- 禁铜区为分层简单多边形(支持凹形), 扣孔后铜(含浮铜)与其任何
  接触均违规; 区域落在孔内且不碰铜则通过。
"""
import json
import math

from shapely.geometry import Polygon
from shapely.ops import nearest_points

from .netcheck import NetlistError


class RuleError(NetlistError):
    """制造规则 JSON 校验错误, position 指向 JSON 内位置。"""


def _finite(value, pos, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleError("%s 必须为数字" % field, pos)
    if not math.isfinite(value):
        raise RuleError("%s 必须为有限数值" % field, pos)
    return float(value)


def _positive(value, pos, field):
    value = _finite(value, pos, field)
    if value <= 0:
        raise RuleError("%s 必须为正数" % field, pos)
    return value


def load_rules(raw, known_nets):
    """解析并校验规则 JSON, 返回 (default_mm, overrides, keepouts)。

    overrides: {frozenset({netA, netB}): mm}(名称对无序, 重复拒绝);
    keepouts: [{"id", "layer", "polygon"}](简单多边形, 支持凹形)。
    known_nets: 网表中出现的全部设计网络名, 未知网络名拒绝。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuleError("规则文件必须为 UTF-8 编码的 JSON")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuleError("规则 JSON 解析失败: %s" % exc,
                        "line %d column %d" % (exc.lineno, exc.colno))
    if not isinstance(data, dict):
        raise RuleError("规则须为 JSON 对象, 含 default_clearance_mm")

    default = _positive(data.get("default_clearance_mm"),
                        "default_clearance_mm", "default_clearance_mm")

    overrides = {}
    raw_overrides = data.get("clearance_overrides", [])
    if not isinstance(raw_overrides, list):
        raise RuleError("clearance_overrides 必须为数组",
                        "clearance_overrides")
    for idx, item in enumerate(raw_overrides):
        pos = "clearance_overrides[%d]" % idx
        if not isinstance(item, dict):
            raise RuleError("条目须为 JSON 对象", pos)
        nets = item.get("nets")
        if (not isinstance(nets, list) or len(nets) != 2
                or any(not isinstance(n, str) or not n for n in nets)):
            raise RuleError("nets 必须为两个网络名组成的数组", pos)
        name_a, name_b = nets
        if name_a == name_b:
            raise RuleError("名称对的两个网络不能相同", pos)
        for name in (name_a, name_b):
            if name not in known_nets:
                raise RuleError("未知网络名 %s" % name, pos)
        key = frozenset((name_a, name_b))
        if key in overrides:
            raise RuleError("重复的名称对 %s/%s" % (name_a, name_b), pos)
        overrides[key] = _positive(item.get("clearance_mm"), pos,
                                   "clearance_mm")

    keepouts = []
    seen_ids = set()
    raw_keepouts = data.get("keepouts", [])
    if not isinstance(raw_keepouts, list):
        raise RuleError("keepouts 必须为数组", "keepouts")
    for idx, item in enumerate(raw_keepouts):
        pos = "keepouts[%d]" % idx
        if not isinstance(item, dict):
            raise RuleError("条目须为 JSON 对象", pos)
        kid = item.get("id")
        if not isinstance(kid, str) or not kid:
            raise RuleError("id 必须为非空字符串", pos)
        if kid in seen_ids:
            raise RuleError("重复的禁铜区 ID %s" % kid, pos)
        seen_ids.add(kid)
        layer = item.get("layer")
        if layer not in ("top", "bottom"):
            raise RuleError("layer 必须为 top 或 bottom", pos)
        coords = item.get("polygon")
        if not isinstance(coords, list) or len(coords) < 3:
            raise RuleError("polygon 至少需要 3 个顶点", pos)
        pts = []
        for vidx, vertex in enumerate(coords):
            vpos = "%s.polygon[%d]" % (pos, vidx)
            if not isinstance(vertex, list) or len(vertex) != 2:
                raise RuleError("顶点须为 [x, y] 数组", vpos)
            pts.append((_finite(vertex[0], vpos, "x"),
                        _finite(vertex[1], vpos, "y")))
        poly = Polygon(pts)
        if not poly.is_valid or not poly.exterior.is_simple:
            if poly.buffer(0).is_empty:
                raise RuleError("禁铜区面积为零", pos)
            raise RuleError("禁铜区多边形自交", pos)
        if poly.area <= 0:
            raise RuleError("禁铜区面积为零", pos)
        keepouts.append({"id": kid, "layer": layer, "polygon": poly})
    return default, overrides, keepouts


def _comp_nets(conn, terminals):
    """实际网络(连通分量) id -> 设计网络名集合(无端子则为空)。"""
    term_by_id = {t.id: t for t in terminals}
    comp_nets = {}
    for tid, node in conn["term_node"].items():
        comp_nets.setdefault(conn["comp_of"][node], set()).add(
            term_by_id[tid].net)
    return comp_nets


def _threshold(comp_a, comp_b, comp_nets, default, overrides):
    """两实际网络间适用阈值: 覆盖值替代默认, 多名称取覆盖最大值。"""
    nets_a = comp_nets.get(comp_a, set())
    nets_b = comp_nets.get(comp_b, set())
    if not nets_a or not nets_b:
        return default
    # 逐名称对取值: 有覆盖用覆盖值, 无覆盖的组合回退默认阈值
    values = []
    for name_a in nets_a:
        for name_b in nets_b:
            values.append(overrides.get(frozenset((name_a, name_b)),
                                        default))
    return max(values)


def _round_pt(point):
    return [round(point.x, 6), round(point.y, 6)]


def audit(conn, terminals, default, overrides, keepouts):
    """对连通图执行铜间距与禁铜区审查, 返回违规报告。"""
    layers = conn["layers"]
    comp_of = conn["comp_of"]
    comp_nets = _comp_nets(conn, terminals)

    clearance_violations = []
    for layer in ("top", "bottom"):
        islands = layers[layer]
        for i in range(len(islands)):
            for j in range(i + 1, len(islands)):
                comp_i = comp_of[("island", layer, i)]
                comp_j = comp_of[("island", layer, j)]
                if comp_i == comp_j:
                    continue  # 同实际网络免检
                limit = _threshold(comp_i, comp_j, comp_nets,
                                   default, overrides)
                distance = islands[i].distance(islands[j])
                if distance < limit:  # 等于阈值允许
                    pt_i, pt_j = nearest_points(islands[i], islands[j])
                    clearance_violations.append({
                        "layer": layer,
                        "islands": ["%s#%d" % (layer, i),
                                    "%s#%d" % (layer, j)],
                        "actual_networks": [comp_i, comp_j],
                        "design_nets": [sorted(comp_nets.get(comp_i, [])),
                                        sorted(comp_nets.get(comp_j, []))],
                        "threshold_mm": round(limit, 6),
                        "distance_mm": round(distance, 6),
                        "nearest_points": [_round_pt(pt_i),
                                           _round_pt(pt_j)],
                    })

    keepout_violations = []
    for zone in keepouts:
        layer = zone["layer"]
        for idx, island in enumerate(layers[layer]):
            contact = island.intersection(zone["polygon"])
            if contact.is_empty:
                continue  # 含区域位于孔内不碰铜的情况
            point = contact.representative_point()
            comp = comp_of[("island", layer, idx)]
            keepout_violations.append({
                "keepout_id": zone["id"],
                "layer": layer,
                "island": "%s#%d" % (layer, idx),
                "actual_network": comp,
                "design_nets": sorted(comp_nets.get(comp, [])),
                "contact_point": _round_pt(point),
            })

    return {
        "clearance_violations": clearance_violations,
        "keepout_violations": keepout_violations,
        "ok": not clearance_violations and not keepout_violations,
    }
