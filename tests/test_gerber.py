
import math

import pytest

from copper_rev208.errors import GerberError
from copper_rev208.geometry import arc_points, build_geometry, summarize
from copper_rev208.parser import Parser
from copper_rev208.svg_export import geometry_to_svg

HEADER = "%FSLAX24Y24*%\n%MOMM*%\n"


def run(body, tol=0.01):
    p = Parser(HEADER + body + "\nM02*\n")
    events = p.parse()
    return build_geometry(events, p.apertures, tol)


def test_region_square_area():
    geom = run("%ADD10C,0.5*%\nG36*\nX0Y0D02*\nX100000Y0D01*\n"
               "X100000Y100000D01*\nX0Y100000D01*\nX0Y0D01*\nG37*")
    assert geom.area == pytest.approx(100.0)


def test_circle_flash_area():
    geom = run("%ADD10C,2.0*%\nD10*\nX5000Y5000D03*", tol=0.001)
    assert geom.area == pytest.approx(math.pi, rel=1e-3)


def test_rect_flash():
    geom = run("%ADD10R,3.0X2.0*%\nD10*\nX0Y0D03*")
    assert geom.area == pytest.approx(6.0)
    assert geom.bounds == pytest.approx((-1.5, -1.0, 1.5, 1.0))


def test_line_stroke_overlap_no_double_count():
    # 同一条 10mm x 1mm 线段画两次, 面积不得重复累计
    body = ("%ADD10C,1.0*%\nD10*\nG01*\nX0Y0D02*\nX100000Y0D01*\n"
            "X0Y0D02*\nX100000Y0D01*")
    geom = run(body)
    assert geom.area == pytest.approx(10 + math.pi / 4, abs=0.01)


def test_quarter_arc_ccw():
    pts = arc_points(1, 0, 0, 1, -1, 0, "G03", 0.001, 1, "t")
    assert len(pts) > 10
    for x, y in pts:
        assert math.hypot(x, y) == pytest.approx(1, abs=2e-3)
    assert pts[-1] == (0, 1)


def test_cw_arc_crosses_quadrants():
    # 顺时针从 (1,0) 到 (0,1): 应走 270 度大圆弧
    pts = arc_points(1, 0, 0, 1, -1, 0, "G02", 0.01, 1, "t")
    angles = [math.atan2(y, x) % (2 * math.pi) for x, y in pts]
    assert max(angles) > 4.0  # 经过第四/三/二象限


def test_full_circle_arc():
    pts = arc_points(1, 0, 1, 0, -1, 0, "G03", 0.01, 1, "t")
    xs = [p[0] for p in pts]
    assert min(xs) == pytest.approx(-1, abs=0.01)


def test_arc_radius_mismatch_rejected():
    with pytest.raises(GerberError):
        arc_points(0, 0, 3, 4, 1, 0, "G03", 0.01, 1, "t")


def test_lpc_subtract_then_lpd_restore():
    body = ("%ADD10C,2.0*%\nD10*\n%LPD*%\nX0Y0D03*\n"
            "%LPC*%\nX0Y0D03*\n%LPD*%\nX0Y0D03*")
    geom = run(body, tol=0.001)
    assert geom.area == pytest.approx(math.pi, rel=1e-3)


def test_lpc_creates_hole():
    body = ("%ADD10C,1.0*%\n%LPD*%\nG36*\nX0Y0D02*\nX200000Y0D01*\n"
            "X200000Y200000D01*\nX0Y200000D01*\nX0Y0D01*\nG37*\n"
            "%LPC*%\n%ADD11C,4.0*%\nD11*\nX100000Y100000D03*")
    geom = run(body)
    stats = summarize(geom)
    assert stats["holes"] == 1
    assert stats["components"] == 1
    assert stats["area_mm2"] == pytest.approx(400 - 4 * math.pi, rel=1e-3)


def test_inch_units():
    p = Parser("%FSLAX24Y24*%\n%MOIN*%\n%ADD10C,0.1*%\nD10*\nX0Y0D03*\nM02*\n")
    geom = build_geometry(p.parse(), p.apertures, 0.001)
    assert geom.area == pytest.approx(math.pi * (2.54 / 2) ** 2, rel=1e-3)


def test_modal_coordinates_and_d01():
    geom = run("%ADD10C,1.0*%\nD10*\nG01X0Y0D02*\nX100000D01*\nY100000*")
    assert geom.bounds[2] == pytest.approx(10.5, abs=0.01)
    assert geom.bounds[3] == pytest.approx(10.5, abs=0.01)


def test_undefined_aperture_error():
    with pytest.raises(GerberError) as e:
        run("D10*\nX0Y0D03*")
    assert "未定义的光圈" in str(e.value)
    assert e.value.line is not None


def test_unclosed_region_error():
    with pytest.raises(GerberError) as e:
        run("%ADD10C,0.5*%\nG36*\nX0Y0D02*\nX100000Y0D01*\n"
            "X100000Y100000D01*\nG37*")
    assert "未闭合" in str(e.value)


def test_self_intersecting_region_error():
    with pytest.raises(GerberError) as e:
        run("%ADD10C,0.5*%\nG36*\nX0Y0D02*\nX100000Y0D01*\n"
            "X100000Y100000D01*\nX0Y100000D01*\nX100000Y0D01*\nX0Y0D01*\nG37*")
    assert "自交" in str(e.value)


def test_truncated_file_error():
    with pytest.raises(GerberError) as e:
        Parser(HEADER + "%ADD10C,0.5*%\nD10*\nX0Y0D03*\n").parse()
    assert "截断" in str(e.value)


def test_unsupported_command_error():
    with pytest.raises(GerberError):
        run("G74*")
    with pytest.raises(GerberError):
        run("%ADD10C,0.5*%\nM01*")


def test_rect_aperture_draw_rejected():
    with pytest.raises(GerberError):
        run("%ADD10R,1.0X1.0*%\nD10*\nG01*\nX0Y0D02*\nX100000Y0D01*")


def test_empty_copper():
    geom = run("%ADD10C,0.5*%\nX0Y0D02*")
    stats = summarize(geom)
    assert stats["area_mm2"] == 0.0
    assert stats["bbox_mm"] is None
    svg = geometry_to_svg(geom)
    assert svg.startswith("<svg")


def test_svg_holes_and_orientation():
    body = ("%ADD10C,1.0*%\n%LPD*%\nG36*\nX0Y0D02*\nX200000Y0D01*\n"
            "X200000Y200000D01*\nX0Y200000D01*\nX0Y0D01*\nG37*\n"
            "%LPC*%\n%ADD11C,4.0*%\nD11*\nX100000Y100000D03*")
    geom = run(body)
    svg = geometry_to_svg(geom)
    assert 'fill-rule="evenodd"' in svg
    assert svg.count("M") >= 2  # 外环 + 孔
    assert 'viewBox="0 0 20 20"' in svg
