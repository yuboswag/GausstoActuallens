"""
validation.py
初始结构验证（步骤 2 → 步骤 3 入场检查）：
  V1 面形加工性（sag/R）
  V2 近轴 EFL 自洽性（厚透镜矩阵追迹）
  V3 初级色差残差（Σφ/V_gen）
  V4 Petzval 场曲评估
"""

import numpy as np

from structure import compute_sag

# ============================================================
# 第十部分：初始结构验证
# ============================================================
# 对 compute_initial_structure 输出的 R、d、n 进行入场资格验证，
# 确认在录入 Zemax 前满足以下四项指标：
#
#   V1  面形加工性    每面 sag/R ≤ sag_r_limit（默认 0.30）
#                     sag = R - √(R² - (D/2)²)，衡量加工和检验难度。
#
#   V2  近轴 EFL 自洽  用厚透镜传递矩阵 [y, nу] 追迹全系统，
#                      验证实际 EFL 与薄透镜理论目标的偏差 ≤ efl_tol（默认 2%）。
#                      偏差来源：薄透镜近似忽略了主面因厚度而产生的位移。
#
#   V3  初级色差残差   计算 Σφᵢ/V_gen,ᵢ，对应一级轴向色差量级。
#                      需提供 S_VGEN_LIST（从 action_a 输出表格 V_gen 列复制）。
#
#   V4  Petzval 场曲  计算 P = Σφᵢ/nᵢ，评估场曲先天条件。
#                     |P| 过大时场曲将主导 Zemax 优化，难以同时兼顾球差/彗差。
# ============================================================


def _build_lens_sequence(glass_names, nd_vals, cemented_pairs,
                          surfaces, thickness, spacings_mm):
    """
    从 compute_initial_structure 返回值重建厚透镜矩阵追迹所需的面序列。

    返回 list[dict]，按光路顺序每项对应一个折射面：
        'R'      : float，曲率半径（inf 表示平面）
        'n_in'   : float，入射侧折射率
        'n_out'  : float，出射侧折射率
        't_after': float，本面之后介质厚度 (mm)；最后一面为 0
        'desc'   : str，面描述（用于错误追踪）
    """
    N = len(glass_names)
    nd = [nd_vals[g] for g in glass_names]
    cem_first_map = {ci: cj for ci, cj in cemented_pairs}

    seq = []
    processed = set()

    for i in range(N):
        if i in processed:
            continue

        if i in cem_first_map:
            # ── 胶合双片 ──────────────────────────────────────────────
            cj    = cem_first_map[i]
            n_ci  = nd[i]
            n_cj  = nd[cj]
            t_ci  = thickness[i][0]
            t_cj  = thickness[cj][0]

            # surfaces 中对应三面：前表面(lens_idx=i, not cem)、
            # 胶合面(lens_idx=i, is_cem)、后表面(lens_idx=cj, not cem)
            R_front = next(s[1] for s in surfaces if s[2] == i  and not s[3])
            R_cem   = next(s[1] for s in surfaces if s[2] == i  and     s[3])
            R_back  = next(s[1] for s in surfaces if s[2] == cj and not s[3])
            # 胶合组之后的空气间隔索引为 cj（即 spacings_mm[cj]）
            spacing_after = spacings_mm[cj] if cj < len(spacings_mm) else 0.0

            seq.append({'R': R_front, 'n_in': 1.0,  'n_out': n_ci,
                        't_after': t_ci,          'desc': f'片{i+1}({glass_names[i]}) 前表面'})
            seq.append({'R': R_cem,   'n_in': n_ci, 'n_out': n_cj,
                        't_after': t_cj,          'desc': f'片{i+1}/{cj+1} 胶合面'})
            seq.append({'R': R_back,  'n_in': n_cj, 'n_out': 1.0,
                        't_after': spacing_after, 'desc': f'片{cj+1}({glass_names[cj]}) 后表面'})
            processed.add(i)
            processed.add(cj)

        else:
            # ── 独立单片 ──────────────────────────────────────────────
            n_i   = nd[i]
            t_i   = thickness[i][0]
            # 取该片的两个非胶合面（前后顺序由 compute_initial_structure 的
            # surfaces 构建顺序保证：前表面先 append，后表面后 append）
            surfs_i = [s for s in surfaces if s[2] == i and not s[3]]
            R_front = surfs_i[0][1]
            R_back  = surfs_i[1][1] if len(surfs_i) >= 2 else float('inf')
            spacing_after = spacings_mm[i] if i < len(spacings_mm) else 0.0

            seq.append({'R': R_front, 'n_in': 1.0, 'n_out': n_i,
                        't_after': t_i,           'desc': f'片{i+1}({glass_names[i]}) 前表面'})
            seq.append({'R': R_back,  'n_in': n_i, 'n_out': 1.0,
                        't_after': spacing_after, 'desc': f'片{i+1}({glass_names[i]}) 后表面'})
            processed.add(i)

    return seq


def build_seq_with_dispersion(
    glass_names,
    nd_values,
    cemented_pairs,
    surfaces,
    thickness,
    spacings_mm,
    vgen_list,
):
    """
    构建面序列并注入 name-keyed 色散量（封装重复的 3 步流程）。

    返回已注入色散的面序列 list[dict]，可直接用于 trace_paraxial / analyze_one_position。

    函数内部延迟导入 seidel_gemini，避免 validation ↔ seidel_gemini 循环依赖风险。
    """
    from seidel_gemini import add_dispersion_to_seq, dn_from_vd

    seq = _build_lens_sequence(
        glass_names    = glass_names,
        nd_vals        = nd_values,
        cemented_pairs = cemented_pairs,
        surfaces       = surfaces,
        thickness      = thickness,
        spacings_mm    = spacings_mm,
    )
    glass_dn_by_name = {
        g: dn_from_vd(nd_values[g], v)
        for g, v in zip(glass_names, vgen_list)
        if v is not None and v > 0
    }
    add_dispersion_to_seq(seq, glass_dn_by_name, key='name')
    return seq


def _compute_thick_efl(seq):
    """
    用 [y, nу] 传递矩阵法计算厚透镜系统等效焦距（EFL）。

    状态向量 v = [y, nu]，其中 nu = n·u（光学方向余弦）。

    折射矩阵（曲率 c = 1/R，n1→n2）：
        M_R = [[1,            0],
               [-(n2-n1)·c,  1]]

    传播矩阵（介质 n2，厚度 t）：
        M_T = [[1,  t/n2],
               [0,  1   ]]

    系统矩阵 M = M_last × … × M_T × M_R（依次左乘）
    EFL = −1/C，其中 C = M[1,0]，系统两侧均为空气（n=1）。

    返回 EFL (mm)；无焦系统返回 None。
    """
    M = np.eye(2)
    for surf in seq:
        R, n1, n2, t = surf['R'], surf['n_in'], surf['n_out'], surf['t_after']
        c = 1.0 / R if abs(R) > 1e-10 else 0.0
        # 折射
        M = np.array([[1.0,              0.0],
                       [-(n2 - n1) * c,  1.0]]) @ M
        # 传播
        if abs(t) > 1e-12:
            M = np.array([[1.0,  t / n2],
                           [0.0,  1.0  ]]) @ M

    C = M[1, 0]
    return (-1.0 / C) if abs(C) > 1e-12 else None




def validate_initial_structure(
        result,
        glass_names,
        nd_values,
        focal_lengths_mm,
        cemented_pairs,
        spacings_mm,
        D_mm,
        target_f_mm,
        Vgen_list     = None,
        efl_tol       = 0.02,
        sag_r_warn    = 0.20,
        sag_r_limit   = 0.30,
        petzval_limit = 0.05):
    """
    对 compute_initial_structure 的输出进行入场资格验证（V1~V4）。

    参数
    ----
    result           : compute_initial_structure 的返回值
    glass_names      : list[str]，各片玻璃牌号
    nd_values        : dict {牌号: nd}
    focal_lengths_mm : list[float]，各片目标焦距
    cemented_pairs   : list[tuple]，胶合对
    spacings_mm      : list[float]，相邻片间距（mm）
    D_mm             : float，通光口径（mm）
    target_f_mm      : float，组元目标焦距 f_group（mm）
    Vgen_list        : list[float] 或 None；None 则跳过 V3
    efl_tol          : float，EFL 容差（分数，0.02 = ±2%）
    sag_r_warn       : float，sag/R 警告阈值（默认 0.20）
    sag_r_limit      : float，sag/R 不通过阈值（默认 0.30）
    petzval_limit    : float，|Σφ/n| 不通过阈值，mm⁻¹（默认 0.05）

    返回
    ----
    (all_passed, warnings, failures) : (bool, list[str], list[str])
        all_passed = True 表示无不通过项（可能仍有警告）
    """
    surfaces    = result['surfaces']
    thickness   = result['thickness']
    clamp_notes = result.get('clamp_notes', {})   # {面描述: 截断说明}
    N           = len(glass_names)
    phi_list    = [1.0 / f for f in focal_lengths_mm]
    nd_list     = [nd_values[g] for g in glass_names]
    sep         = '=' * 72

    warn_list = []
    fail_list = []

    print(f"\n{sep}")
    print(f"  初始结构验证报告（步骤 2 → 步骤 3 入场检查）")
    print(f"{sep}")

    # ── V1：面形加工性（sag/R）────────────────────────────────────
    print(f"\n【V1】面形加工性  "
          f"sag = R−√(R²−(D/2)²)，警告 ≥ {sag_r_warn:.2f}，不通过 ≥ {sag_r_limit:.2f}")
    if clamp_notes:
        print(f"  ⓘ  带 ↳ 标记的面已被 S_MIN_R_MM 约束截断，"
              f"R 非 Seidel 最优值（见各面备注）。")
    print(f"  {'面序':^5}  {'描述':^42}  {'R (mm)':>10}  "
          f"{'sag (mm)':>9}  {'sag/R':>7}  状态")
    print("  " + "-" * 82)

    for surf_idx, (desc, R, lens_idx, is_cem) in enumerate(surfaces, 1):
        if abs(R) > 1e5:
            print(f"  面{surf_idx:<3}  {desc:42}  {'∞':>10}  "
                  f"{'---':>9}  {'---':>7}  ✓ 平面")
            continue

        sag = compute_sag(R, D_mm)

        if np.isnan(sag):
            mark = '✗ |R| < D/2，无法加工'
            fail_list.append(
                f'[V1] 面{surf_idx}（{desc}）：|R|={abs(R):.2f}mm < D/2={D_mm/2:.1f}mm，'
                f'球面无法覆盖完整通光口径。→ 提高 S_MIN_R_MM 或换焦距更长的玻璃重新穷举。'
            )
            print(f"  面{surf_idx:<3}  {desc:42}  {R:>+10.2f}  "
                  f"{'---':>9}  {'---':>7}  {mark}")
            if desc in clamp_notes:
                print(f"  {'':5}  ↳ {clamp_notes[desc]}")
            continue

        sag_r = sag / abs(R)

        if sag_r >= sag_r_limit:
            mark = f'✗ {sag_r:.4f} ≥ {sag_r_limit:.2f}'
            fail_list.append(
                f'[V1] 面{surf_idx}（{desc}）：sag/R = {sag_r:.4f} ≥ {sag_r_limit:.2f}。'
                f'→ 提高 S_MIN_R_MM 或增大 min_f_mm 重新穷举，选焦距更长的玻璃。'
            )
        elif sag_r >= sag_r_warn:
            mark = f'△ {sag_r:.4f} ≥ {sag_r_warn:.2f}'
            warn_list.append(
                f'[V1] 面{surf_idx}（{desc}）：sag/R = {sag_r:.4f}，偏强。'
                f'建议向加工厂确认该面检验能力。'
            )
        else:
            mark = f'✓ {sag_r:.4f}'

        print(f"  面{surf_idx:<3}  {desc:42}  {R:>+10.2f}  "
              f"{sag:>9.4f}  {sag_r:>7.4f}  {mark}")
        # 若该面曾被 c_max 截断，在下一行打印说明
        if desc in clamp_notes:
            print(f"  {'':5}  ↳ {clamp_notes[desc]}")

    # ── V2：近轴 EFL 自洽性（厚透镜矩阵追迹）────────────────────
    print(f"\n【V2】近轴 EFL 自洽性  "
          f"目标 {target_f_mm:+.3f} mm，容差 ±{efl_tol * 100:.0f}%")

    try:
        seq       = _build_lens_sequence(
            glass_names, nd_values, cemented_pairs,
            surfaces, thickness, spacings_mm
        )
        thick_efl = _compute_thick_efl(seq)

        if thick_efl is None:
            print(f"  ⚠ 系统矩阵 C ≈ 0，接近无焦状态，EFL 无意义。")
            warn_list.append(
                '[V2] 厚透镜矩阵 C = 0，系统接近无焦，EFL 验证跳过。请检查焦距配置。'
            )
        else:
            err_frac = (thick_efl - target_f_mm) / abs(target_f_mm)
            err_pct  = err_frac * 100
            abs_err  = thick_efl - target_f_mm

            print(f"  薄透镜理论 EFL  = {target_f_mm:+.4f} mm  （S_FOCAL_LENGTHS_MM 薄透镜假设）")
            print(f"  厚透镜追迹 EFL  = {thick_efl:+.4f} mm  （含中心厚度 + 间距的矩阵追迹）")
            print(f"  绝对偏差        = {abs_err:+.4f} mm")
            print(f"  相对偏差        = {err_pct:+.3f}%")

            if abs(err_frac) > efl_tol:
                mark = f'✗ 超过 ±{efl_tol * 100:.0f}% 容差'
                fail_list.append(
                    f'[V2] 厚透镜 EFL 偏差 {err_pct:+.2f}%，超过 ±{efl_tol * 100:.0f}% 阈值。'
                    f'薄-厚透镜差异过大。→ 录入 Zemax 后请先将所有间距设为变量，'
                    f'以 EFL 为约束条件优化，收敛后再开始像差校正。'
                )
            elif abs(err_frac) > efl_tol / 2:
                mark = f'△ 偏差 {err_pct:+.2f}%（超半容差）'
                warn_list.append(
                    f'[V2] EFL 偏差 {err_pct:+.2f}%，存在薄-厚透镜近似误差。'
                    f'建议在 Zemax 中先微调间距使 EFL 归位。'
                )
            else:
                mark = f'✓ 偏差 {err_pct:+.2f}%'

            print(f"  评估：{mark}")
            print(f"  说明：偏差源于薄透镜近似——忽略了各片厚度造成的主面位移。")
            print(f"        录入 Zemax 后建议将所有间距设为变量，先约束 EFL，再优化像差。")

    except Exception as e:
        print(f"  ⚠ 矩阵追迹异常：{e}")
        warn_list.append(f'[V2] 矩阵追迹异常（{e}），请手动核查面序列数据。')

    # ── V3：初级色差残差（需 Vgen_list）──────────────────────────
    print(f"\n【V3】初级色差残差  Σφᵢ / V_gen,ᵢ"
          f"{'（跳过）' if Vgen_list is None else ''}")

    if Vgen_list is None:
        print(f"  跳过：S_VGEN_LIST 未提供。")
        print(f"  如需验证，请将第一步输出表格中的 V_gen 列复制到 S_VGEN_LIST。")
    elif len(Vgen_list) != N:
        print(f"  ⚠ S_VGEN_LIST 长度（{len(Vgen_list)}）与片数（{N}）不符，跳过。")
        warn_list.append(f'[V3] S_VGEN_LIST 长度与片数不符，色差残差验证跳过。')
    else:
        print(f"  {'片':^4}  {'玻璃':^12}  {'φ (mm⁻¹)':>12}  "
              f"{'V_gen':>8}  {'φ/V_gen':>14}")
        print("  " + "-" * 58)

        valid_pairs = []
        for i, (g, phi, V) in enumerate(zip(glass_names, phi_list, Vgen_list)):
            if V is None or abs(V) < 1e-6:
                v_str  = 'N/A'
                pv_str = 'N/A'
            else:
                v_str  = f'{V:.3f}'
                pv_str = f'{phi / V:+.4e}'
                valid_pairs.append((phi, V))
            print(f"  片{i+1:<2}  {g:12}  {phi:>+12.6f}  {v_str:>8}  {pv_str:>14}")

        if valid_pairs:
            sum_pv   = sum(phi / V for phi, V in valid_pairs)
            err_disp = abs(sum_pv)
            # 等效一阶焦移量 = f × |Σφ/V|（表示 λ_short 与 λ_long 的焦面间距估计）
            delta_f  = abs(target_f_mm) * err_disp

            print(f"\n  Σφᵢ/V_gen,ᵢ  = {sum_pv:+.4e} mm⁻¹")
            print(f"  |残差|         = {err_disp:.4e} mm⁻¹")
            print(f"  等效焦移量 Δf  = {delta_f:.4f} mm"
                  f"  ≈ {delta_f / abs(target_f_mm) * 100:.4f}% × f_group")

            if err_disp < 1e-4:
                mark = '✓ 优秀，色差控制良好'
            elif err_disp < 1e-3:
                mark = '✓ 合格'
            elif err_disp < 5e-3:
                mark = f'△ {err_disp:.2e}，偏大（建议 < 1e-3）'
                warn_list.append(
                    f'[V3] Σφ/V = {err_disp:.2e}，色差残差偏大。'
                    f'→ 考虑降低 TOL_DISP（当前建议 ≤ 1e-3）后重新穷举玻璃。'
                )
            else:
                mark = f'✗ {err_disp:.2e}，色差校正严重不足'
                fail_list.append(
                    f'[V3] Σφ/V = {err_disp:.2e} >> 1e-3，色差校正严重不足。'
                    f'→ 降低 TOL_DISP 并重新穷举玻璃组合，或检查 V_gen 数值是否正确填写。'
                )
            print(f"  评估：{mark}")

    # ── V4：Petzval 场曲评估 ─────────────────────────────────────
    print(f"\n【V4】Petzval 场曲  P = Σφᵢ/nᵢ"
          f"（不通过阈值 |P| > {petzval_limit:.3f} mm⁻¹）")

    P_ptz = sum(phi / n for phi, n in zip(phi_list, nd_list))
    R_ptz = -1.0 / P_ptz if abs(P_ptz) > 1e-12 else float('inf')

    # [Fix-Bug5] 将列标题 "n_d" 改为 "n(λref)"，准确反映 nd_values 在
    # auto/structure 模式下存放的是工作参考波长折射率 n_ref 而非目录 nd
    print(f"  {'片':^4}  {'玻璃':^12}  {'φ (mm⁻¹)':>12}  "
          f"{'n(λref)':>8}  {'φ/n':>12}")
    print(f"  （n 取自 nd_values，auto/structure 模式下为工作参考波长折射率 n_ref）")
    print("  " + "-" * 54)
    for i, (g, phi, n) in enumerate(zip(glass_names, phi_list, nd_list)):
        print(f"  片{i+1:<2}  {g:12}  {phi:>+12.6f}  "
              f"{n:>8.5f}  {phi / n:>+12.6f}")

    print(f"\n  Petzval 和 P   = {P_ptz:+.6f} mm⁻¹")
    if abs(R_ptz) > 1e5:
        print(f"  Petzval 半径   = ∞ mm （理想平场）")
    else:
        print(f"  Petzval 半径   = {R_ptz:+.2f} mm")
        # 参考量：|R_ptz| 与口径的比值，越大场曲越平
        print(f"  |R_ptz| / D    = {abs(R_ptz) / D_mm:.1f}×  "
              f"（通常希望 > 5~10×，具体取决于像面尺寸）")

    if abs(P_ptz) > petzval_limit:
        mark = f'✗ |P| = {abs(P_ptz):.5f} > {petzval_limit:.3f}'
        fail_list.append(
            f'[V4] Petzval 和过大（|P| = {abs(P_ptz):.5f} mm⁻¹ > {petzval_limit}），'
            f'R_ptz = {R_ptz:+.1f} mm。'
            f'→ 步骤 3/4 中场曲项将主导优化，建议检查正负组折射率分配，'
            f'或在 optimizer.py 的 _penalty_petzval 中收紧约束后重新搜索高斯解。'
        )
    elif abs(P_ptz) > petzval_limit * 0.70:
        mark = f'△ |P| = {abs(P_ptz):.5f}，接近阈值'
        warn_list.append(
            f'[V4] Petzval 和 {abs(P_ptz):.5f} mm⁻¹ 接近上限，'
            f'R_ptz = {R_ptz:+.1f} mm，进入 Zemax 后需重点关注场曲控制。'
        )
    else:
        mark = f'✓ |P| = {abs(P_ptz):.5f} mm⁻¹'
    print(f"  评估：{mark}")

    # ── 汇总 ─────────────────────────────────────────────────────
    all_passed = len(fail_list) == 0

    print(f"\n{sep}")
    print(f"  验证汇总")
    print(f"{sep}")

    if not fail_list and not warn_list:
        print(f"\n  ✅ 全部通过，无警告。")
        print(f"  → 可将上方 Zemax 汇总表数据录入 Zemax，进行赛德尔系数诊断（步骤 3）。")
    else:
        if fail_list:
            print(f"\n  ❌ 不通过项（共 {len(fail_list)} 项）"
                  f"——建议返回步骤 2 修正后再录入 Zemax：")
            for k, msg in enumerate(fail_list, 1):
                print(f"    {k}. {msg}")

        if warn_list:
            nl = '\n' if fail_list else ''
            print(f"{nl}  ⚠ 警告项（共 {len(warn_list)} 项）"
                  f"——可录入 Zemax，但需在优化中重点关注：")
            for k, msg in enumerate(warn_list, 1):
                print(f"    {k}. {msg}")

        if all_passed:
            print(f"\n  ✅ 无不通过项，可录入 Zemax。")

    print(f"\n{sep}")
    return all_passed, warn_list, fail_list


# ============================================================
# 第八部分：主程序入口
# ============================================================

