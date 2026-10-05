"""工程变更评审: 几何对齐、铜层差分、连通变化对比与报告汇总。

新版几何(铜、端子、孔)先绕原点逆时针旋转(限 0/90/180/270 度)
再平移, 变换到旧版坐标系; 层不交换, 不做自动配准或镜像。
差分按层计算加铜(新-旧)/删铜(旧-新)/共有区域, 保留孔洞与细铜;
连通变化按稳定端子 ID 比较无序端子对, 仅统计两版均落铜的端子,
落铜状态变化单独列出。全部报告与 SVG 统一在旧版坐标系。
"""
import math
from collections import defaultdict
from itertools import combinations

from shapely import affinity

from .drc import audit
from .netcheck import Hole, NetlistError, Terminal, connectivity, report

_ANGLES = (0, 90, 180, 270)


def validate_transform(rotation_deg, translate_x, translate_y):
    """校验旋转角与平移量, 返回规范化的 (rotation, dx, dy)。"""
    if isinstance(rotation_deg, bool) or not isinstance(
            rotation_deg, (int, float)):
        raise NetlistError("rotation_deg 必须为数字", "rotation_deg")
    if float(rotation_deg) not in [float(a) for a in _ANGLES]:
        raise NetlistError(
            "rotation_deg 仅支持 0/90/180/270 度", "rotation_deg")
    rotation = int(rotation_deg)
    for name, value in (("translate_x", translate_x),
                        ("translate_y", translate_y)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise NetlistError("%s 必须为数字" % name, name)
        if not math.isfinite(value):
            raise NetlistError("%s 必须为有限数值" % name, name)
    return rotation, float(translate_x), float(translate_y)


def transform_xy(x, y, rotation, dx, dy):
    """先绕原点逆时针旋转再平移(90 度倍数取精确三角值)。"""
    rad = math.radians(rotation)
    cos_v = round(math.cos(rad))
    sin_v = round(math.sin(rad))
    return (x * cos_v - y * sin_v + dx, x * sin_v + y * cos_v + dy)


def transform_geometry(geom, rotation, dx, dy):
    """对整层铜几何施加旋转+平移。"""
    if geom.is_empty:
        return geom
    out = affinity.rotate(geom, rotation, origin=(0.0, 0.0))
    return affinity.translate(out, xoff=dx, yoff=dy)


def transform_terminals(terminals, rotation, dx, dy):
    """端子坐标变换到旧版坐标系, ID/网络/层保持不变。"""
    out = []
    for t in terminals:
        x, y = transform_xy(t.x, t.y, rotation, dx, dy)
        out.append(Terminal(t.id, t.net, t.layer, x, y, t.pos))
    return out


def transform_holes(holes, rotation, dx, dy):
    """孔位坐标变换到旧版坐标系(孔径与镀铜属性不变)。"""
    out = []
    for h in holes:
        x, y = transform_xy(h.x, h.y, rotation, dx, dy)
        out.append(Hole(h.id, x, y, h.diameter, h.plated, h.pos))
    return out


def match_revisions(old_terminals, new_terminals):
    """校验两版端子 ID 与设计网名一致(位置可变), 不一致即拒绝整单。"""
    old_by_id = {t.id: t for t in old_terminals}
    new_by_id = {t.id: t for t in new_terminals}
    missing = sorted(set(old_by_id) - set(new_by_id))
    if missing:
        raise NetlistError(
            "新版网表缺少旧版端子 %s" % ", ".join(missing),
            old_by_id[missing[0]].pos)
    extra = sorted(set(new_by_id) - set(old_by_id))
    if extra:
        raise NetlistError(
            "新版网表多出端子 %s" % ", ".join(extra),
            new_by_id[extra[0]].pos)
    for tid in sorted(old_by_id):
        old_t, new_t = old_by_id[tid], new_by_id[tid]
        if old_t.net != new_t.net:
            raise NetlistError(
                "端子 %s 设计网名不一致: 旧版 %s / 新版 %s"
                % (tid, old_t.net, new_t.net), new_t.pos)
        if old_t.layer != new_t.layer:
            raise NetlistError(
                "端子 %s 所在层不一致: 旧版 %s / 新版 %s"
                % (tid, old_t.layer, new_t.layer), new_t.pos)


def _clean(geom):
    if geom.is_empty:
        return geom
    return geom.buffer(0)


def layer_diff(old_geom, new_geom):
    """同一曲线误差下按层差分: 加铜/删铜/共有(保留孔洞与细铜)。"""
    return {
        "added": _clean(new_geom.difference(old_geom)),
        "removed": _clean(old_geom.difference(new_geom)),
        "common": _clean(old_geom.intersection(new_geom)),
    }


def _round_ring(coords):
    pts = list(coords)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return [[round(x, 6), round(y, 6)] for x, y in pts]


def region_detail(geom):
    """面积、区域数与边界(外环+孔洞), 空几何正常返回零值。"""
    polys = [g for g in getattr(geom, "geoms", [geom])
             if g.geom_type == "Polygon" and not g.is_empty]
    polys.sort(key=lambda g: (g.bounds[0], g.bounds[1],
                              g.bounds[2], g.bounds[3]))
    return {
        "area_mm2": round(geom.area, 6) if not geom.is_empty else 0.0,
        "regions": len(polys),
        "boundaries": [
            {"exterior": _round_ring(p.exterior.coords),
             "holes": [_round_ring(ring.coords) for ring in p.interiors]}
            for p in polys
        ],
    }


def _connected_pairs(conn, terminal_ids):
    """端子集合内的连通无序对(按实际网络连通分量分组)。"""
    by_comp = defaultdict(list)
    for tid in terminal_ids:
        by_comp[conn["comp_of"][conn["term_node"][tid]]].append(tid)
    pairs = set()
    for members in by_comp.values():
        for pair in combinations(sorted(members), 2):
            pairs.add(pair)
    return pairs


def compare_connectivity(old_conn, new_conn):
    """按稳定端子 ID 对比两版连通关系(不比较临时铜岛编号)。

    仅比较两版均落铜的端子; 落铜状态变化单独列出,
    旧版已悬空的端子不参与断路统计。
    """
    old_landed = set(old_conn["term_node"])
    new_landed = set(new_conn["term_node"])
    both = old_landed & new_landed
    old_pairs = _connected_pairs(old_conn, both)
    new_pairs = _connected_pairs(new_conn, both)
    return {
        "added_connections": [list(p) for p in sorted(new_pairs - old_pairs)],
        "lost_connections": [list(p) for p in sorted(old_pairs - new_pairs)],
        "landed_status_changes": {
            "lost_copper": sorted(old_landed - new_landed),
            "gained_copper": sorted(new_landed - old_landed),
        },
    }


def revision_reports(top_geom, bottom_geom, terminals, holes, tol, rules):
    """单版短/断路报告与制造违规报告(不改写原文件, 不自动修板)。"""
    default, overrides, keepouts = rules
    conn = connectivity(top_geom, bottom_geom, terminals, holes, tol)
    net_report = report(conn, terminals)
    drc_report = audit(conn, terminals, default, overrides, keepouts)
    return conn, {
        "netcheck": net_report,
        "drc": drc_report,
        "ok": net_report["ok"] and drc_report["ok"],
    }
