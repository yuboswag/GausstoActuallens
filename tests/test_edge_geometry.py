"""edge_geometry 模块单元测试。"""
import math
import pytest
from edge_geometry import (
    SurfaceGeom,
    compute_sag,
    correct_edge_thickness,
    correct_air_edge_gap,
    enforce_edge_geometry,
)


def test_compute_sag_convex():
    """R=+50, h=10 凸面 sag 约 +1.005 mm。"""
    sag = compute_sag(50.0, 10.0)
    assert abs(sag - 1.0102) < 1e-3


def test_compute_sag_concave():
    """R=-50, h=10 凹面 sag 约 -1.005 mm。"""
    sag = compute_sag(-50.0, 10.0)
    assert abs(sag + 1.0102) < 1e-3


def test_compute_sag_plane_zero_radius():
    assert compute_sag(0.0, 10.0) == 0.0


def test_compute_sag_plane_huge_radius():
    assert compute_sag(1e15, 10.0) == 0.0


def test_compute_sag_aperture_too_large():
    with pytest.raises(ValueError):
        compute_sag(5.0, 10.0)


def test_et_correction_ok_passthrough():
    """ET 已合规时 CT 不变。"""
    surfaces = [
        SurfaceGeom(1, 100.0, 3.0, 5.0, True, 1),
        SurfaceGeom(2, -100.0, 1.0, 5.0, False, 1),
    ]
    out, recs = correct_edge_thickness(surfaces, et_min=0.4, ct_min=1.0)
    assert len(recs) == 0
    assert out[0].thickness == 3.0


def test_et_correction_fixes_negative():
    """ET 为负时 CT 被增大到使 ET=ET_MIN。"""
    surfaces = [
        SurfaceGeom(1, 20.0, 1.0, 10.0, True, 1),
        SurfaceGeom(2, -20.0, 1.0, 10.0, False, 1),
    ]
    sag1 = compute_sag(20.0, 10.0)
    sag2 = compute_sag(-20.0, 10.0)
    et_before = 1.0 - sag1 + sag2
    assert et_before < 0
    out, recs = correct_edge_thickness(surfaces, et_min=0.4, ct_min=1.0)
    assert len(recs) == 1
    et_after = out[0].thickness - sag1 + sag2
    assert abs(et_after - 0.4) < 1e-6


def test_aet_correction_only_offending_config():
    """只有 Tele 构型 d2 不合规时只改 Tele。"""
    surfaces = [
        SurfaceGeom(1, 30.0, 2.0, 8.0, True, 1),
        SurfaceGeom(2, -30.0, 0.0, 8.0, False, 1),
        SurfaceGeom(3, 25.0, 2.0, 7.0, True, 2),
        SurfaceGeom(4, -25.0, 0.0, 7.0, False, 2),
        SurfaceGeom(5, 20.0, 2.0, 6.0, True, 3),
        SurfaceGeom(6, -20.0, 0.0, 6.0, False, 3),
        SurfaceGeom(7, 40.0, 2.0, 5.0, True, 4),
        SurfaceGeom(8, -40.0, 0.0, 5.0, False, 4),
    ]
    config_spacings = {
        "Wide": {1: 5.0, 2: 5.0, 3: 5.0},
        "Tele": {1: 5.0, 2: 0.5, 3: 5.0},
    }
    out, recs = correct_air_edge_gap(surfaces, config_spacings, aet_min=0.15)
    assert all(rec[0] == "Tele" and rec[1] == 2 for rec in recs)
    assert out["Wide"][2] == 5.0
    assert out["Tele"][2] > 0.5


def test_ttl_inflation_within_limits():
    """简单案例下 TTL 膨胀计算正确。"""
    surfaces = [
        SurfaceGeom(1, 100.0, 3.0, 5.0, True, 1),
        SurfaceGeom(2, -100.0, 0.0, 5.0, False, 1),
        SurfaceGeom(3, 100.0, 3.0, 5.0, True, 2),
        SurfaceGeom(4, -100.0, 0.0, 5.0, False, 2),
        SurfaceGeom(5, 100.0, 3.0, 5.0, True, 3),
        SurfaceGeom(6, -100.0, 0.0, 5.0, False, 3),
        SurfaceGeom(7, 100.0, 3.0, 5.0, True, 4),
        SurfaceGeom(8, -100.0, 0.0, 5.0, False, 4),
    ]
    config_spacings = {"Wide": {1: 5.0, 2: 5.0, 3: 5.0}}
    cfg = {
        "ET_MIN_MM": 0.4,
        "AET_MIN_MM": 0.15,
        "CT_MIN_MM": 1.0,
        "TTL_INFLATION_WARN": 0.05,
        "TTL_INFLATION_ABORT": 0.15,
    }
    _, _, report = enforce_edge_geometry(surfaces, config_spacings, cfg)
    assert not report.aborted
    assert "Wide" in report.ttl_before
    assert "Wide" in report.ttl_after
