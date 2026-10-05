
"""FastAPI 请求处理层: 上传、铜层重建、SVG 下载、双层网表核对、制造规则审查。"""
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from copper_rev208.drc import audit, load_rules
from copper_rev208.errors import GerberError
from copper_rev208.geometry import build_geometry, summarize
from copper_rev208.netcheck import (NetlistError, analyze, connectivity,
                                    load_netlist, report)
from copper_rev208.parser import Parser
from copper_rev208.revision import RevisionError, review_revision, validate_transform
from copper_rev208.revision_svg import copper_changes_to_svg
from copper_rev208.svg_export import geometry_to_svg

app = FastAPI(title="Gerber Revision Review 208")

# svg_id -> (svg_text, stats)
_results = {}
_revision_results = {}


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


async def _load_named_netlist(netlist, label):
    try:
        return load_netlist(await netlist.read())
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": "%s网表: %s" % (label, exc.message),
            "position": exc.position})


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


@app.post("/api/revision-review")
async def revision_review(
    old_top: UploadFile = File(...), old_bottom: UploadFile = File(...),
    new_top: UploadFile = File(...), new_bottom: UploadFile = File(...),
    old_netlist: UploadFile = File(...), new_netlist: UploadFile = File(...),
    rules: UploadFile = File(...), dx_mm: float = Form(...),
    dy_mm: float = Form(...), rotation_deg: float = Form(...),
    tolerance: float = Form(0.01)):
    if tolerance <= 0:
        raise HTTPException(422, "tolerance 必须为正数(毫米)")
    try:
        dx, dy, rotation = validate_transform(dx_mm, dy_mm, rotation_deg)
    except RevisionError as exc:
        raise HTTPException(422, {"error": exc.message, "position": exc.position})
    old_geoms = await _build_layers(old_top, old_bottom, tolerance)
    new_geoms = await _build_layers(new_top, new_bottom, tolerance)
    old_nets = await _load_named_netlist(old_netlist, "旧版")
    new_nets = await _load_named_netlist(new_netlist, "新版")
    try:
        rule_set = load_rules(await rules.read(), {t.net for t in old_nets[0]})
    except NetlistError as exc:
        raise HTTPException(422, {"error": exc.message, "position": exc.position})
    try:
        result = review_revision(
            {"top": old_geoms["顶层"], "bottom": old_geoms["底层"]},
            {"top": new_geoms["顶层"], "bottom": new_geoms["底层"]},
            old_nets, new_nets, rule_set, dx, dy, rotation, tolerance)
    except NetlistError as exc:
        raise HTTPException(422, {"error": exc.message, "position": exc.position})
    svg = copper_changes_to_svg(result.pop("_svg_layers"))
    svg_id = uuid.uuid4().hex
    _revision_results[svg_id] = svg
    result["svg_id"] = svg_id
    result["svg_url"] = "/api/revision-review/%s/svg" % svg_id
    return result


@app.get("/api/revision-review/{svg_id}/svg")
async def download_revision_svg(svg_id: str):
    svg = _revision_results.get(svg_id)
    if svg is None:
        raise HTTPException(404, "结果不存在或已过期")
    return Response(svg, media_type="image/svg+xml", headers={
        "Content-Disposition":
        'attachment; filename="revision_%s.svg"' % svg_id[:8]})
