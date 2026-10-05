"""工程变更评审: 新版刚体变换、扣孔铜差分与稳定端子连通对比。"""
import math
from dataclasses import replace

from shapely.affinity import rotate, translate
from shapely.geometry import GeometryCollection
from shapely.ops import unary_union

from .drc import audit
from .netcheck import NetlistError, connectivity, report

_ROTATIONS = (0.0, 90.0, 180.0, 270.0)


class RevisionError(NetlistError):
    """两版工程输入不一致或变换参数错误。"""


def validate_transform(dx, dy, rotation):
    for name, value in (("dx", dx), ("dy", dy), ("rotation", rotation)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RevisionError("%s 必须为数字" % name, name)
        if not math.isfinite(value):
            raise RevisionError("%s 必须为有限数值" % name, name)
    if float(rotation) not in _ROTATIONS:
        raise RevisionError("rotation 仅允许 0、90、180、270 度", "rotation")
    return float(dx), float(dy), float(rotation)


def _transform_point(x, y, dx, dy, rotation):
    rad = math.radians(rotation)
    cosine, sine = math.cos(rad), math.sin(rad)
    return (x * cosine - y * sine + dx, x * sine + y * cosine + dy)


def transform_geometry(geom, dx, dy, rotation):
    """先绕原点逆时针旋转, 再平移; 不镜像、不自动配准。"""
    if geom.is_empty:
        return geom
    moved = rotate(geom, rotation, origin=(0.0, 0.0), use_radians=False)
    return translate(moved, xoff=dx, yoff=dy)


def _transform_records(records, dx, dy, rotation):
    result = []
    for record in records:
        x, y = _transform_point(record.x, record.y, dx, dy, rotation)
        result.append(replace(record, x=x, y=y,
                              pos="新版变换后:" + record.pos))
    return result


def transform_terminals(terminals, dx, dy, rotation):
    return _transform_records(terminals, dx, dy, rotation)


def transform_holes(holes, dx, dy, rotation):
    return _transform_records(holes, dx, dy, rotation)


def validate_matching_netlists(old_terminals, new_terminals):
    old_by_id = {term.id: term for term in old_terminals}
    new_by_id = {term.id: term for term in new_terminals}
    old_ids, new_ids = set(old_by_id), set(new_by_id)
    if old_ids != new_ids:
        details = []
        missing = sorted(old_ids - new_ids)
        extra = sorted(new_ids - old_ids)
        if missing:
            details.append("新版缺少端子 %s" % ", ".join(missing))
        if extra:
            details.append("新版多出端子 %s" % ", ".join(extra))
        raise RevisionError("两版端子 ID 不一致: " + "；".join(details),
                            "old_netlist/new_netlist")
    for tid in sorted(old_ids):
        old_term, new_term = old_by_id[tid], new_by_id[tid]
        if old_term.net != new_term.net:
            raise RevisionError(
                "端子 %s 的设计网名不一致: 旧版 %s, 新版 %s"
                % (tid, old_term.net, new_term.net),
                "%s / %s" % (old_term.pos, new_term.pos))
        if old_term.layer != new_term.layer:
            raise RevisionError(
                "端子 %s 的层别不一致: 旧版 %s, 新版 %s; 层不交换"
                % (tid, old_term.layer, new_term.layer),
                "%s / %s" % (old_term.pos, new_term.pos))


def _polygons(geom):
    if geom.is_empty:
        return []
    return [g for g in getattr(geom, "geoms", [geom])
            if g.geom_type == "Polygon" and not g.is_empty]


def _ring(coords):
    return [[round(x, 6), round(y, 6)] for x, y in coords]


def _boundaries(geom):
    return [{
        "exterior": _ring(polygon.exterior.coords),
        "interiors": [_ring(ring.coords) for ring in polygon.interiors],
    } for polygon in _polygons(geom)]


def _region_summary(geom):
    polygons = _polygons(geom)
    return {
        "area_mm2": round(sum(polygon.area for polygon in polygons), 6),
        "region_count": len(polygons),
        "boundaries": _boundaries(geom),
    }


def _copper_from_connection(conn, layer):
    islands = conn["layers"][layer]
    if not islands:
        return GeometryCollection()
    copper = unary_union(islands)
    return copper.buffer(0) if not copper.is_valid else copper


def _connected_landed_pairs(conn):
    ids = sorted(conn["term_node"])
    pairs = set()
    for index, left in enumerate(ids):
        left_component = conn["comp_of"][conn["term_node"][left]]
        for right in ids[index + 1:]:
            right_component = conn["comp_of"][conn["term_node"][right]]
            if left_component == right_component:
                pairs.add((left, right))
    return pairs


def review_revision(old_geoms, new_geoms, old_netlist, new_netlist,
                    rule_set, dx, dy, rotation, tolerance):
    dx, dy, rotation = validate_transform(dx, dy, rotation)
    old_terminals, old_holes = old_netlist
    new_terminals_raw, new_holes_raw = new_netlist
    validate_matching_netlists(old_terminals, new_terminals_raw)
    new_terminals = transform_terminals(new_terminals_raw, dx, dy, rotation)
    new_holes = transform_holes(new_holes_raw, dx, dy, rotation)
    moved_new_geoms = {
        layer: transform_geometry(geom, dx, dy, rotation)
        for layer, geom in new_geoms.items()
    }

    old_conn = connectivity(old_geoms["top"], old_geoms["bottom"],
                            old_terminals, old_holes, tolerance)
    new_conn = connectivity(moved_new_geoms["top"], moved_new_geoms["bottom"],
                            new_terminals, new_holes, tolerance)

    layers = {}
    for layer in ("top", "bottom"):
        old_copper = _copper_from_connection(old_conn, layer)
        new_copper = _copper_from_connection(new_conn, layer)
        geometries = {
            "added": new_copper.difference(old_copper).buffer(0),
            "removed": old_copper.difference(new_copper).buffer(0),
            "common": old_copper.intersection(new_copper).buffer(0),
        }
        layers[layer] = {
            "added_copper": _region_summary(geometries["added"]),
            "removed_copper": _region_summary(geometries["removed"]),
            "common_copper": _region_summary(geometries["common"]),
            "_geometries": geometries,
        }

    old_landing = {term.id: term.id in old_conn["term_node"]
                   for term in old_terminals}
    new_landing = {term.id: term.id in new_conn["term_node"]
                   for term in new_terminals}
    both_landed = {tid for tid, landed in old_landing.items()
                   if landed and new_landing[tid]}
    old_pairs = {pair for pair in _connected_landed_pairs(old_conn)
                 if pair[0] in both_landed and pair[1] in both_landed}
    new_pairs = {pair for pair in _connected_landed_pairs(new_conn)
                 if pair[0] in both_landed and pair[1] in both_landed}

    default, overrides, keepouts = rule_set
    old_review = audit(old_conn, old_terminals, default, overrides, keepouts)
    new_review = audit(new_conn, new_terminals, default, overrides, keepouts)
    old_net_report = report(old_conn, old_terminals)
    new_net_report = report(new_conn, new_terminals)
    old_review["netcheck"] = old_net_report
    new_review["netcheck"] = new_net_report
    old_review["ok"] = old_review["ok"] and old_net_report["ok"]
    new_review["ok"] = new_review["ok"] and new_net_report["ok"]

    return {
        "transform": {
            "dx_mm": dx, "dy_mm": dy, "rotation_deg": rotation,
            "order": "rotate_origin_ccw_then_translate",
        },
        "copper_changes": {
            layer: {key: value for key, value in data.items()
                    if not key.startswith("_")}
            for layer, data in layers.items()
        },
        "connectivity_changes": {
            "added_connections": [list(pair)
                                   for pair in sorted(new_pairs - old_pairs)],
            "lost_connections": [list(pair)
                                  for pair in sorted(old_pairs - new_pairs)],
            "landing_status_changes": {
                "gained_copper": [tid for tid in sorted(old_landing)
                                   if not old_landing[tid] and new_landing[tid]],
                "lost_copper": [tid for tid in sorted(old_landing)
                                 if old_landing[tid] and not new_landing[tid]],
            },
            "compared_terminals": sorted(both_landed),
        },
        "old_review": old_review,
        "new_review": new_review,
        "ok": old_review["ok"] and new_review["ok"],
        "_svg_layers": layers,
    }
