"""
边缘几何预校正模块。
在写入 Zemax 之前，对薄透镜近似产生的 LDE/MCE 数据做最小幅度修正，
保证镜片不交叉、空气间隔不为负、Zemax 能完成实光线追迹。
"""
from dataclasses import dataclass, field
from typing import List, Dict, Tuple
import math


@dataclass
class SurfaceGeom:
    """单个光学面几何参数。"""
    surf_idx: int          # Action_a 内部面号（不是 Zemax 面号）
    radius: float          # 曲率半径 R (mm)，0 或 |R|>1e10 表示平面
    thickness: float       # 当前厚度 CT (mm)
    semi_diameter: float   # 有效半口径 h (mm)
    is_glass: bool         # True=该面之后是玻璃，False=该面之后是空气
    group_id: int          # 所属镜组 G1/G2/G3/G4 (1-4)


@dataclass
class CorrectionReport:
    """预校正报告。"""
    ct_corrections: List[Tuple[int, float, float, float]] = field(default_factory=list)
    aet_corrections: List[Tuple[str, int, float, float, float]] = field(default_factory=list)
    ttl_before: Dict[str, float] = field(default_factory=dict)
    ttl_after: Dict[str, float] = field(default_factory=dict)
    ttl_inflation_pct: Dict[str, float] = field(default_factory=dict)
    aborted: bool = False
    abort_reason: str = ""


def compute_sag(radius: float, semi_diameter: float) -> float:
    """
    计算球面矢高。
    符号约定：R>0 表示曲率中心在面右侧（凸面朝左），sag>0 表示该面顶点位于
    其口径边缘的左侧（即面向右凸出）。
    平面或近平面（|R|>1e10 或 R==0）返回 0.0。
    h > |R| 时抛 ValueError。
    """
    if radius == 0.0 or abs(radius) > 1e10:
        return 0.0
    if semi_diameter > abs(radius):
        raise ValueError(
            f"半口径 {semi_diameter} 超过曲率半径绝对值 {abs(radius)}"
        )
    sign = 1.0 if radius > 0 else -1.0
    abs_r = abs(radius)
    sag_mag = abs_r - math.sqrt(abs_r * abs_r - semi_diameter * semi_diameter)
    return sign * sag_mag


def correct_edge_thickness(
    surfaces: List[SurfaceGeom],
    et_min: float,
    ct_min: float,
) -> Tuple[List[SurfaceGeom], List[Tuple[int, float, float, float]]]:
    """
    遍历所有玻璃片对，ET = CT - sag_前 + sag_后 < et_min 时增加 CT。
    同时保证 CT >= ct_min。
    返回修正后的面列表副本和修正记录 (surf_idx, ET_before, CT_old, CT_new)。
    """
    corrected = [
        SurfaceGeom(s.surf_idx, s.radius, s.thickness, s.semi_diameter,
                    s.is_glass, s.group_id)
        for s in surfaces
    ]
    records = []
    for i, s in enumerate(corrected):
        if not s.is_glass:
            continue
        if i + 1 >= len(corrected):
            continue
        s_next = corrected[i + 1]
        sag_front = compute_sag(s.radius, s.semi_diameter)
        sag_back = compute_sag(s_next.radius, s_next.semi_diameter)
        et_before = s.thickness - sag_front + sag_back
        ct_old = s.thickness
        ct_new = ct_old
        if et_before < et_min:
            ct_new = ct_old + (et_min - et_before)
        if ct_new < ct_min:
            ct_new = ct_min
        if ct_new != ct_old:
            records.append((s.surf_idx, et_before, ct_old, ct_new))
            s.thickness = ct_new
    return corrected, records


def correct_air_edge_gap(
    surfaces: List[SurfaceGeom],
    config_spacings: Dict[str, Dict[int, float]],
    aet_min: float,
) -> Tuple[Dict[str, Dict[int, float]], List[Tuple[str, int, float, float, float]]]:
    """
    对每个构型每个变焦空气间隔 d_k (k=1,2,3)：
    AET = d - sag_后(前一组末面) - sag_前(后一组首面) < aet_min 时抬高 d。
    返回修正后的 spacings 副本和修正记录
    (config_name, gap_idx, AET_before, d_old, d_new)。

    sag 取"向间隔内侵入"为正：前一组末面凸面朝右 (R<0) 时 sag_后 为正；
    后一组首面凸面朝左 (R>0) 时 sag_前 为正。
    """
    group_last_surf = {}
    group_first_surf = {}
    for s in surfaces:
        if s.group_id not in group_first_surf:
            group_first_surf[s.group_id] = s
        group_last_surf[s.group_id] = s

    gap_to_groups = {1: (1, 2), 2: (2, 3), 3: (3, 4)}

    corrected = {cfg: dict(d) for cfg, d in config_spacings.items()}
    records = []

    for cfg_name, spacings in corrected.items():
        for gap_idx, (g_left, g_right) in gap_to_groups.items():
            if gap_idx not in spacings:
                continue
            s_left_last = group_last_surf.get(g_left)
            s_right_first = group_first_surf.get(g_right)
            if s_left_last is None or s_right_first is None:
                continue
            sag_left = -compute_sag(s_left_last.radius, s_left_last.semi_diameter)
            sag_right = compute_sag(s_right_first.radius, s_right_first.semi_diameter)
            sag_in_left = max(0.0, sag_left)
            sag_in_right = max(0.0, sag_right)
            d_old = spacings[gap_idx]
            aet_before = d_old - sag_in_left - sag_in_right
            if aet_before < aet_min:
                d_new = d_old + (aet_min - aet_before)
                records.append((cfg_name, gap_idx, aet_before, d_old, d_new))
                spacings[gap_idx] = d_new
    return corrected, records


def _compute_ttl(
    surfaces: List[SurfaceGeom],
    spacings: Dict[int, float],
) -> float:
    """TTL = 所有面 CT 之和（玻璃面）+ d1+d2+d3。"""
    glass_sum = sum(s.thickness for s in surfaces if s.is_glass)
    air_sum = sum(spacings.get(k, 0.0) for k in (1, 2, 3))
    return glass_sum + air_sum


def enforce_edge_geometry(
    surfaces: List[SurfaceGeom],
    config_spacings: Dict[str, Dict[int, float]],
    config: dict,
) -> Tuple[List[SurfaceGeom], Dict[str, Dict[int, float]], CorrectionReport]:
    """
    主入口：ET 校正 → AET 校正 → TTL 膨胀检查。
    config 为 EDGE_GEOMETRY 字典。
    """
    report = CorrectionReport()

    for cfg_name, sp in config_spacings.items():
        report.ttl_before[cfg_name] = _compute_ttl(surfaces, sp)

    surfaces_after, ct_records = correct_edge_thickness(
        surfaces, config["ET_MIN_MM"], config["CT_MIN_MM"]
    )
    report.ct_corrections = ct_records

    spacings_after, aet_records = correct_air_edge_gap(
        surfaces_after, config_spacings, config["AET_MIN_MM"]
    )
    report.aet_corrections = aet_records

    for cfg_name, sp in spacings_after.items():
        ttl_after = _compute_ttl(surfaces_after, sp)
        report.ttl_after[cfg_name] = ttl_after
        ttl_before = report.ttl_before[cfg_name]
        inflation = (ttl_after - ttl_before) / ttl_before if ttl_before > 0 else 0.0
        report.ttl_inflation_pct[cfg_name] = inflation
        if inflation > config["TTL_INFLATION_ABORT"]:
            report.aborted = True
            report.abort_reason = (
                f"构型 {cfg_name} TTL 膨胀 {inflation*100:.1f}%, "
                f"超过硬上限 {config['TTL_INFLATION_ABORT']*100:.1f}%"
            )

    return surfaces_after, spacings_after, report


def print_report(report: CorrectionReport) -> None:
    """打印格式化报告到 stdout。"""
    print("=" * 60)
    print("边缘几何预校正报告")
    print("=" * 60)

    if report.ct_corrections:
        print(f"\n[ET 校正] 共修正 {len(report.ct_corrections)} 片玻璃中心厚度:")
        for surf_idx, et_before, ct_old, ct_new in report.ct_corrections:
            delta = ct_new - ct_old
            print(f"  Surf {surf_idx:>3}  ET_before={et_before:+.2f}  "
                  f"CT: {ct_old:.2f} -> {ct_new:.2f} mm  ({delta:+.2f})")
    else:
        print("\n[ET 校正] 无需修正")

    if report.aet_corrections:
        print(f"\n[AET 校正] 共修正 {len(report.aet_corrections)} 处空气间隔:")
        for cfg_name, gap_idx, aet_before, d_old, d_new in report.aet_corrections:
            delta = d_new - d_old
            print(f"  {cfg_name:<5} d{gap_idx}  AET_before={aet_before:+.2f}  "
                  f"d: {d_old:.2f} -> {d_new:.2f} mm  ({delta:+.2f})")
    else:
        print("\n[AET 校正] 无需修正")

    print("\n[TTL 膨胀]")
    for cfg_name in report.ttl_before:
        before = report.ttl_before[cfg_name]
        after = report.ttl_after[cfg_name]
        pct = report.ttl_inflation_pct[cfg_name] * 100
        print(f"  {cfg_name:<6}: {before:.2f} -> {after:.2f} mm  ({pct:+.2f}%)")

    print()
    if report.aborted:
        print(f"状态: ABORT — {report.abort_reason}")
    else:
        print("状态: OK")
    print("=" * 60)


def compute_auto_within_group_spacings(
    n_lenses: int,
    cemented_pairs: List[Tuple[int, int]],
    min_r_mm: float,
    D_mm: float,
    margin_mm: float = 1.0,
) -> List[float]:
    """
    根据 min_r_mm 和组口径 D，自动估算组内片间最小安全空气间距。

    返回长度为 n_lenses 的列表：
        - 第 i 个元素 = 片 i 后表面到片 i+1 前表面的间距（mm）
        - i = n_lenses - 1（最末片之后）按惯例填 0.0
        - 胶合对中前一片对应位置填 0.0（与现有 spacings_mm 中 0.0=胶合 的语义一致）

    估算公式（保守上界）：
        h = D_mm / 2
        sag_max = min_r_mm - sqrt(min_r_mm² - h²)
        spacing_safe = 2 * sag_max + margin_mm

    说明：
        - 假设最不利情形：相邻两面均凸面相对，各贡献一个 sag。
        - 实际 search 出的 R 一般 >= min_r_mm，sag 更小，因此该值是松上界。
        - 对单透镜片间和胶合对外的片间统一应用，胶合对内部不动。

    异常：
        - 若 D_mm/2 > min_r_mm，几何不可行，抛 ValueError。
    """
    h = D_mm / 2.0
    if h > min_r_mm:
        raise ValueError(
            f"半口径 D/2 = {h:.3f} mm 超过 min_r_mm = {min_r_mm:.3f} mm，几何不可行"
        )

    sag_max = min_r_mm - math.sqrt(min_r_mm * min_r_mm - h * h)
    spacing_safe = round(2.0 * sag_max + margin_mm, 2)

    # 胶合对中"前一片"对应的间距位置应填 0.0
    cem_first_set = {pair[0] for pair in cemented_pairs}

    spacings: List[float] = []
    for i in range(n_lenses):
        if i == n_lenses - 1:
            spacings.append(0.0)
        elif i in cem_first_set:
            spacings.append(0.0)
        else:
            spacings.append(spacing_safe)

    return spacings
