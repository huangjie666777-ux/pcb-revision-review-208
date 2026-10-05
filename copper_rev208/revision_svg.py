"""工程变更分层 SVG: 加铜、删铜和共有铜统一绘制在旧版坐标系。"""
_FMT = "{:.4f}"
_COLORS = {"added": "#2ca02c", "removed": "#d62728", "common": "#7f7f7f"}


def _fmt(value):
    text = _FMT.format(value).rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _ring_path(coords, max_y):
    path = []
    for index, (x, y) in enumerate(coords):
        path.append("%s%s %s" % ("M" if index == 0 else "L",
                                  _fmt(x), _fmt(max_y - y)))
    return "".join(path) + "Z"


def _geometry_paths(geom, color, max_y):
    if geom.is_empty:
        return []
    paths = []
    for polygon in getattr(geom, "geoms", [geom]):
        if polygon.geom_type != "Polygon" or polygon.is_empty:
            continue
        data = _ring_path(polygon.exterior.coords, max_y)
        for interior in polygon.interiors:
            data += _ring_path(interior.coords, max_y)
        paths.append('<path d="%s" fill="%s" fill-rule="evenodd"/>'
                     % (data, color))
    return paths


def copper_changes_to_svg(layers):
    bounds = [geom.bounds for layer in layers.values()
              for geom in layer["_geometries"].values() if not geom.is_empty]
    if not bounds:
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1" '
                'width="1mm" height="1mm"/>\n')
    min_x = min(b[0] for b in bounds)
    max_x = max(b[2] for b in bounds)
    max_y = max(b[3] for b in bounds)
    width = max(max_x - min_x, 1e-9)
    height = max(max(b[3] - b[1] for b in bounds), 1e-9)
    groups = []
    for layer in ("top", "bottom"):
        paths = []
        for kind in ("common", "removed", "added"):
            paths += _geometry_paths(layers[layer]["_geometries"][kind],
                                     _COLORS[kind], max_y)
        groups.append('<g id="%s" transform="translate(%s,0)">%s</g>'
                      % (layer, _fmt(-min_x), "".join(paths)))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s" '
            'width="%smm" height="%smm">\n%s\n</svg>\n'
            % (_fmt(width), _fmt(height), _fmt(width), _fmt(height),
               "".join(groups)))
