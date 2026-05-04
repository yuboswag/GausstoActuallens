"""
Action_a 各组实际焦距诊断模块.

定位场景
--------
高斯解 EFL 与 Zemax 实测 EFL 偏差 ~30-50% 时, 用本模块在 group 候选选定后、
edge_geometry / Zemax 写入之前介入, 把每组薄透镜组合 EFL 算出来, 与 CSV
目标 + Zemax 实测三方对比, 定位问题在哪一段:

    候选 EFL ≈ 目标         → LDE 写入阶段有 bug (曲率/厚度被改了)
    候选 EFL ≈ Zemax 实测   → search/scoring 阶段有 bug (没收敛到目标)
    候选 EFL 与两边都不一致 → 还有第三种 bug, 需进一步排查

挂载点
------
main.py 中 ALL_SPACINGS_MM 构造之前, 即 group 候选已选定但 edge_geometry
还未运行的位置.

独立运行
--------
    python diagnose_group_efl.py    # 跑内置自检 + demo
"""

from __future__ import annotations
import numpy as np


# =========================================================================
# 核心: ABCD 矩阵法计算单组组合等效焦距
# =========================================================================

def compute_group_efl(surfaces: list[dict]) -> float:
    """
    用 ABCD 矩阵 (reduced ray) 计算一组透镜的组合等效焦距.

    Parameters
    ----------
    surfaces : list of dict
        按光线传播顺序排列, 每个面:
            'c'       : 面曲率 = 1/R, 单位 mm^-1 (平面 = 0).
            'n_after' : 该面之后介质的折射率 (玻璃面后 = n_d, 空气面后 = 1.0).
            't_after' : 该面到下一面的轴向距离, 单位 mm (最后一面填 0).
        入射介质默认空气 (n=1.0).

    Returns
    -------
    f : float
        组合等效焦距 (mm). 正值=会聚, 负值=发散, inf=无焦.
    """
    if not surfaces:
        raise ValueError("surfaces 列表为空")

    n_prev = 1.0
    M = np.eye(2)

    for i, s in enumerate(surfaces):
        # 折射矩阵 (reduced ray [y, n*u]):  [[1, 0], [-phi, 1]],  phi = (n2-n1)*c
        phi = (s['n_after'] - n_prev) * s['c']
        R = np.array([[1.0, 0.0], [-phi, 1.0]])
        M = R @ M
        # 平移矩阵 (除最后一面外都做):  [[1, t/n], [0, 1]]
        if i < len(surfaces) - 1:
            T = np.array([[1.0, s['t_after'] / s['n_after']], [0.0, 1.0]])
            M = T @ M
        n_prev = s['n_after']

    C = M[1, 0]
    if abs(C) < 1e-12:
        return float('inf')
    return -1.0 / C


# =========================================================================
# 三方对比诊断打印
# =========================================================================

def diagnose_group_efls(
    groups: list[list[dict]],
    f_targets: list[float],
    f_zemax: list[float] | None = None,
    group_names: tuple[str, ...] = ("G1", "G2", "G3", "G4"),
    flag_threshold_pct: float = 5.0,
) -> list[dict]:
    """
    多方对比每组 EFL:
      f_target            CSV header 给的目标焦距
      f_thin_zerogap      薄透镜 + 零间距 (用 surfaces 的 (n-1)(c1-c2) 求 Σφᵢ).
                          search.py 按构造保证此列 = f_target.
                          如果此列 ≠ f_target → 锅在 structure.py 曲率分配阶段.
      f_thick_realgap     厚透镜 + 真实间距 (full ABCD 全面追迹).
                          这是结构进入 Zemax 后的真正 EFL.
                          与 f_thin_zerogap 之差 = 间距+厚度合成效应造成的 drift.
      f_zemax             Zemax 实测 (Cardinal Points).
                          应该与 f_thick_realgap 一致.

    Parameters
    ----------
    groups : list of (list of surface dict)
        4 个组, 每组按光线传播顺序排列的面数据.
    f_targets : list of float
        4 个目标焦距.
    f_zemax : list of float, optional
        Zemax 实测焦距, 不传则只 3 列对比.
    flag_threshold_pct : float
        偏差超此值打 verdict 标记.
    """
    assert len(groups) == len(f_targets) == len(group_names)
    if f_zemax is not None:
        assert len(f_zemax) == len(groups)

    has_zmx = f_zemax is not None

    print("\n" + "=" * 100)
    print("  各组焦距诊断:  f_target  vs  薄透镜零间距 (search 应保证)  "
          "vs  厚透镜真间距 (struct 实际)  vs  Zemax 实测")
    print("=" * 100)
    if has_zmx:
        hdr = (f"  {'Group':<6}{'#surf':>6}{'f_target':>10}"
               f"{'f_thin0gap':>12}{'f_thick':>10}{'f_zemax':>10}"
               f"{'thin0/tgt%':>12}{'thick/tgt%':>12}{'verdict':>14}")
    else:
        hdr = (f"  {'Group':<6}{'#surf':>6}{'f_target':>10}"
               f"{'f_thin0gap':>12}{'f_thick':>10}"
               f"{'thin0/tgt%':>12}{'thick/tgt%':>12}")
    print(hdr)
    print("-" * 100)

    rows = []
    for i, (name, surfaces, f_t) in enumerate(zip(group_names, groups, f_targets)):
        # 厚透镜真间距: 直接 ABCD 全面追迹
        f_thick = compute_group_efl(surfaces)

        # 薄透镜零间距: 把所有 t_after 临时清零, 但保留 c 和 n_after
        thin_surfaces = [
            {'c': s['c'], 'n_after': s['n_after'], 't_after': 0.0}
            for s in surfaces
        ]
        f_thin0 = compute_group_efl(thin_surfaces)

        diff_thin0 = (f_thin0 - f_t) / f_t * 100 if f_t else float('nan')
        diff_thick = (f_thick - f_t) / f_t * 100 if f_t else float('nan')

        row = {
            'name': name, 'f_target': f_t,
            'f_thin_zerogap': f_thin0, 'f_thick_realgap': f_thick,
            'diff_thin0_vs_target_pct': diff_thin0,
            'diff_thick_vs_target_pct': diff_thick,
        }

        if has_zmx:
            f_z = f_zemax[i]
            row['f_zemax'] = f_z

            # verdict 判定
            thin0_ok = abs(diff_thin0) < flag_threshold_pct
            thick_ok = abs(diff_thick) < flag_threshold_pct
            if thin0_ok and thick_ok:
                verdict = "ALL OK"
            elif thin0_ok and not thick_ok:
                # 薄零间距对得上 → search 没问题
                # 厚真间距对不上 → drift 来自间距+厚度合成
                verdict = "GAP/THICK"
            elif not thin0_ok:
                # 薄零间距都对不上 → structure.py 曲率分配有问题
                verdict = "STRUCT BUG"
            else:
                verdict = "?"
            row['verdict'] = verdict

            print(f"  {name:<6}{len(surfaces):>6}{f_t:>10.3f}"
                  f"{f_thin0:>12.3f}{f_thick:>10.3f}{f_z:>10.3f}"
                  f"{diff_thin0:>11.2f}%{diff_thick:>11.2f}%{verdict:>14}")
        else:
            row['verdict'] = ("OK" if abs(diff_thick) < flag_threshold_pct
                              else "FLAG")
            print(f"  {name:<6}{len(surfaces):>6}{f_t:>10.3f}"
                  f"{f_thin0:>12.3f}{f_thick:>10.3f}"
                  f"{diff_thin0:>11.2f}%{diff_thick:>11.2f}%")

        rows.append(row)

    print("=" * 100)
    if has_zmx:
        verdicts = {r['verdict'] for r in rows}
        if verdicts == {"ALL OK"}:
            print("  ✓ 所有组 thin0gap 与 thick_realgap 都接近 f_target.")
        elif verdicts <= {"ALL OK", "GAP/THICK"}:
            print("  ⚠ 薄透镜零间距 EFL 接近 target (search 输出正确), 但厚透镜")
            print("    真间距 EFL drift > 5%. 锅在 structure.py — 曲率分配阶段")
            print("    没有补偿组内间距+玻璃厚度对组合 EFL 的影响.")
            print("    建议: structure.py 加一个 1D 补偿循环, 对所有曲率统一缩放")
            print("          直到 ABCD 全面追迹 EFL = f_target.")
        elif "STRUCT BUG" in verdicts:
            print("  ⚠ 至少一组连薄透镜零间距 EFL 都对不上 target.")
            print("    structure.py 的曲率分配本身就跑偏了 (没保留 search 的 φᵢ).")
        else:
            print("  ⚠ 混合状态, 见 verdict 列.")
    print("=" * 100 + "\n")

    return rows


# =========================================================================
# Adapter — 针对 Action_a 中 compute_initial_structure 的真实输出格式
# =========================================================================
#
# 数据契约 (与 main.py line 838-887 现有提取逻辑保持一致):
#   struct_result['surfaces']  : list of (desc, R_val, lens_idx, is_cem)
#   struct_result['thickness'] : list, 索引按透镜, [.. ][0] = 中心厚度
#   glass_names                : list[str], 索引按透镜
#   nd_values                  : dict {glass_name -> nd}
#   cemented_pairs             : list[(li_front, li_back)] 或空
#   spacings_mm                : list[float], 透镜 i 与 i+1 之间的空气间距
# =========================================================================

def extract_surfaces_from_struct_result(
    struct_result: dict,
    glass_names: list[str],
    nd_values: dict,
    cemented_pairs: list,
    spacings_mm: list[float],
) -> list[dict]:
    """
    从 Action_a 的 compute_initial_structure 输出 + 周边参数提取本组 surfaces.

    复用 main.py line 838-887 的"逐面确定后续介质和厚度"逻辑, 转换成本模块
    需要的 {'c', 'n_after', 't_after'} 格式.
    """
    _surfs = struct_result['surfaces']
    _thick = struct_result['thickness']

    surfaces = []
    for si, (desc, R_val, lens_idx, is_cem) in enumerate(_surfs):
        # 确定本面之后的介质 nd 和到下一面的距离 t  (照搬 main.py 逻辑)
        if is_cem:
            pair = next((p for p in cemented_pairs if p[0] == lens_idx), None)
            if pair:
                next_li = pair[1]
                glass = glass_names[next_li]
                nd = nd_values[glass]
                t = _thick[next_li][0]
            else:
                nd = 1.0
                t = 0.0
        elif si + 1 < len(_surfs):
            _, _, next_li, next_is_cem = _surfs[si + 1]
            if next_li == lens_idx or next_is_cem:
                # 前表面或胶合段中间, 后续仍是玻璃
                glass = glass_names[lens_idx]
                nd = nd_values[glass]
                t = _thick[lens_idx][0]
            else:
                # 后表面, 后续是空气间隔
                nd = 1.0
                t = spacings_mm[lens_idx] if lens_idx < len(spacings_mm) else 0.0
        else:
            # 组内最后一面
            nd = 1.0
            t = 0.0

        # 曲率: c = 1/R, 平面 (R=inf 或非有限) → c=0
        if not np.isfinite(R_val) or R_val == 0:
            c = 0.0
        else:
            c = 1.0 / R_val

        surfaces.append({'c': c, 'n_after': nd, 't_after': t})

    return surfaces


def diagnose_from_action_a_state(
    auto_struct_results: list[dict],
    auto_all_glass_names: list[list[str]],
    auto_all_nd_values: list[dict],
    all_cemented_pairs: list[list],
    all_spacings_mm: list[list[float]],
    f_targets: list[float],
    f_zemax: list[float] | None = None,
    group_names: tuple[str, ...] = ("G1", "G2", "G3", "G4"),
) -> list[dict]:
    """
    一键诊断: 从 main.py 那一组 ALL_* / auto_* 列表直接出诊断表.
    用于在 main.py line 654 后插入, 接入只需 1 个调用.
    """
    surfaces_per_group = []
    for gi in range(len(auto_struct_results)):
        if auto_struct_results[gi] is None:
            surfaces_per_group.append([])
            continue
        surfaces_per_group.append(extract_surfaces_from_struct_result(
            auto_struct_results[gi],
            auto_all_glass_names[gi],
            auto_all_nd_values[gi],
            all_cemented_pairs[gi],
            all_spacings_mm[gi],
        ))
    return diagnose_group_efls(
        surfaces_per_group, f_targets,
        f_zemax=f_zemax, group_names=group_names,
    )


# =========================================================================
# 自检 + demo
# =========================================================================

def _self_test():
    """单透镜 R1=50, R2=inf, n=1.5, t=2mm: 薄透镜公式 f=R1/(n-1)=100mm."""
    surfaces = [
        {'c': 1 / 50.0, 'n_after': 1.5, 't_after': 2.0},
        {'c': 0.0,      'n_after': 1.0, 't_after': 0.0},
    ]
    f = compute_group_efl(surfaces)
    expected = 100.0
    err_pct = abs(f - expected) / expected * 100
    print(f"[self_test] R1=50, R2=inf, n=1.5, t=2mm  →  expected ~{expected:.1f},"
          f" got {f:.3f},  err {err_pct:.3f}%")
    assert err_pct < 2.0, f"自检失败: 误差 {err_pct}% > 2%"
    print("[self_test] PASS")


def _adapter_test():
    """
    构造一个 mock struct_result, 验证 adapter 能正确把 Action_a 数据结构
    转成 surfaces, 并算出预期焦距.

    模拟一个 2 片非胶合组: R1=50/-100/无穷/-200, n=1.5/1.6, 中心厚 t=4/3, 间距 1.
    薄透镜公式各片: f1 = 1/((n1-1)(c1-c2)) = 1/(0.5*(0.02+0.01)) = 66.67
                    f2 = 1/((n2-1)(c1-c2)) = 1/(0.6*(0-(-0.005))) = 333.33
    组合 (薄透镜叠加): 1/f = 1/66.67 + 1/333.33 - d/(f1*f2)
                       d ≈ 1, → 1/f ≈ 0.015 + 0.003 - 0.000045 ≈ 0.0180
                       f ≈ 55.6 mm
    """
    # mock struct_result: 4 个面 (lens0 前/后, lens1 前/后)
    mock_struct = {
        'surfaces': [
            ('L1 front', 50.0,           0, False),
            ('L1 back', -100.0,          0, False),
            ('L2 front', float('inf'),   1, False),
            ('L2 back', -200.0,          1, False),
        ],
        'thickness': [(4.0,), (3.0,)],   # 透镜 0 中心厚 4, 透镜 1 中心厚 3
    }
    glass_names = ['MOCK15', 'MOCK16']
    nd_values   = {'MOCK15': 1.5, 'MOCK16': 1.6}
    cemented_pairs = []
    spacings_mm = [1.0]   # 透镜 0 与 1 之间空气间距 = 1

    surfaces = extract_surfaces_from_struct_result(
        mock_struct, glass_names, nd_values, cemented_pairs, spacings_mm)

    # 验证转换正确
    assert len(surfaces) == 4
    assert abs(surfaces[0]['c'] - 1/50.0) < 1e-9
    assert abs(surfaces[0]['n_after'] - 1.5) < 1e-9
    assert abs(surfaces[0]['t_after'] - 4.0) < 1e-9     # 玻璃中心厚
    assert abs(surfaces[1]['c'] - (-1/100.0)) < 1e-9
    assert abs(surfaces[1]['n_after'] - 1.0) < 1e-9     # 空气
    assert abs(surfaces[1]['t_after'] - 1.0) < 1e-9     # 间距
    assert surfaces[2]['c'] == 0.0                       # 平面
    assert abs(surfaces[3]['t_after']) < 1e-9            # 最后一面

    f = compute_group_efl(surfaces)
    print(f"[adapter_test] 2 片组 mock  →  f = {f:.3f} mm  (预期 ~55-56)")
    assert 50 < f < 60, f"adapter 转换或矩阵法异常, f={f}"
    print("[adapter_test] PASS")


if __name__ == "__main__":
    _self_test()
    _adapter_test()

    # demo: 用你的 CSV header f_targets + Zemax 截图实测值, 演示三方对比.
    print("\n[demo] 用 CSV 中的 f_targets + Zemax 实测 + 假想候选 演示输出格式:\n")

    f_targets = [60.074, -13.625, 31.123, 58.333]   # CSV header
    f_zmx     = [74.011, -17.999, 25.963, 42.295]   # 你的 Zemax 截图

    # 用一组随便构造的"假候选" 仅为演示打印格式.
    # 真实使用时这 4 个 list 由 adapt_action_a_groups_to_surfaces() 生成.
    G1 = [{'c': 1/40.0,  'n_after': 1.6, 't_after': 4.0},
          {'c': -1/300., 'n_after': 1.0, 't_after': 0.0}]
    G2 = [{'c': -1/30.,  'n_after': 1.7, 't_after': 1.5},
          {'c': 1/15.0,  'n_after': 1.0, 't_after': 0.0}]
    G3 = [{'c': 1/20.0,  'n_after': 1.55, 't_after': 3.5},
          {'c': -1/40.,  'n_after': 1.0,  't_after': 0.0}]
    G4 = [{'c': 1/35.0,  'n_after': 1.5,  't_after': 3.0},
          {'c': -1/120., 'n_after': 1.0,  't_after': 0.0}]

    diagnose_group_efls([G1, G2, G3, G4], f_targets, f_zemax=f_zmx)
