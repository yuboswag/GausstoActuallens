"""
validate_geometry.py

对扁平面处方（SURFACE_PRESCRIPTION 格式）做几何可行性校验。
对每个变焦 config 单独跑，检查：
  1. 面级别：R/D 比、矢高可计算性
  2. 片级别：单片边缘厚度（中心厚 − 矢高有符号贡献）
  3. 组级别：胶合对总边缘厚度
  4. 间隙级别：相邻片边缘气隙

输入数据契约（与 test_bridge.py 中 SURFACE_PRESCRIPTION/ZOOM_CONFIGS 一致）：
  surface_prescription: List[(idx, desc, R, nd, t, glass)]
    - idx: 0-based 面序号
    - R: 曲率半径（mm），inf/极大表示平面
    - nd: 折射率，1.0 表示空气
    - t: 该面到下一面的距离（mm）
    - glass: 玻璃牌号，None 表示空气

  zoom_configs: List[(name, efl, d1, d2, d3, epd)]
    name: 配置名（"Wide"/"Tele"/...）
    d1/d2/d3: 变焦组之间的空气间距（mm）

  variable_thickness_idx: [d1_idx, d2_idx, d3_idx]
    SURFACE_PRESCRIPTION 中哪些 idx 的 thickness 在变焦时被覆盖。
    例如 [6, 13, 18]（0-based，对应 G1-L4后、G2-L4后、G3-L3后）。

输出：每个 config 一份 GeomReport，包含分级问题列表 + 修复建议。
"""

import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional


# ── 阈值参数（可按工艺标准调整）──────────────────────────
THRESH_RD_FAIL = 0.5         # R/D < 0.5：矢高 NaN，物理不可加工
THRESH_RD_WARN = 0.6         # R/D < 0.6：加工困难
THRESH_T_EDGE_FAIL = 0.0     # 边缘厚度 < 0：物理不可
THRESH_T_EDGE_WARN = 0.3     # 边缘厚度 < 0.3mm：机械风险
THRESH_GAP_EDGE_FAIL = 0.0   # 边缘气隙 < 0：相邻片交叉
THRESH_GAP_EDGE_WARN = 0.5   # 边缘气隙 < 0.5mm：装配风险
SAFETY_MARGIN = 1.0          # 修复建议时留的额外安全裕度（mm）


@dataclass
class GeomIssue:
    severity: str          # 'FAIL' / 'WARN'
    category: str          # 'surface_R' / 'lens_edge' / 'cement_edge' / 'gap_edge'
    location: str
    value: float
    threshold: float
    hint: str


@dataclass
class GeomReport:
    config_name: str
    issues: List[GeomIssue] = field(default_factory=list)

    @property
    def n_fail(self) -> int:
        return sum(1 for i in self.issues if i.severity == 'FAIL')

    @property
    def n_warn(self) -> int:
        return sum(1 for i in self.issues if i.severity == 'WARN')

    @property
    def is_ok(self) -> bool:
        return self.n_fail == 0 and self.n_warn == 0

    @property
    def is_buildable(self) -> bool:
        return self.n_fail == 0


# ── 矢高与有符号贡献 ───────────────────────────────────
def _sag(R: float, D: float) -> float:
    """球面矢高，平面返回 0，R<D/2 返回 NaN。"""
    if abs(R) > 1e6:
        return 0.0
    half_D = D / 2.0
    discriminant = R**2 - half_D**2
    if discriminant < 0:
        return float('nan')
    return abs(R) - np.sqrt(discriminant)


def _signed_contrib(R: float, D: float, is_front: bool) -> float:
    """
    某面对其所属"片中心 vs 边缘厚度差"的有符号贡献。
    > 0 表示该面让中心鼓出（使中心厚 > 边缘厚）。

    前表面：R > 0（凸朝光源） → 正贡献
    后表面：R < 0（凸朝像方） → 正贡献
    """
    s = _sag(R, D)
    if np.isnan(s):
        return float('nan')
    if is_front:
        return s if R > 0 else -s
    else:
        return s if R < 0 else -s


# ── 从扁平面处方推断片/胶合/气隙结构 ────────────────────
def _parse_prescription(prescription: List[tuple]) -> Dict:
    """
    扫描扁平面处方，构建结构化信息：
      lens_units: List[Dict]，每个单元（单片或胶合组）一项
        - 'kind': 'single' / 'cemented'
        - 'face_indices': 该单元在 prescription 中的 idx 列表
                         （单片2个、胶合3个）
        - 'lens_thicknesses': 各片中心厚度（单片1个、胶合2个）
        - 'glass_names': 各片玻璃名

      air_gaps: List[Dict]，相邻单元之间的空气间隔
        - 'after_unit': 在哪个 lens_unit 之后
        - 'face_idx': 该空气间隔对应 prescription 中的 idx
                     （即前一单元最后一面的 idx）
        - 'thickness': 当前 thickness 值（变焦时会被覆盖）
    """
    N = len(prescription)
    lens_units = []
    air_gaps = []

    i = 0
    while i < N:
        idx, desc, R, nd, t, glass = prescription[i]

        if glass is None:
            # 空气面：是某片的"后表面 + 后续空气间隔"，应该已被前一个单元处理掉
            # 如果到这里还没处理，说明前面解析出错（保险逻辑）
            i += 1
            continue

        # 当前面有玻璃 → 这是一个新单元的前表面
        # 看下一面：
        #   下一面 glass=None → 单片（本面=前表面，下一面=后表面）
        #   下一面 glass!=None → 胶合（本面=前表面，下一面=胶合面，再下一面=后表面）

        if i + 1 < N and prescription[i+1][5] is not None:
            # 胶合对
            if i + 2 >= N:
                raise ValueError(f"胶合对在 idx={idx} 处缺少后表面")
            front = prescription[i]      # 前表面
            cement = prescription[i+1]   # 胶合面
            back = prescription[i+2]     # 后表面（应为空气面）

            lens_units.append({
                'kind': 'cemented',
                'face_indices': [front[0], cement[0], back[0]],
                'lens_thicknesses': [front[4], cement[4]],  # 前片厚、后片厚
                'glass_names': [front[5], cement[5]],
                'desc_summary': f"胶合({front[5]}+{cement[5]})",
            })
            # 处理这个单元后面的空气间隔（back 这一面的 thickness）
            air_gaps.append({
                'after_unit': len(lens_units) - 1,
                'face_idx': back[0],
                'thickness': back[4],
            })
            i += 3
        else:
            # 单片
            if i + 1 >= N:
                raise ValueError(f"单片在 idx={idx} 处缺少后表面")
            front = prescription[i]
            back = prescription[i+1]

            lens_units.append({
                'kind': 'single',
                'face_indices': [front[0], back[0]],
                'lens_thicknesses': [front[4]],
                'glass_names': [front[5]],
                'desc_summary': f"片({front[5]})",
            })
            air_gaps.append({
                'after_unit': len(lens_units) - 1,
                'face_idx': back[0],
                'thickness': back[4],
            })
            i += 2

    return {'lens_units': lens_units, 'air_gaps': air_gaps}


# ── 应用变焦 config 的间距覆盖 ──────────────────────────
def _apply_zoom_overrides(parsed: Dict,
                          zoom_cfg: Tuple,
                          variable_thickness_idx: List[int]) -> Dict:
    """
    根据一个 zoom config (name, efl, d1, d2, d3, epd)，
    把变焦间距覆盖到对应的 air_gaps 上。
    返回新的 parsed 副本（不修改原数据）。
    """
    name, efl, d1, d2, d3, epd = zoom_cfg
    overrides = dict(zip(variable_thickness_idx, [d1, d2, d3]))

    new_air_gaps = []
    for gap in parsed['air_gaps']:
        new_gap = dict(gap)
        if gap['face_idx'] in overrides:
            new_gap['thickness'] = overrides[gap['face_idx']]
        new_air_gaps.append(new_gap)

    return {
        'lens_units': parsed['lens_units'],
        'air_gaps': new_air_gaps,
    }


# ── 校验：面 R/D ────────────────────────────────────────
def _check_surface_R(prescription: List[tuple], D: float,
                      issues: List[GeomIssue]):
    for idx, desc, R, nd, t, glass in prescription:
        if abs(R) > 1e6:
            continue
        rd = abs(R) / D
        if rd < THRESH_RD_FAIL:
            issues.append(GeomIssue(
                severity='FAIL', category='surface_R',
                location=f"面{idx}({desc})",
                value=rd, threshold=THRESH_RD_FAIL,
                hint=f"R={R:+.2f}mm < D/2={D/2:.1f}mm，矢高无定义。"
                     f"建议增大 min_R_mm 或更换光焦度更小的玻璃。"
            ))
        elif rd < THRESH_RD_WARN:
            issues.append(GeomIssue(
                severity='WARN', category='surface_R',
                location=f"面{idx}({desc})",
                value=rd, threshold=THRESH_RD_WARN,
                hint=f"R/D={rd:.2f} 偏小，加工难度高。"
            ))


# ── 校验：片/胶合对边缘厚度 ─────────────────────────────
def _check_lens_edges(prescription: List[tuple],
                      lens_units: List[Dict],
                      D: float,
                      issues: List[GeomIssue]):
    # 把 prescription 转成 dict 便于查找
    pr_by_idx = {p[0]: p for p in prescription}

    for unit in lens_units:
        if unit['kind'] == 'single':
            front_idx, back_idx = unit['face_indices']
            R1 = pr_by_idx[front_idx][2]
            R2 = pr_by_idx[back_idx][2]
            t_center = unit['lens_thicknesses'][0]

            c_front = _signed_contrib(R1, D, is_front=True)
            c_back = _signed_contrib(R2, D, is_front=False)

            if np.isnan(c_front) or np.isnan(c_back):
                issues.append(GeomIssue(
                    severity='FAIL', category='lens_edge',
                    location=unit['desc_summary'],
                    value=float('nan'), threshold=0.0,
                    hint='矢高无定义（R<D/2）。先解决 R 过小问题。'
                ))
                continue

            t_edge = t_center - c_front - c_back

            if t_edge < THRESH_T_EDGE_FAIL:
                deficit = -t_edge
                recommended = c_front + c_back + SAFETY_MARGIN
                issues.append(GeomIssue(
                    severity='FAIL', category='lens_edge',
                    location=unit['desc_summary'],
                    value=t_edge, threshold=THRESH_T_EDGE_FAIL,
                    hint=f"中心厚 {t_center:.2f}mm 不够覆盖矢高贡献"
                         f"(前{c_front:+.2f}+后{c_back:+.2f})，"
                         f"缺{deficit:.2f}mm。建议中心厚 ≥ {recommended:.2f}mm "
                         f"（修改 t_center_min 或 structure.py 厚度计算逻辑）。"
                ))
            elif t_edge < THRESH_T_EDGE_WARN:
                issues.append(GeomIssue(
                    severity='WARN', category='lens_edge',
                    location=unit['desc_summary'],
                    value=t_edge, threshold=THRESH_T_EDGE_WARN,
                    hint=f"边缘厚度 {t_edge:.2f}mm 偏薄，机械应力风险。"
                ))

        else:  # cemented
            front_idx, cement_idx, back_idx = unit['face_indices']
            R1 = pr_by_idx[front_idx][2]
            R3 = pr_by_idx[back_idx][2]
            t_total = sum(unit['lens_thicknesses'])

            c_front = _signed_contrib(R1, D, is_front=True)
            c_back = _signed_contrib(R3, D, is_front=False)

            if np.isnan(c_front) or np.isnan(c_back):
                issues.append(GeomIssue(
                    severity='FAIL', category='cement_edge',
                    location=unit['desc_summary'],
                    value=float('nan'), threshold=0.0,
                    hint='矢高无定义。先解决 R 过小问题。'
                ))
                continue

            t_edge = t_total - c_front - c_back

            if t_edge < THRESH_T_EDGE_FAIL:
                deficit = -t_edge
                recommended = c_front + c_back + SAFETY_MARGIN
                issues.append(GeomIssue(
                    severity='FAIL', category='cement_edge',
                    location=unit['desc_summary'],
                    value=t_edge, threshold=THRESH_T_EDGE_FAIL,
                    hint=f"胶合总厚 {t_total:.2f}mm 不够覆盖首尾矢高"
                         f"(前{c_front:+.2f}+后{c_back:+.2f})，"
                         f"缺{deficit:.2f}mm。建议总厚 ≥ {recommended:.2f}mm "
                         f"（修改 t_cemented_min）。"
                ))
            elif t_edge < THRESH_T_EDGE_WARN:
                issues.append(GeomIssue(
                    severity='WARN', category='cement_edge',
                    location=unit['desc_summary'],
                    value=t_edge, threshold=THRESH_T_EDGE_WARN,
                    hint=f"胶合对边缘厚 {t_edge:.2f}mm 偏薄。"
                ))


# ── 校验：相邻单元空气间隙边缘冲突 ──────────────────────
def _check_gap_edges(prescription: List[tuple],
                     lens_units: List[Dict],
                     air_gaps: List[Dict],
                     D: float,
                     issues: List[GeomIssue]):
    pr_by_idx = {p[0]: p for p in prescription}

    for u_idx, gap in enumerate(air_gaps):
        # 最后一个单元后是 BFD（到像面的距离），不是相邻片间距，跳过
        if u_idx >= len(lens_units) - 1:
            continue

        prev_unit = lens_units[u_idx]
        next_unit = lens_units[u_idx + 1]

        # 前一单元的"最后玻璃面"（其后表面 = 空气面，但矢高用最后玻璃面的 R）
        # 单片：face_indices[0]=前、face_indices[1]=后（空气面）→ 矢高用前表面 R 不对
        # 注意：对单片，face_indices[1] 是 nd=1.0 的空气面，但它的 R 也是该片后表面的 R！
        # 因为 SURFACE_PRESCRIPTION 中 (1, "G1-L1后", -790.914, 1.0, 1.0, None) 的 R 就是后表面曲率
        # 所以直接取 face_indices[-1] 的 R 即可
        prev_back_R = pr_by_idx[prev_unit['face_indices'][-1]][2]
        next_front_R = pr_by_idx[next_unit['face_indices'][0]][2]

        c_prev_back = _signed_contrib(prev_back_R, D, is_front=False)
        c_next_front = _signed_contrib(next_front_R, D, is_front=True)

        if np.isnan(c_prev_back) or np.isnan(c_next_front):
            continue  # R 问题已在 surface_R 检查中报过

        air_gap = gap['thickness']
        gap_edge = air_gap - c_prev_back - c_next_front

        loc = f"{prev_unit['desc_summary']} ↔ {next_unit['desc_summary']}"
        if gap_edge < THRESH_GAP_EDGE_FAIL:
            deficit = -gap_edge
            recommended = c_prev_back + c_next_front + SAFETY_MARGIN
            issues.append(GeomIssue(
                severity='FAIL', category='gap_edge',
                location=loc,
                value=gap_edge, threshold=THRESH_GAP_EDGE_FAIL,
                hint=f"气隙 {air_gap:.2f}mm 不够覆盖矢高凸出"
                     f"(前片后{c_prev_back:+.2f}+后片前{c_next_front:+.2f})，"
                     f"缺 {deficit:.2f}mm。建议气隙 ≥ {recommended:.2f}mm "
                     f"（face_idx={gap['face_idx']}）。"
            ))
        elif gap_edge < THRESH_GAP_EDGE_WARN:
            issues.append(GeomIssue(
                severity='WARN', category='gap_edge',
                location=loc,
                value=gap_edge, threshold=THRESH_GAP_EDGE_WARN,
                hint=f"边缘气隙 {gap_edge:.2f}mm 偏小，装配难度高。"
            ))


# ── 主入口 ──────────────────────────────────────────
def validate_geometry(
    surface_prescription: List[tuple],
    zoom_configs: List[Tuple],
    D_mm: float,
    variable_thickness_idx: List[int],
) -> Dict[str, GeomReport]:
    """
    对每个 zoom config 跑一次几何校验。

    参数
    ----
    surface_prescription : 见模块文档
    zoom_configs : List[(name, efl, d1, d2, d3, epd)]
    D_mm : 用于矢高计算的代表口径（建议取最大 EPD 或 1.2 倍）
    variable_thickness_idx : 变焦间距对应的 0-based 面索引列表
        默认配置下应为 [6, 13, 18]（对应 G1/G2/G3 后的空气间隔）

    返回
    ----
    {config_name: GeomReport}
    """
    parsed = _parse_prescription(surface_prescription)
    reports = {}

    for cfg in zoom_configs:
        cfg_name = cfg[0]
        report = GeomReport(config_name=cfg_name)

        # 应用本 config 的变焦间距覆盖
        cfg_parsed = _apply_zoom_overrides(parsed, cfg, variable_thickness_idx)

        # 1. 面级别（不随 config 变，但每个 config 都报一次便于定位）
        _check_surface_R(surface_prescription, D_mm, report.issues)

        # 2. 片/胶合对级别（不随 config 变）
        _check_lens_edges(surface_prescription, cfg_parsed['lens_units'],
                          D_mm, report.issues)

        # 3. 间隙级别（这一项随 config 变）
        _check_gap_edges(surface_prescription, cfg_parsed['lens_units'],
                         cfg_parsed['air_gaps'], D_mm, report.issues)

        reports[cfg_name] = report

    return reports


# ── 报告打印 ────────────────────────────────────────
def print_geometry_report(reports: Dict[str, GeomReport]):
    sep = "=" * 78
    print(f"\n{sep}")
    print(f" 几何可行性校验报告（{len(reports)} 个 config）")
    print(sep)

    n_total = len(reports)
    n_buildable = sum(1 for r in reports.values() if r.is_buildable)
    n_clean = sum(1 for r in reports.values() if r.is_ok)
    print(f" 汇总：{n_clean}/{n_total} 全部 OK，"
          f"{n_buildable}/{n_total} 物理可建（无 FAIL）")

    for cfg_name, report in reports.items():
        if report.is_ok:
            status = '✓ OK'
        elif report.is_buildable:
            status = f'⚠ {report.n_warn} WARN'
        else:
            status = f'✗ {report.n_fail} FAIL + {report.n_warn} WARN'
        print(f"\n  [{cfg_name}] {status}")

        if not report.issues:
            continue

        sorted_issues = sorted(report.issues,
                                key=lambda i: (0 if i.severity == 'FAIL' else 1))
        for issue in sorted_issues:
            mark = '✗' if issue.severity == 'FAIL' else '⚠'
            print(f"    {mark} [{issue.category}] {issue.location}: "
                  f"value={issue.value:+.3f} (阈值 {issue.threshold:+.2f})")
            print(f"      → {issue.hint}")

    print(f"\n{sep}\n")