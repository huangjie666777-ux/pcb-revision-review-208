
"""从最终铜层几何导出 SVG。

Gerber 坐标 Y 轴向上, SVG Y 轴向下, 导出时做镜像翻转;
孔洞通过 fill-rule="evenodd" 正确表达。
"""

_FMT = "{:.4f}"


def _fmt(v):
    s = _FMT.format(v).rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _ring_path(coords, max_y):
    parts = []
    for k, (x, y) in enumerate(coords):
        cmd = "M" if k == 0 else "L"
        parts.append("%s%s %s" % (cmd, _fmt(x), _fmt(max_y - y)))
    parts.append("Z")
    return "".join(parts)


def geometry_to_svg(geom):
    if geom.is_empty:
        return ('<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 1 1" width="1mm" height="1mm"/>\n')
    minx, miny, maxx, maxy = geom.bounds
    width = max(maxx - minx, 1e-9)
    height = max(maxy - miny, 1e-9)
    paths = []
    for g in getattr(geom, "geoms", [geom]):
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        d = _ring_path(g.exterior.coords, maxy)
        for interior in g.interiors:
            d += _ring_path(interior.coords, maxy)
        paths.append('<path d="%s"/>' % d)
    body = "".join(paths)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'viewBox="0 0 %s %s" width="%smm" height="%smm">\n'
        '<g transform="translate(%s,0)" fill="#b87333" '
        'fill-rule="evenodd" stroke="none">%s</g>\n</svg>\n'
        % (_fmt(width), _fmt(height), _fmt(width), _fmt(height),
           _fmt(-minx), body)
    )
