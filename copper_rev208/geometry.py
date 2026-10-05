
"""曝光几何引擎: 将解析事件流合成为最终铜层几何(Shapely)。

- 线段/圆弧按光圈直径缓冲为真实覆盖(圆头圆角), 并集去重;
- G36/G37 区域按内部填充, 仅允许单个简单直线闭环;
- LPD 加入铜层, LPC 从已有铜层扣除, 严格按指令顺序组合。
"""
import math

from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from .errors import GerberError

_BUFFER_RESOLUTION = 32  # 缓冲每象限分段数


def arc_points(x1, y1, x2, y2, i, j, direction, tol, line, source):
    """将 G75 多象限圆弧离散为折线(含终点), 弦高误差 <= tol(mm)。"""
    cx, cy = x1 + i, y1 + j
    r_start = math.hypot(x1 - cx, y1 - cy)
    r_end = math.hypot(x2 - cx, y2 - cy)
    if r_start <= 0:
        raise GerberError("非法圆弧: 圆心偏移 I/J 不能同时为零", line, source)
    check = max(4 * tol, 0.02)
    if abs(r_end - r_start) > check:
        raise GerberError(
            "非法圆弧: 终点不在圆弧上(半径 %.4f vs %.4f mm)"
            % (r_start, r_end), line, source)
    radius = r_start
    a0 = math.atan2(y1 - cy, x1 - cx)
    a1 = math.atan2(y2 - cy, x2 - cx)
    full = math.hypot(x2 - x1, y2 - y1) <= 1e-9
    if direction == "G03":        # 逆时针
        sweep = (a1 - a0) % (2 * math.pi)
    else:                          # G02 顺时针
        sweep = (a0 - a1) % (2 * math.pi)
    if full:
        sweep = 2 * math.pi
    elif sweep <= 1e-12:
        raise GerberError("非法圆弧: 起终点重合但未构成整圆", line, source)

    # 由弦高误差推导步距角: tol = r*(1 - cos(step/2))
    if tol >= radius:
        step = math.pi
    else:
        step = 2 * math.acos(1 - tol / radius)
    step = min(step, math.pi / 2)
    n = max(1, math.ceil(sweep / step))
    sign = 1.0 if direction == "G03" else -1.0
    pts = []
    for k in range(1, n + 1):
        ang = a0 + sign * sweep * k / n
        pts.append((cx + radius * math.cos(ang), cy + radius * math.sin(ang)))
    pts[-1] = (x2, y2)  # 终点精确落位
    return pts


def circle_quad_segs(tol, radius):
    """由弦高误差 tol(mm) 推导圆缓冲的每象限分段数。"""
    if radius <= 0:
        return _BUFFER_RESOLUTION
    if tol >= radius:
        return 4
    return max(4, math.ceil(math.pi / (2.0 * math.acos(1.0 - tol / radius))))


def _stroke(pts, width, tol):
    return LineString(pts).buffer(
        width / 2.0,
        cap_style="round",
        join_style="round",
        quad_segs=circle_quad_segs(tol, width / 2.0),
    )


def _flash(aperture, x, y, tol):
    if aperture.kind == "C":
        return Point(x, y).buffer(aperture.params[0] / 2.0,
                                  quad_segs=circle_quad_segs(
                                      tol, aperture.params[0] / 2.0))
    w, h = aperture.params
    return box(x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)


def build_geometry(events, apertures, tol):
    """按事件顺序合成铜层几何, 返回 Shapely 几何(可能为空)。"""
    copper = None
    dark = True
    region_pts = None
    region_start_line = None
    region_start_src = None

    def expose(shape):
        nonlocal copper
        if dark:
            copper = shape if copper is None else copper.union(shape)
        else:
            if copper is not None:
                copper = copper.difference(shape)

    for ev in events:
        if ev.kind == "polarity":
            dark = ev.data["dark"]
        elif ev.kind == "flash":
            expose(_flash(apertures[ev.data["aperture"]],
                          ev.data["x"], ev.data["y"], tol))
        elif ev.kind == "draw":
            d = ev.data
            ap = apertures[d["aperture"]]
            if ap.kind != "C":
                raise GerberError("矩形光圈仅支持闪光, 不能用于绘制",
                                  ev.line, ev.source)
            if "arc" in d:
                pts = [(d["x1"], d["y1"])] + arc_points(
                    d["x1"], d["y1"], d["x2"], d["y2"], d["i"], d["j"],
                    d["arc"], tol, ev.line, ev.source)
            else:
                pts = [(d["x1"], d["y1"]), (d["x2"], d["y2"])]
            width = ap.params[0]  # 绘制仅圆形光圈
            expose(_stroke(pts, width, tol))
        elif ev.kind == "region_start":
            region_pts = []
            region_start_line = ev.line
            region_start_src = ev.source
        elif ev.kind == "region_draw":
            d = ev.data
            p = (d["x2"], d["y2"])
            if not region_pts:
                region_pts.append((d["x1"], d["y1"]))
            region_pts.append(p)
        elif ev.kind == "region_end":
            if not region_pts or len(region_pts) < 4:
                raise GerberError("区域未闭合: 顶点不足", ev.line, ev.source)
            first, last = region_pts[0], region_pts[-1]
            if math.hypot(first[0] - last[0], first[1] - last[1]) > 1e-9:
                raise GerberError(
                    "区域未闭合: 终点 %s 与起点 %s 不一致"
                    % (last, first), ev.line, ev.source)
            ring = region_pts[:-1]
            if len(ring) < 3:
                raise GerberError("区域未闭合: 顶点不足", ev.line, ev.source)
            poly = Polygon(ring)
            if not poly.is_valid or not poly.exterior.is_simple:
                raise GerberError("区域轮廓自交, 非法多边形",
                                  region_start_line, region_start_src)
            expose(orient(poly, sign=1.0))
            region_pts = None
        # select_aperture / eof 无需处理

    if copper is None:
        from shapely.geometry import GeometryCollection
        return GeometryCollection()
    if copper.is_empty:
        return copper
    return copper.buffer(0)  # 清理潜在拓扑瑕疵


def summarize(geom):
    """面积 / 包围盒 / 连通块数 / 孔洞数。"""
    if geom.is_empty:
        return {
            "area_mm2": 0.0,
            "bbox_mm": None,
            "components": 0,
            "holes": 0,
        }
    polys = []
    for g in getattr(geom, "geoms", [geom]):
        if g.geom_type == "Polygon" and not g.is_empty:
            polys.append(g)
    minx, miny, maxx, maxy = geom.bounds
    return {
        "area_mm2": round(geom.area, 6),
        "bbox_mm": {
            "min_x": round(minx, 6), "min_y": round(miny, 6),
            "max_x": round(maxx, 6), "max_y": round(maxy, 6),
            "width": round(maxx - minx, 6), "height": round(maxy - miny, 6),
        },
        "components": len(polys),
        "holes": sum(len(p.interiors) for p in polys),
    }
