"""双层板网表核对: 铜岛提取、镀铜孔连通图、短/断路报告。

复用 parser/geometry 重建的顶/底铜层(同一板坐标系, 不做镜像),
先在各层扣除全部孔盘形成铜岛(同层仅点接触不导通),
再按镀铜孔壁与铜岛的正长度接触建立跨层连通图(点接触不算,
非镀铜孔只扣铜, 顶底平面重叠不导通, 支持多孔传递连接),
最后对照 JSON 网表给出实际网络、短路(含铜岛-孔连接链)
与断路(分离端子组)。任何校验失败都抛出错误, 不交付部分结果。
"""
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass

from shapely.geometry import Point

from .errors import GerberError
from .geometry import circle_quad_segs

_CONTACT_EPS = 1e-9  # 孔壁接触正长度阈值(mm)


class NetlistError(GerberError):
    """网表 JSON 校验错误, position 指向 JSON 内位置。"""

    def __init__(self, message, position=None):
        self.position = position
        super().__init__(message, None, position)


@dataclass
class Terminal:
    id: str
    net: str
    layer: str
    x: float
    y: float
    pos: str


@dataclass
class Hole:
    id: str
    x: float
    y: float
    diameter: float
    plated: bool
    pos: str


def _finite_number(value, pos, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NetlistError("%s 必须为数字" % field, pos)
    if not math.isfinite(value):
        raise NetlistError("%s 必须为有限数值" % field, pos)
    return float(value)


def load_netlist(raw):
    """解析并校验网表 JSON 字节流, 返回 (terminals, holes)。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise NetlistError("网表文件必须为 UTF-8 编码的 JSON")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NetlistError("网表 JSON 解析失败: %s" % exc,
                           "line %d column %d" % (exc.lineno, exc.colno))
    if not isinstance(data, dict):
        raise NetlistError("网表须为 JSON 对象, 含 terminals 与 holes 数组")
    terminals_raw = data.get("terminals")
    holes_raw = data.get("holes")
    if not isinstance(terminals_raw, list):
        raise NetlistError("terminals 必须为数组", "terminals")
    if not isinstance(holes_raw, list):
        raise NetlistError("holes 必须为数组", "holes")

    seen_ids = set()

    def _unique_id(item, pos):
        if not isinstance(item, dict):
            raise NetlistError("条目须为 JSON 对象", pos)
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            raise NetlistError("id 必须为非空字符串", pos)
        if ident in seen_ids:
            raise NetlistError("重复的 ID %s" % ident, pos)
        seen_ids.add(ident)
        return ident

    terminals = []
    for idx, item in enumerate(terminals_raw):
        pos = "terminals[%d]" % idx
        ident = _unique_id(item, pos)
        net = item.get("net")
        if not isinstance(net, str) or not net:
            raise NetlistError("net 必须为非空字符串", pos)
        layer = item.get("layer")
        if layer not in ("top", "bottom"):
            raise NetlistError("layer 必须为 top 或 bottom", pos)
        x = _finite_number(item.get("x"), pos, "x")
        y = _finite_number(item.get("y"), pos, "y")
        terminals.append(Terminal(ident, net, layer, x, y, pos))

    holes = []
    for idx, item in enumerate(holes_raw):
        pos = "holes[%d]" % idx
        ident = _unique_id(item, pos)
        x = _finite_number(item.get("x"), pos, "x")
        y = _finite_number(item.get("y"), pos, "y")
        diameter = _finite_number(item.get("diameter"), pos, "diameter")
        if diameter <= 0:
            raise NetlistError("diameter 必须为正数", pos)
        plated = item.get("plated")
        if not isinstance(plated, bool):
            raise NetlistError("plated 必须为布尔值", pos)
        holes.append(Hole(ident, x, y, diameter, plated, pos))

    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            a, b = holes[i], holes[j]
            dist = math.hypot(a.x - b.x, a.y - b.y)
            if dist <= (a.diameter + b.diameter) / 2.0:
                raise NetlistError(
                    "孔 %s 与 %s 重叠或相切(中心距 %.6f mm)"
                    % (a.id, b.id, dist),
                    "%s / %s" % (a.pos, b.pos))
    return terminals, holes


def _islands(geom):
    islands = [g for g in getattr(geom, "geoms", [geom])
               if g.geom_type == "Polygon" and not g.is_empty]
    # 按包围盒排序, 保证铜岛编号与报告顺序确定
    islands.sort(key=lambda g: (g.bounds[0], g.bounds[1],
                                g.bounds[2], g.bounds[3]))
    return islands


def _label(node):
    kind = node[0]
    if kind == "hole":
        return node[1]
    return "%s#%d" % (node[1], node[2])


def connectivity(top_geom, bottom_geom, terminals, holes, tol):
    """构建扣孔铜岛与镀铜孔连通图, 供网表核对与制造规则审查复用。"""
    disks = {}
    for h in holes:
        disks[h.id] = Point(h.x, h.y).buffer(
            h.diameter / 2.0,
            quad_segs=circle_quad_segs(tol, h.diameter / 2.0))

    layers = {}
    for layer, geom in (("top", top_geom), ("bottom", bottom_geom)):
        g = geom
        for h in holes:
            g = g.difference(disks[h.id])
        if not g.is_empty:
            g = g.buffer(0)
        layers[layer] = _islands(g)

    adj = defaultdict(set)
    nodes = set()
    for layer in ("top", "bottom"):
        for idx in range(len(layers[layer])):
            nodes.add(("island", layer, idx))
    for h in holes:
        if not h.plated:
            continue
        ring = disks[h.id].exterior
        hnode = ("hole", h.id)
        for layer in ("top", "bottom"):
            for idx, island in enumerate(layers[layer]):
                shared = island.boundary.intersection(ring)
                if shared.length > _CONTACT_EPS:
                    inode = ("island", layer, idx)
                    adj[hnode].add(inode)
                    adj[inode].add(hnode)
                    nodes.add(hnode)

    comp_of = {}
    components = []
    for node in sorted(nodes):
        if node in comp_of:
            continue
        cid = len(components)
        queue = deque([node])
        comp_of[node] = cid
        members = []
        while queue:
            cur = queue.popleft()
            members.append(cur)
            for nxt in adj[cur]:
                if nxt not in comp_of:
                    comp_of[nxt] = cid
                    queue.append(nxt)
        components.append(members)

    term_node = {}
    unconnected = []
    for t in terminals:
        pt = Point(t.x, t.y)
        hits = [idx for idx, island in enumerate(layers[t.layer])
                if island.covers(pt)]
        if len(hits) > 1:
            raise NetlistError(
                "端子 %s 落在 %s 层多个铜岛交界处, 归属歧义"
                % (t.id, t.layer), t.pos)
        if not hits:
            unconnected.append(t)
        else:
            term_node[t.id] = ("island", t.layer, hits[0])

    return {
        "layers": layers,
        "adj": adj,
        "components": components,
        "comp_of": comp_of,
        "term_node": term_node,
        "unconnected": unconnected,
    }


def report(conn, terminals):
    """由连通图对照网表生成短/断路核对报告。"""
    layers = conn["layers"]
    adj = conn["adj"]
    components = conn["components"]
    comp_of = conn["comp_of"]
    term_node = conn["term_node"]
    unconnected = conn["unconnected"]

    term_by_id = {t.id: t for t in terminals}
    networks = []
    shorts = []
    for cid, members in enumerate(components):
        tids = sorted(tid for tid, node in term_node.items()
                      if comp_of[node] == cid)
        nets = sorted({term_by_id[tid].net for tid in tids})
        networks.append({
            "id": cid,
            "nets": nets,
            "terminals": tids,
            "islands": sorted(_label(n) for n in members
                              if n[0] == "island"),
            "holes": sorted(_label(n) for n in members
                            if n[0] == "hole"),
        })
        if len(nets) > 1:
            first = {}
            for net in nets:
                first[net] = next(tid for tid in tids
                                  if term_by_id[tid].net == net)
            path = _path(adj, term_node[first[nets[0]]],
                         term_node[first[nets[-1]]])
            shorts.append({
                "nets": nets,
                "terminals": tids,
                "chain": [_label(n) for n in path],
            })

    groups_by_net = defaultdict(lambda: defaultdict(list))
    for tid, node in term_node.items():
        groups_by_net[term_by_id[tid].net][comp_of[node]].append(tid)
    opens = []
    for net in sorted(groups_by_net):
        groups = groups_by_net[net]
        if len(groups) > 1:
            ordered = sorted((sorted(groups[cid]) for cid in groups),
                             key=lambda g: g[0])
            opens.append({
                "net": net,
                "groups": ordered,
            })

    return {
        "islands": {"top": len(layers["top"]),
                    "bottom": len(layers["bottom"])},
        "networks": networks,
        "unconnected_terminals": [
            {"id": t.id, "net": t.net, "layer": t.layer,
             "reason": "未落在 %s 层扣孔后的铜上" % t.layer}
            for t in unconnected
        ],
        "shorts": shorts,
        "opens": opens,
        "ok": not shorts and not opens,
    }


def analyze(top_geom, bottom_geom, terminals, holes, tol):
    """构建铜岛与镀铜孔连通图, 对照网表生成核对报告。"""
    return report(connectivity(top_geom, bottom_geom, terminals, holes, tol),
                  terminals)


def _path(adj, start, goal):
    """两点间最短路径(BFS), 返回节点序列; 不连通时返回 [start]。"""
    if start == goal:
        return [start]
    parent = {start: None}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in sorted(adj[cur]):
            if nxt in parent:
                continue
            parent[nxt] = cur
            if nxt == goal:
                path = [nxt]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                path.reverse()
                return path
            queue.append(nxt)
    return [start]
