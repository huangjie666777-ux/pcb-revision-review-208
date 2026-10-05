
"""FastAPI 请求处理层: 上传、铜层重建、SVG 下载、双层网表核对、
制造规则审查、工程变更评审。"""
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from copper_rev208.drc import audit, load_rules
from copper_rev208.errors import GerberError
from copper_rev208.geometry import build_geometry, summarize
from copper_rev208.netcheck import (NetlistError, analyze, connectivity,
                                    load_netlist, report)
from copper_rev208.parser import Parser
from copper_rev208.review import (compare_connectivity, layer_diff,
                                  match_revisions, region_detail,
                                  revision_reports, transform_geometry,
                                  transform_holes, transform_terminals,
                                  validate_transform)
from copper_rev208.svg_export import diff_to_svg, geometry_to_svg

app = FastAPI(title="Gerber Revision Review 208")

# svg_id -> (svg_text, stats)
_results = {}
# review_id -> {"top": svg, "bottom": svg}
_reviews = {}


def _rebuild(text, tolerance):
    parser = Parser(text)
    events = parser.parse()
    geom = build_geometry(events, parser.apertures, tolerance)
    stats = summarize(geom)
    svg = geometry_to_svg(geom)
    return stats, svg, geom


@app.post("/api/rebuild")
async def rebuild(file: UploadFile = File(...),
                  tolerance: float = Form(0.01)):
    if tolerance <= 0:
        raise HTTPException(422, "tolerance 必须为正数(毫米)")
    raw = await file.read()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(422, "仅支持 ASCII 编码的 Gerber 文件")
    try:
        stats, svg, _geom = _rebuild(text, tolerance)
    except GerberError as exc:
        raise HTTPException(422, {
            "error": exc.message, "line": exc.line, "source": exc.source})
    svg_id = uuid.uuid4().hex
    _results[svg_id] = svg
    stats["svg_id"] = svg_id
    stats["svg_url"] = "/api/rebuild/%s/svg" % svg_id
    return stats


@app.get("/api/rebuild/{svg_id}/svg")
async def download_svg(svg_id: str):
    svg = _results.get(svg_id)
    if svg is None:
        raise HTTPException(404, "结果不存在或已过期")
    return Response(
        svg, media_type="image/svg+xml",
        headers={"Content-Disposition":
                 'attachment; filename="copper_%s.svg"' % svg_id[:8]})


async def _build_layers(top, bottom, tolerance):
    if tolerance <= 0:
        raise HTTPException(422, "tolerance 必须为正数(毫米)")
    geoms = {}
    for label, upload in (("顶层", top), ("底层", bottom)):
        raw = await upload.read()
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            raise HTTPException(422, "%s Gerber 仅支持 ASCII 编码" % label)
        try:
            parser = Parser(text)
            events = parser.parse()
            geoms[label] = build_geometry(events, parser.apertures, tolerance)
        except GerberError as exc:
            raise HTTPException(422, {
                "error": "%s Gerber: %s" % (label, exc.message),
                "line": exc.line, "source": exc.source})
    return geoms


async def _load_netlist_upload(netlist):
    raw = await netlist.read()
    try:
        return load_netlist(raw)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})


@app.post("/api/netcheck")
async def netcheck(top: UploadFile = File(...),
                   bottom: UploadFile = File(...),
                   netlist: UploadFile = File(...),
                   tolerance: float = Form(0.01)):
    geoms = await _build_layers(top, bottom, tolerance)
    terminals, holes = await _load_netlist_upload(netlist)
    try:
        return analyze(geoms["顶层"], geoms["底层"], terminals, holes,
                       tolerance)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})


@app.post("/api/drc")
async def drc(top: UploadFile = File(...),
              bottom: UploadFile = File(...),
              netlist: UploadFile = File(...),
              rules: UploadFile = File(...),
              tolerance: float = Form(0.01)):
    geoms = await _build_layers(top, bottom, tolerance)
    terminals, holes = await _load_netlist_upload(netlist)
    raw_rules = await rules.read()
    known_nets = {t.net for t in terminals}
    try:
        rule_set = load_rules(raw_rules, known_nets)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})
    try:
        conn = connectivity(geoms["顶层"], geoms["底层"], terminals, holes,
                            tolerance)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})
    result = audit(conn, terminals, *rule_set)
    net_report = report(conn, terminals)
    result["netcheck"] = net_report
    result["ok"] = result["ok"] and net_report["ok"]
    return result


async def _load_rules_upload(rules, known_nets):
    raw_rules = await rules.read()
    try:
        return load_rules(raw_rules, known_nets)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})


@app.post("/api/review")
async def review(old_top: UploadFile = File(...),
                 old_bottom: UploadFile = File(...),
                 old_netlist: UploadFile = File(...),
                 new_top: UploadFile = File(...),
                 new_bottom: UploadFile = File(...),
                 new_netlist: UploadFile = File(...),
                 rules: UploadFile = File(...),
                 rotation_deg: float = Form(...),
                 translate_x: float = Form(...),
                 translate_y: float = Form(...),
                 tolerance: float = Form(0.01)):
    try:
        rotation, dx, dy = validate_transform(rotation_deg, translate_x,
                                              translate_y)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})
    old_geoms = await _build_layers(old_top, old_bottom, tolerance)
    new_geoms = await _build_layers(new_top, new_bottom, tolerance)
    old_terminals, old_holes = await _load_netlist_upload(old_netlist)
    new_terminals, new_holes = await _load_netlist_upload(new_netlist)
    try:
        match_revisions(old_terminals, new_terminals)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})
    rule_set = await _load_rules_upload(
        rules, {t.net for t in old_terminals})

    # 新版铜/端子/孔整体变换到旧版坐标系(层不交换, 不自动配准)
    new_top_geom = transform_geometry(new_geoms["顶层"], rotation, dx, dy)
    new_bottom_geom = transform_geometry(new_geoms["底层"], rotation, dx, dy)
    new_terminals = transform_terminals(new_terminals, rotation, dx, dy)
    new_holes = transform_holes(new_holes, rotation, dx, dy)

    try:
        old_conn, old_reports = revision_reports(
            old_geoms["顶层"], old_geoms["底层"], old_terminals, old_holes,
            tolerance, rule_set)
        new_conn, new_reports = revision_reports(
            new_top_geom, new_bottom_geom, new_terminals, new_holes,
            tolerance, rule_set)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})

    review_id = uuid.uuid4().hex
    layers = {}
    svgs = {}
    for layer, old_geom, new_geom in (
            ("top", old_geoms["顶层"], new_top_geom),
            ("bottom", old_geoms["底层"], new_bottom_geom)):
        diff = layer_diff(old_geom, new_geom)
        svgs[layer] = diff_to_svg(diff)
        layers[layer] = {
            "added": region_detail(diff["added"]),
            "removed": region_detail(diff["removed"]),
            "common": region_detail(diff["common"]),
            "svg_url": "/api/review/%s/svg/%s" % (review_id, layer),
        }
    _reviews[review_id] = svgs

    return {
        "review_id": review_id,
        "transform": {"rotation_deg": rotation,
                      "translate_x": dx, "translate_y": dy},
        "coordinate_system": "旧版坐标系",
        "layers": layers,
        "connectivity_changes": compare_connectivity(old_conn, new_conn),
        "old_revision": old_reports,
        "new_revision": new_reports,
        "ok": old_reports["ok"] and new_reports["ok"],
    }


@app.get("/api/review/{review_id}/svg/{layer}")
async def download_review_svg(review_id: str, layer: str):
    svgs = _reviews.get(review_id)
    if svgs is None or layer not in svgs:
        raise HTTPException(404, "结果不存在或已过期")
    return Response(
        svgs[layer], media_type="image/svg+xml",
        headers={"Content-Disposition":
                 'attachment; filename="review_%s_%s.svg"'
                 % (review_id[:8], layer)})
