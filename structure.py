"""
structure.py
初始结构计算：由玻璃材料和焦距分配出发，用 Seidel 初级像差理论
计算共轭因子 p、形状因子 q、曲率半径 R 和中心厚度 t，
输出可直接录入 Zemax 的初始结构数据表。
"""

import numpy as np
from pathlib import Path
from scipy.optimize import brentq, minimize_scalar, minimize

from zoom_utils import compute_pbar_from_zoom_data, load_zoom_ray_csv


def compute_principal_planes(surfaces_data, thicknesses_data, nd_values_dict,
                             glass_names_list, spacings_mm_list, cemented_pairs_list):
    """
    用 ABCD 矩阵法计算一个镜组的前后主面位置。

    参数：
    - surfaces_data: compute_initial_structure 返回的 surfaces 列表
      格式 [(描述str, R值float, 片索引int, 是否胶合面bool), ...]
    - thicknesses_data: compute_initial_structure 返回的 thickness 字典
      格式 {片索引: (中心厚度float, 说明str)}
    - nd_values_dict: {玻璃名称: nd折射率} 字典
    - glass_names_list: 玻璃名称列表 ['H-LaF50B', 'ZF6', ...]
    - spacings_mm_list: 片间空气间距列表
    - cemented_pairs_list: 胶合对列表 [(1,2), ...]

    返回：
    (delta_H, delta_Hp)
    - delta_H:  前主面 H 到组第一面的距离（mm），正值表示 H 在第一面右侧（组内部）
    - delta_Hp: 后主面 H' 到组最后面的距离（mm），负值表示 H' 在最后面左侧（组内部）
    """
    # 提取片数
    N_lens = len(glass_names_list)

    # 构建胶合集合，便于快速查询
    cemented_set = set()
    for ci, cj in cemented_pairs_list:
        cemented_set.add(ci)
        cemented_set.add(cj)

    # ── 步骤1：逐段构建 (R, t, n) 序列 ─────────────────────────────
    # 每个折射面后紧跟一个传输段（最后一面的传输段是最后一片的出射到像面，不计入主面计算）
    # 我们需要的顺序是：
    #   R1 (n0=1→n1) → T1 (n1, t1) → R2 (n1→n2) → T2 (n2, t2) → ... → Rk

    R_list = []        # 各折射面曲率半径列表，顺序同 surfaces_data
    n_after_list = []  # 各折射面之后的介质折射率（用于该段传输段的介质）
    t_segment_list = []  # 各传输段厚度（折射面之间），长度 = len(R_list) - 1

    # 第一步：确定每个折射面 *之后* 的介质
    for idx, (desc, R_val, lens_idx, is_cem) in enumerate(surfaces_data):
        R_list.append(R_val)
        if is_cem:
            # 胶合面：之后是后片玻璃
            pair = next((p for p in cemented_pairs_list if p[0] == lens_idx), None)
            if pair:
                _, next_idx = pair
                n_after = nd_values_dict[glass_names_list[next_idx]]
            else:
                n_after = 1.0
        else:
            # 非胶合面：查看下一个面
            if idx + 1 < len(surfaces_data):
                _, _, next_lens_idx, next_is_cem = surfaces_data[idx + 1]
                if next_lens_idx == lens_idx:
                    # 同片的下一个面：本片玻璃
                    n_after = nd_values_dict[glass_names_list[lens_idx]]
                else:
                    # 不同片：空气间隔
                    n_after = 1.0
            else:
                # 最后一个面：出射到空气
                n_after = 1.0
        n_after_list.append(n_after)

    # 第二步：由 n_after 推导 n_before（前一面的 after 即为该面的 before）
    n_before_list = []
    for idx in range(len(R_list)):
        if idx == 0:
            n_before_list.append(1.0)
        else:
            n_before_list.append(n_after_list[idx - 1])

    # 第三步：确定各传输段的厚度
    for idx in range(len(R_list) - 1):
        _, _, lens_idx, is_cem = surfaces_data[idx]
        _, _, next_lens_idx, _ = surfaces_data[idx + 1]

        if is_cem:
            # 胶合面之后是后片玻璃，厚度为后片中心厚
            pair = next((p for p in cemented_pairs_list if p[0] == lens_idx), None)
            if pair:
                _, back_idx = pair
                t = thicknesses_data[back_idx][0]
            else:
                t = 0.0
        elif next_lens_idx == lens_idx:
            # 同片内部：本片中心厚度
            t = thicknesses_data[lens_idx][0]
        else:
            # 片间空气间隔
            if lens_idx < len(spacings_mm_list):
                t = spacings_mm_list[lens_idx]
            else:
                t = 0.0
        t_segment_list.append(t)

    # ── 步骤2：构建系统矩阵 M ─────────────────────────────────────
    # M = R_k * T_{k-1} * ... * R_2 * T_1 * R_1
    # 每次右乘：M ← T @ M（先从最右边开始累积）

    M = np.eye(2)  # [[1, 0], [0, 1]]

    for i in range(len(R_list)):
        R_i = R_list[i]
        n_before = n_before_list[i]
        n_after = n_after_list[i]

        # 折射矩阵 R_surf：n_before → n_after
        # 由 Snell 定律和光线追迹推导：n_before*u - n_after*u' = (n_after-n_before)*h/R
        # 整理得：n_after*u' = n_before*u - (n_after-n_before)*h/R
        # 在 [h, n*u]^T 空间中，转移矩阵第二行第一列 = -(n_after - n_before)/R = -phi_surf
        if abs(R_i) > 1e12:  # 平面
            R_mat = np.eye(2)
        else:
            phi_surf = (n_after - n_before) / R_i
            R_mat = np.array([[1.0, 0.0], [-phi_surf, 1.0]])

        M = R_mat @ M

        # 如果不是最后一面，添加传输矩阵
        if i < len(R_list) - 1:
            t = t_segment_list[i]
            n = n_after  # 传输介质的折射率即该面之后的折射率
            T_mat = np.array([[1.0, t / n], [0.0, 1.0]])
            M = T_mat @ M

    A, B = M[0, 0], M[0, 1]
    C, D = M[1, 0], M[1, 1]

    # ── 步骤3：提取主面位置 ───────────────────────────────────────
    if abs(C) < 1e-12:
        print("  ⚠ 无焦系统（|C| ≈ 0），无法定义主面位置")
        return 0.0, 0.0

    Phi_sys = -C  # 系统光焦度 (mm^-1)
    delta_H = (D - 1.0) / C      # 前主面到第一面的距离（mm）
    delta_Hp = (1.0 - A) / C     # 后主面到最后面的距离（mm，正值=向右=组外）

    # ── 步骤4：打印诊断信息 ───────────────────────────────────────
    efl = -1.0 / C if abs(C) > 1e-12 else float('inf')
    print(f"  主面位置: delta_H = {delta_H:+.4f} mm (从第一面), "
          f"delta_H' = {delta_Hp:+.4f} mm (从最后面)")
    print(f"  系统光焦度 Phi = {Phi_sys:.6f} mm^-1, EFL = {efl:.3f} mm")

    return delta_H, delta_Hp


def compute_sag(R, D):
    """
    计算曲率半径为 R、口径为 D 的球面矢高。
    s = R - sqrt(R² - (D/2)²)
    平面（R=inf）时返回 0。
    """
    if abs(R) > 1e6:
        return 0.0
    half_D = D / 2.0
    discriminant = R**2 - half_D**2
    if discriminant < 0:
        # R < D/2，物理上无法加工出完整口径的球面
        return float('nan')
    return abs(R) - np.sqrt(discriminant)


def compute_thickness(R1, R2, D, is_positive_lens,
                      t_edge_min, t_center_min):
    """
    计算单片透镜的中心厚度。

    正透镜（中心厚、边缘薄）：
        t_center = sag(R1) + sag(R2) + t_edge_min
        其中 sag(R) 是该面在口径边缘处的矢高贡献。

    负透镜（中心薄、边缘厚）：
        t_center = t_center_min（直接取最小中心厚度）

    参数
    ----
    R1, R2          : 前后表面曲率半径（mm），符号按光学惯例
    D               : 口径（mm）
    is_positive_lens: True 为正透镜，False 为负透镜
    t_edge_min      : 最小边缘厚度（mm）
    t_center_min    : 最小中心厚度（mm）

    返回
    ----
    (t_center, note) : 中心厚度（mm）和说明字符串
    """
    if is_positive_lens:
        s1 = compute_sag(R1, D)
        s2 = compute_sag(R2, D)
        if np.isnan(s1) or np.isnan(s2):
            return t_center_min, '⚠ 矢高计算异常（R<D/2），取最小值'
        # 符号规则：前表面 R>0 凸（正贡献），后表面 R<0 凸（正贡献）；弯月镜一面为负贡献
        contrib1 = s1 if R1 > 0 else -s1
        contrib2 = s2 if R2 < 0 else -s2
        t = max(contrib1 + contrib2 + t_edge_min, t_center_min)
        note = f'矢高符号贡献({contrib1:.2f}+{contrib2:.2f}) + 最小边缘厚({t_edge_min:.1f}mm) = {t:.2f}mm'
    else:
        # 负透镜：中心最薄，直接取最小中心厚度
        t = t_center_min
        note = f'负透镜，取最小中心厚度 {t_center_min:.1f}mm'

    return round(t, 2), note


def compute_cemented_thickness(R1, R_cem, R3, D, phi2, phi3,
                               t_cemented_min, t_edge_min):
    """
    计算胶合双片的总中心厚度，并合理分配给两片。

    胶合组通常作为一个整体考虑：
        - 整体中心厚度 = 前片厚度 + 后片厚度
        - 胶合面不是空气间隔，两片紧贴
        - 整体是否为正/负取决于合焦距

    参数
    ----
    R1, R_cem, R3 : 三个面的曲率半径
    D             : 口径
    phi2, phi3    : 两片各自的光焦度
    t_cemented_min: 整体最小中心厚度
    t_edge_min    : 最小边缘厚度

    返回
    ----
    (t2, t3, note) : 前片厚度、后片厚度、说明
    """
    phi_total = phi2 + phi3
    is_positive_total = phi_total >= 0

    s1 = compute_sag(R1, D)
    s_cem = compute_sag(R_cem, D)
    s3 = compute_sag(R3, D)

    if any(np.isnan(s) for s in [s1, s_cem, s3]):
        t_total = t_cemented_min
        note = '⚠ 矢高计算异常，取最小总厚'
    elif is_positive_total:
        # [Fix-Bug2] 胶合面两侧方向相反，净贡献为 0，只计首尾两面的有符号贡献：
        # 前表面 R1>0（凸）→ 正贡献；R1<0（凹）→ 负贡献
        # 后表面 R3<0（凸）→ 正贡献；R3>0（凹）→ 负贡献
        contrib1 = s1 if R1 > 0 else -s1
        contrib3 = s3 if R3 < 0 else -s3
        t_total = max(contrib1 + contrib3 + t_edge_min, t_cemented_min)
        note = (f'整体正光焦度：首尾面矢高贡献({contrib1:.2f}+{contrib3:.2f})'
                f' + 最小边缘厚({t_edge_min:.1f}mm) = {t_total:.2f}mm')
    else:
        t_total = t_cemented_min
        note = f'整体负光焦度，取最小总厚 {t_cemented_min:.1f}mm'

    # 按光焦度强度比例分配两片厚度（强片略厚）
    abs_phi = [abs(phi2), abs(phi3)]
    sum_phi = sum(abs_phi)
    if sum_phi < 1e-10:
        t2, t3 = t_total / 2, t_total / 2
    else:
        # 确保每片不低于最小中心厚度
        t2 = max(t_total * abs_phi[0] / sum_phi, 1.0)
        t3 = max(t_total * abs_phi[1] / sum_phi, 1.0)
        # 如果分配后总和偏离，等比归一化
        scale = t_total / (t2 + t3)
        t2 *= scale
        t3 *= scale

    return round(t2, 2), round(t3, 2), note


def compute_initial_structure(
        glass_names,
        nd_values,
        focal_lengths_mm,
        cemented_pairs,
        spacings_mm,
        D_mm,
        min_R_mm,
        t_edge_min,
        t_center_min,
        t_cemented_min,
        h1=None,
        u0=0.0,
        pbar_overrides=None,
        gap_thresh_mm: float = 5.0,
        delta_c_min: float = 1.0 / 300.0,
        ubar0: float = 0.1,
        w_SI: float = 1.0,
        w_SII: float = 2.0,
        w_SIV: float = 0.5):
    """
    计算薄透镜系统的初始结构（p、q、R、厚度）。

    参数
    ----
    glass_names      : list[str]，各片玻璃牌号
    nd_values        : dict，玻璃折射率 {牌号: nd}
    focal_lengths_mm : list[float]，各片焦距（mm）
    cemented_pairs   : list[tuple]，胶合对（0-based 索引）
    spacings_mm      : list[float]，相邻片间距（mm）
    D_mm             : float，通光口径（mm）
    min_R_mm         : float，最小曲率半径约束（mm）
    t_edge_min       : float，最小边缘厚度（mm）
    t_center_min     : float，最小中心厚度（mm）
    t_cemented_min   : float，胶合对最小总厚度（mm）
    h1               : float 或 None，边缘光线初始高度（None → D/2）
    u0               : float，初始折射角（0 = 平行光）
    pbar_overrides   : dict 或 None，{片索引(0-based): p̄值}
        用于变焦组（G2、G3 等）的 h⁴ 加权平均共轭因子覆盖。
        若某片索引在此字典中，则步骤2中用 p̄ 替换单点追迹的 p 值，
        从而使该片的形状因子 q 在整个变焦行程上综合球差最小。
        G1（平行光入射，p 恒为 1）和 G4（固定共轭）无需传入此参数。
        示例：pbar_overrides={0: -0.178, 1: -0.052}（G2 为片0，G3 为片1）
    """

    N   = len(glass_names)
    phi = [1.0 / f for f in focal_lengths_mm]
    nd  = [nd_values[g] for g in glass_names]
    if h1 is None:
        h1 = D_mm / 2.0
    c_max = 1.0 / min_R_mm
    cemented_set = set(idx for pair in cemented_pairs for idx in pair)
    if pbar_overrides is None:
        pbar_overrides = {}

    # ── 步骤1：近轴边缘光线追迹（参考位置，用于胶合对 SI 优化）──
    h_k, u_in, u_out = [], [], []
    hk, uk = h1, u0
    for i in range(N):
        h_k.append(hk)
        u_in.append(uk)
        uk = uk - hk * phi[i]
        u_out.append(uk)
        if i < N - 1:
            hk = hk + spacings_mm[i] * uk

    # ── 步骤2：共轭因子 p ─────────────────────────────────────
    # 单点追迹结果作为基础值；pbar_overrides 中的片位用变焦
    # 加权平均 p̄ 覆盖，以代表全变焦行程的折中最优形状。
    p_source = []   # 记录每片 p 的来源（用于打印）
    p = []
    for i in range(N):
        if i in pbar_overrides:
            p.append(float(pbar_overrides[i]))
            p_source.append('zoom_avg')
        else:
            denom = u_out[i] - u_in[i]
            if abs(denom) < 1e-12:
                p.append(0.0)
            else:
                p.append((u_out[i] + u_in[i]) / denom)
            p_source.append('single_pos')

    # ── 步骤3：形状因子 q（非胶合片）─────────────────────────
    def q_opt(n, pv):
        return -2 * (n**2 - 1) * pv / (n + 2)

    q = [None if i in cemented_set else q_opt(nd[i], p[i])
         for i in range(N)]

    # ── 步骤4：单片曲率半径 ───────────────────────────────────
    def radii_single(q_val, phi_val, n_val, c_max_val):
        """
        由形状因子 q 反算单片透镜的曲率半径 R1、R2。

        若某面理想曲率 |c| > c_max_val，截断到 ±c_max_val，
        并调整另一面补偿以保持光焦度 φ = (n-1)(c1-c2) 不变。
        若双面均超限则双面都贴边（φ 会有轻微偏离，输出警告）。

        返回 (R1, R2, note_front, note_back)
            note_front / note_back : None（未截断）或 str（说明截断原因）
        """
        c1 = phi_val * (1 + q_val) / (2 * (n_val - 1))
        c2 = phi_val * (q_val - 1) / (2 * (n_val - 1))
        c1_ideal, c2_ideal = c1, c2
        note1 = note2 = None

        # ── c1 超限：截断 c1，补偿 c2 以保持 φ ──────────────
        if abs(c1) > c_max_val + 1e-12:
            c1_clamped = c_max_val if c1 > 0 else -c_max_val
            c2 = c1_clamped - phi_val / (n_val - 1)
            R1_ideal_str = (f'{1/c1_ideal:+.2f}mm'
                            if abs(c1_ideal) > 1e-12 else '∞')
            note1 = (f'已截断（Seidel最优 R={R1_ideal_str} < min_R，'
                     f'φ 补偿已转移至后表面）')
            c1 = c1_clamped

        # ── c2 超限：截断 c2，补偿 c1 ───────────────────────
        if abs(c2) > c_max_val + 1e-12:
            c2_clamped = c_max_val if c2 > 0 else -c_max_val
            c1_new     = c2_clamped + phi_val / (n_val - 1)
            R2_ideal_str = (f'{1/c2_ideal:+.2f}mm'
                            if abs(c2_ideal) > 1e-12 else '∞')
            if abs(c1_new) > c_max_val + 1e-12:
                # 双面均超限，两面都贴边，φ 轻微偏离目标
                c1_new = c_max_val if c1_new > 0 else -c_max_val
                note2 = (f'已截断（Seidel最优 R={R2_ideal_str} < min_R，'
                         f'双面均达极限，φ 轻微偏离目标，建议增大 min_f_mm）')
                note1 = ((note1 + '；') if note1 else '') + \
                        '（双面截断，φ 存在轻微偏离）'
            else:
                note2 = (f'已截断（Seidel最优 R={R2_ideal_str} < min_R，'
                         f'φ 补偿已转移至前表面）')
            c2 = c2_clamped
            c1 = c1_new

        R1 = 1.0 / c1 if abs(c1) > 1e-12 else float('inf')
        R2 = 1.0 / c2 if abs(c2) > 1e-12 else float('inf')
        return R1, R2, note1, note2

    # ── 步骤5：胶合双片——可行域内最小化 |SI| ─────────────────
    cem_results = {}

    for ci, cj in cemented_pairs:
        n2, n3   = nd[ci], nd[cj]
        phi2_val = phi[ci]
        phi3_val = phi[cj]
        h_c      = h_k[ci]
        u_c      = u_in[ci]

        # 三面曲率偏移量（固定，由折射率和光焦度决定）
        off_cem = -phi2_val / (n2 - 1)
        off_3   = -phi3_val / (n3 - 1)
        off_tot = off_cem + off_3

        # c₁ 可行域（每面 |c| ≤ c_max）
        c1_lo = max(-c_max, -c_max - off_cem, -c_max - off_tot)
        c1_hi = min(c_max,   c_max - off_cem,  c_max - off_tot)
        feasible = c1_lo < c1_hi

        def SI(c1):
            c_c = c1 + off_cem
            c3  = c_c + off_3
            u, nc, acc = u_c, 1.0, 0.0
            for cs, nn in zip([c1, c_c, c3], [n2, n3, 1.0]):
                A     = nc * u + (nn - nc) * cs * h_c
                u_nxt = A / nn
                acc  += -A**2 * h_c * (u_nxt / nn - u / nc)
                u, nc = u_nxt, nn
            return acc

        if feasible:
            pts = np.linspace(c1_lo + 1e-7, c1_hi - 1e-7, 500)
            si_pts = [SI(c) for c in pts]
            zeros = [brentq(SI, pts[j], pts[j+1])
                     for j in range(len(pts)-1)
                     if si_pts[j] * si_pts[j+1] < 0]
            if zeros:
                c1_ch = min(zeros, key=abs)
                method = 'SI=0（可行域内精确消球差）'
            else:
                res   = minimize_scalar(lambda c: SI(c)**2,
                                        bounds=(c1_lo + 1e-7, c1_hi - 1e-7),
                                        method='bounded')
                c1_ch = res.x
                method = f'|SI|最小={abs(SI(c1_ch)):.4f}（可行域内最优，残余交Zemax处理）'
        else:
            # [BUG-FIX] 原代码用 bounds=(-0.5, 0.5)，完全绕开了 c_max 约束，
            # 导致"可行域为空"时三面曲率可能远超 min_R_mm。
            #
            # 根因：可行域为空意味着在给定光焦度下，不存在让三面同时满足
            # |c| ≤ c_max 的 c1。此时只能退而求其次：
            #   • 将 c1 的搜索域限制在 [-c_max, c_max]（至少前表面不超限）
            #   • c_cem 和 c3 由 c1 推导得出，可能仍超限——在后续 clamp_notes
            #     中标记，V1 验证会逐面提示。
            #   • 若 c_max 域内 SI 无极值，退化到全域 (-c_max, c_max) 取最小值。
            #
            # 这比原来的 (-0.5, 0.5) 严格得多（|R|≥2mm 实际等于不设约束），
            # 同时保留了"可行域为空"的警告，提示用户需换玻璃或放宽 min_R_mm。
            res = minimize_scalar(
                lambda c: SI(c)**2,
                bounds=(-c_max + 1e-7, c_max - 1e-7),
                method='bounded'
            )
            c1_ch = res.x
            method = (f'⚠ 可行域为空（三面无法同时满足 |R|≥{min_R_mm:.1f}mm）！'
                      f'全局|SI|最小={abs(SI(c1_ch)):.4f}，'
                      f'c_cem/c3 可能仍超限（见 V1 验证 ↳ 提示）。'
                      f'建议换焦距更长的玻璃或适当放宽 S_MIN_R_MM。')

        c_cem = c1_ch + off_cem
        c3_v  = c_cem + off_3

        # ── 记录胶合对各面的截断说明（传递给 clamp_notes）──────────
        # 逐面检查是否超限，超限面在面序列组装时写入 clamp_notes。
        cem_face_notes = {}
        for face_tag, c_val, label in [
            ('front',  c1_ch, f'片{ci+1}({glass_names[ci]}) 前表面'),
            ('cement', c_cem, f'片{ci+1}/{cj+1} 胶合面'),
            ('back',   c3_v,  f'片{cj+1}({glass_names[cj]}) 后表面'),
        ]:
            if abs(c_val) > c_max + 1e-9:
                R_actual  = 1.0 / c_val if abs(c_val) > 1e-12 else float('inf')
                if feasible:
                    note = (f'⚠ 胶合对球差优化结果 R={R_actual:+.2f}mm '
                            f'< min_R（{min_R_mm:.1f}mm），可行域计算存在数值误差，'
                            f'建议适当提高 S_MIN_R_MM。')
                else:
                    note = (f'⚠ 可行域为空，无法同时满足三面 |R|≥{min_R_mm:.1f}mm；'
                            f'当前 R={R_actual:+.2f}mm。'
                            f'→ 换焦距更长的玻璃或适当放宽 S_MIN_R_MM。')
                cem_face_notes[label] = note

        cem_results[(ci, cj)] = dict(
            c1=c1_ch, c_cem=c_cem, c3=c3_v,
            R1   = 1.0/c1_ch  if abs(c1_ch) >1e-12 else float('inf'),
            R_cem= 1.0/c_cem  if abs(c_cem) >1e-12 else float('inf'),
            R3   = 1.0/c3_v   if abs(c3_v)  >1e-12 else float('inf'),
            SI   = SI(c1_ch),
            method=method, feasible=feasible,
            c1_lo=c1_lo, c1_hi=c1_hi,
            face_notes=cem_face_notes,   # 新增：各面超限说明
        )

    # ══ 步骤5b：联合曲率优化（SLSQP）═════════════════════════════════
    # 将全组所有面的曲率作为联合优化变量，最小化 Σ SI_k²（串行光线传播），
    # 同时施加光焦度守恒（等式）和相邻面曲率差（不等式）约束。
    # 以步骤3-5的解耦结果为初始值，保证收敛起点物理合理。
    # 若优化不收敛，降级使用解耦初始值，不影响后续流程。

    # 3a. 建立优化变量索引表
    var_map_jt = {}    # (lens_idx, face_type_str) → var_idx
    units_jt   = []    # [(first_idx, last_idx, [var_idxs], is_cemented)]
    _proc_jt   = set()
    _vi_jt     = 0
    for _i in range(N):
        if _i in _proc_jt:
            continue
        _cem_jt = next((k for k in cemented_pairs if k[0] == _i), None)
        if _cem_jt:
            _ci_jt, _cj_jt = _cem_jt
            var_map_jt[(_ci_jt, 'front')]  = _vi_jt;  _vi_jt += 1
            var_map_jt[(_ci_jt, 'cement')] = _vi_jt;  _vi_jt += 1
            var_map_jt[(_cj_jt, 'back')]   = _vi_jt;  _vi_jt += 1
            units_jt.append((_ci_jt, _cj_jt,
                              [_vi_jt - 3, _vi_jt - 2, _vi_jt - 1], True))
            _proc_jt.add(_cj_jt)
        else:
            var_map_jt[(_i, 'front')] = _vi_jt;  _vi_jt += 1
            var_map_jt[(_i, 'back')]  = _vi_jt;  _vi_jt += 1
            units_jt.append((_i, _i, [_vi_jt - 2, _vi_jt - 1], False))
        _proc_jt.add(_i)
    n_vars_jt = _vi_jt

    # 3e. 构建初始值 x0（来自解耦结果）
    x0_jt = np.zeros(n_vars_jt)
    _proc_x0 = set()
    for _i in range(N):
        if _i in _proc_x0:
            continue
        _cem_x0 = next((k for k in cemented_pairs if k[0] == _i), None)
        if _cem_x0:
            _cj_x0  = _cem_x0[1]
            _cr_x0  = cem_results[_cem_x0]
            x0_jt[var_map_jt[(_i, 'front')]]    = _cr_x0['c1']
            x0_jt[var_map_jt[(_i, 'cement')]]   = _cr_x0['c_cem']
            x0_jt[var_map_jt[(_cj_x0, 'back')]] = _cr_x0['c3']
            _proc_x0.add(_cj_x0)
        elif _i not in cemented_set:
            _R1x, _R2x, _, _ = radii_single(q[_i], phi[_i], nd[_i], c_max)
            x0_jt[var_map_jt[(_i, 'front')]] = (
                1.0 / _R1x if 1e-12 < abs(_R1x) < 1e6 else 0.0)
            x0_jt[var_map_jt[(_i, 'back')]] = (
                1.0 / _R2x if 1e-12 < abs(_R2x) < 1e6 else 0.0)
        _proc_x0.add(_i)

    # 3b. 目标函数：全组 w_SI·SI² + w_SII·SII² + w_SIV·SIV²
    #     同时追迹边缘光线(h,u)和主光线(hb,ub)，允许面间正负抵消
    def _joint_obj(x):
        # 边缘光线初始条件
        _hj,  _uj  = h1,  u0
        # 主光线初始条件：假设光阑在组前，ȳ₀=0，ū₀=ubar0
        _hbj, _ubj = 0.0, ubar0
        # 拉格朗日不变量平方：H = n₀(u₀·ȳ₀ - ū₀·y₀) = -(ubar0·h1)
        _H2 = (ubar0 * h1) ** 2
        # 三项像差线性累加（最后整体平方，允许面间正负抵消）
        _si_sum  = 0.0
        _sii_sum = 0.0
        _siv_sum = 0.0
        for _fi_j, _li_j, _vj, _is_c in units_jt:
            if _is_c:
                _n2j, _n3j = nd[_fi_j], nd[_li_j]
                for _cs_j, _nb_j, _na_j in [
                    (x[_vj[0]], 1.0,  _n2j),
                    (x[_vj[1]], _n2j,  _n3j),
                    (x[_vj[2]], _n3j, 1.0),
                ]:
                    _Aj   = _nb_j * _uj  + (_na_j - _nb_j) * _cs_j * _hj
                    _Abj  = _nb_j * _ubj + (_na_j - _nb_j) * _cs_j * _hbj
                    _unj  = _Aj  / _na_j
                    _ubnj = _Abj / _na_j
                    _du_n = _unj / _na_j - _uj / _nb_j
                    _d1_n = 1.0 / _na_j - 1.0 / _nb_j
                    _si_sum  += _Aj**2      * _hj * _du_n
                    _sii_sum += _Aj * _Abj  * _hj * _du_n
                    _siv_sum += _H2 * _cs_j * _d1_n if abs(_cs_j) > 1e-14 else 0.0
                    _uj  = _unj
                    _ubj = _ubnj
            else:
                _nij = nd[_fi_j]
                # 前表面（n_before=1 → n_after=ni）
                _c1j  = x[_vj[0]]
                _A1j  = _uj  + (_nij - 1.0) * _c1j * _hj
                _Ab1j = _ubj + (_nij - 1.0) * _c1j * _hbj
                _umj  = _A1j  / _nij
                _ubmj = _Ab1j / _nij
                _du1  = _umj / _nij - _uj
                _d1_1 = 1.0 / _nij - 1.0
                _si_sum  += _A1j**2       * _hj * _du1
                _sii_sum += _A1j * _Ab1j  * _hj * _du1
                _siv_sum += _H2 * _c1j * _d1_1 if abs(_c1j) > 1e-14 else 0.0
                # 后表面（n_before=ni → n_after=1）
                _c2j  = x[_vj[1]]
                _A2j  = _nij * _umj  + (1.0 - _nij) * _c2j * _hj
                _Ab2j = _nij * _ubmj + (1.0 - _nij) * _c2j * _hbj
                _du2  = _A2j - _umj / _nij
                _d1_2 = 1.0 - 1.0 / _nij
                _si_sum  += _A2j**2       * _hj * _du2
                _sii_sum += _A2j * _Ab2j  * _hj * _du2
                _siv_sum += _H2 * _c2j * _d1_2 if abs(_c2j) > 1e-14 else 0.0
                _uj  = _A2j
                _ubj = _Ab2j
            if _li_j < N - 1:
                _hj  += spacings_mm[_li_j] * _uj
                _hbj += spacings_mm[_li_j] * _ubj
        return w_SI * _si_sum**2 + w_SII * _sii_sum**2 + w_SIV * _siv_sum**2

    # 3c. 等式约束：每片光焦度守恒 φ = (n-1)(c_front - c_back)
    _jconstr = []
    for _fi_c, _li_c, _vc, _is_cc in units_jt:
        if _is_cc:
            _n2c, _n3c       = nd[_fi_c], nd[_li_c]
            _phi_ci, _phi_cj = phi[_fi_c], phi[_li_c]
            _jconstr.append({'type': 'eq',
                             'fun': (lambda x, v=_vc, n2=_n2c, pc=_phi_ci:
                                     (n2 - 1) * (x[v[0]] - x[v[1]]) - pc)})
            _jconstr.append({'type': 'eq',
                             'fun': (lambda x, v=_vc, n3=_n3c, pc=_phi_cj:
                                     (n3 - 1) * (x[v[1]] - x[v[2]]) - pc)})
        else:
            _nic, _phi_ic = nd[_fi_c], phi[_fi_c]
            _jconstr.append({'type': 'eq',
                             'fun': (lambda x, v=_vc, ni=_nic, pi=_phi_ic:
                                     (ni - 1) * (x[v[0]] - x[v[1]]) - pi)})

    # 3c. 不等式约束：相邻单元边界面曲率差 ≥ delta_c_min（防止冗余面）
    _adj_pairs_jt = []   # 记录触发约束的相邻对，用于诊断打印
    for _ui in range(len(units_jt) - 1):
        _li_u  = units_jt[_ui][1]
        _fi_u  = units_jt[_ui + 1][0]
        _gap_u = spacings_mm[_li_u] if _li_u < len(spacings_mm) else 0.0
        if _gap_u < gap_thresh_mm:
            _ib_u = units_jt[_ui][2][-1]       # 当前单元最后一面
            _if_u = units_jt[_ui + 1][2][0]    # 下一单元第一面
            _adj_pairs_jt.append((_li_u, _fi_u, _ib_u, _if_u, _gap_u))
            _jconstr.append({'type': 'ineq',
                             'fun': (lambda x, ib=_ib_u, iff=_if_u:
                                     abs(x[ib] - x[iff]) - delta_c_min)})

    # 3f. 调用 SLSQP 优化器
    _si0_jt = _joint_obj(x0_jt)
    _jres = minimize(
        _joint_obj,
        x0_jt,
        method='SLSQP',
        bounds=[(-c_max, c_max)] * n_vars_jt,
        constraints=_jconstr,
        options={'ftol': 1e-10, 'maxiter': 2000, 'disp': False},
    )
    if _jres.success:
        x_final_jt    = _jres.x
        _joint_msg_jt = '收敛'
    else:
        x_final_jt    = x0_jt
        _joint_msg_jt = f'未收敛（{_jres.message}），降级使用解耦初始值'
    _si_final_jt = _joint_obj(x_final_jt)

    # 用 x_final_jt 更新 cem_results（胶合对）和 q / _final_radii（单片）
    _final_radii = {}   # {lens_idx: (R1, R2)}，非胶合单片用
    for _fi_u, _li_u, _vu, _is_cu in units_jt:
        if _is_cu:
            _c1f  = x_final_jt[_vu[0]]
            _ccf  = x_final_jt[_vu[1]]
            _c3f  = x_final_jt[_vu[2]]
            cem_results[(_fi_u, _li_u)].update(dict(
                c1    = _c1f,
                c_cem = _ccf,
                c3    = _c3f,
                R1    = 1.0 / _c1f if abs(_c1f) > 1e-12 else float('inf'),
                R_cem = 1.0 / _ccf if abs(_ccf) > 1e-12 else float('inf'),
                R3    = 1.0 / _c3f if abs(_c3f) > 1e-12 else float('inf'),
            ))
        else:
            _c1f = x_final_jt[_vu[0]]
            _c2f = x_final_jt[_vu[1]]
            _final_radii[_fi_u] = (
                1.0 / _c1f if abs(_c1f) > 1e-12 else float('inf'),
                1.0 / _c2f if abs(_c2f) > 1e-12 else float('inf'),
            )
            if abs(phi[_fi_u]) > 1e-12:
                q[_fi_u] = (_c1f + _c2f) * (nd[_fi_u] - 1) / phi[_fi_u]

    clamp_notes = {}   # 早期初始化, 5c 可能往里写; 后续步骤会继续写入

    # [TEMP] 5c 缩放总开关. False = 跳过 5c, 走原始 SLSQP 输出 + Zemax DLS 路径.
    _ENABLE_5C_SCALING = False
    if not _ENABLE_5C_SCALING:
        print(f"\n【步骤5c】EFL 缩放修正  [跳过, _ENABLE_5C_SCALING=False]")
    else:
        # ══ 步骤5c：EFL 缩放修正 ════════════════════════════════════════════
        # 目的: 让本组按 ABCD 全面追迹得到的厚透镜 EFL 精确等于 f_target.
        # 方法: 所有曲率 c → k·c 统一缩放 (q 不变, 胶合面 SI=0 仍 SI=0).
        #       间距和厚度保持不变. brentq 求根 f_thick(k) = 1/Σφᵢ.
        # 副作用: 部分面 |R| 可能突破 min_R_mm; 触发 V1 警告 (clamp_notes).

        # 5c.1: 构造当前面数据 (按光线传播顺序, 与 surfaces 序列一致)
        _scaled_proc = set()
        _curr_face_data = []   # [(c, n_after, t_placeholder, lens_idx, kind, cem_pair)]
        for _i in range(N):
            if _i in _scaled_proc:
                continue
            _cf = next((k for k in cemented_pairs if k[0] == _i), None)
            if _cf:
                _ci, _cj = _cf
                _cr = cem_results[_cf]
                _curr_face_data.append((1.0/_cr['R1']  if abs(_cr['R1'])>1e-12 else 0.0,
                                        nd[_ci], 0.0, _ci, 'cf', _cf))
                _curr_face_data.append((1.0/_cr['R_cem'] if abs(_cr['R_cem'])>1e-12 else 0.0,
                                        nd[_cj], 0.0, _ci, 'cm', _cf))
                _curr_face_data.append((1.0/_cr['R3']  if abs(_cr['R3'])>1e-12 else 0.0,
                                        1.0, 0.0, _cj, 'cb', _cf))
                _scaled_proc.add(_cj)
            else:
                if _i in _final_radii:
                    _R1s, _R2s = _final_radii[_i]
                else:
                    _R1s, _R2s, _, _ = radii_single(q[_i], phi[_i], nd[_i], c_max)
                _curr_face_data.append((1.0/_R1s if abs(_R1s)>1e-12 else 0.0,
                                        nd[_i], 0.0, _i, 'front', None))
                _curr_face_data.append((1.0/_R2s if abs(_R2s)>1e-12 else 0.0,
                                        1.0, 0.0, _i, 'back', None))
            _scaled_proc.add(_i)

        # 5c.2: 算"参考厚度" (固定值, 仅用于 ABCD 评估; 缩放过程中不变)
        _ref_thickness = {}
        for _i in range(N):
            if _i in _ref_thickness:
                continue
            _cf = next((k for k in cemented_pairs if k[0] == _i), None)
            if _cf:
                _cj = _cf[1]
                _cr = cem_results[_cf]
                _t2, _t3, _ = compute_cemented_thickness(
                    _cr['R1'], _cr['R_cem'], _cr['R3'],
                    D_mm, phi[_i], phi[_cj],
                    t_cemented_min, t_edge_min)
                _ref_thickness[_i]  = _t2
                _ref_thickness[_cj] = _t3
            else:
                if _i in _final_radii:
                    _R1r, _R2r = _final_radii[_i]
                else:
                    _R1r, _R2r, _, _ = radii_single(q[_i], phi[_i], nd[_i], c_max)
                _t, _ = compute_thickness(_R1r, _R2r, D_mm, phi[_i] >= 0,
                                           t_edge_min, t_center_min)
                _ref_thickness[_i] = _t

        # 把 t_after 填进 _curr_face_data
        _filled = []
        for _idx, (_c_i, _na_i, _, _li_i, _kind_i, _cp_i) in enumerate(_curr_face_data):
            _is_last = (_idx == len(_curr_face_data) - 1)
            if _is_last:
                _t_after = 0.0
            elif _kind_i == 'cf':
                _t_after = _ref_thickness[_cp_i[0]]
            elif _kind_i == 'cm':
                _t_after = _ref_thickness[_cp_i[1]]
            elif _kind_i == 'front':
                _t_after = _ref_thickness[_li_i]
            else:
                _t_after = (spacings_mm[_li_i]
                            if _li_i < len(spacings_mm) else 0.0)
            _filled.append((_c_i, _na_i, _t_after, _li_i, _kind_i, _cp_i))
        _curr_face_data = _filled

        # 5c.3: f_thick(k)
        def _efl_with_scale(k_val):
            _M = np.eye(2)
            _n_prev = 1.0
            for _idx_e, (_c_e, _na_e, _t_e, _, _, _) in enumerate(_curr_face_data):
                _phi_e = (_na_e - _n_prev) * (k_val * _c_e)
                _R_mat = np.array([[1.0, 0.0], [-_phi_e, 1.0]])
                _M = _R_mat @ _M
                if _idx_e < len(_curr_face_data) - 1:
                    _T_mat = np.array([[1.0, _t_e / _na_e], [0.0, 1.0]])
                    _M = _T_mat @ _M
                _n_prev = _na_e
            _C_e = _M[1, 0]
            if abs(_C_e) < 1e-15:
                return float('inf')
            return -1.0 / _C_e

        # 5c.4: brentq 求根 f_thick(k) = 1/Σφᵢ
        _phi_sum = sum(phi)
        f_target_local = 1.0 / _phi_sum if abs(_phi_sum) > 1e-12 else float('inf')
        _efl_at_1 = _efl_with_scale(1.0)
        _scale_msg = ''
        _scale_factor_applied = 1.0

        # 先在大区间上扫描 f_thick(k), 总是构建并打印扫描表 (无论后续是否求解成功).
        # 用扫描点之间的"端点函数值符号 + 端点函数值绝对值合理性" 来识别非奇点区间.
        _scan_ks = [0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 1.7, 2.0, 2.5, 3.0, 5.0, 10.0]
        _scan_diffs = []
        for _ks in _scan_ks:
            _ev = _efl_with_scale(_ks)
            _diff = (_ev - f_target_local) if np.isfinite(_ev) else float('nan')
            _scan_diffs.append((_ks, _ev, _diff))
        _scan_table_str = '  k扫描表 (target={:+.4f}):\n'.format(f_target_local)
        for _ks, _ev, _diff in _scan_diffs:
            _evs = f'{_ev:+10.3f}' if np.isfinite(_ev) else '       inf'
            _dfs = f'{_diff:+10.3f}' if np.isfinite(_diff) else '       nan'
            _scan_table_str += f'    k={_ks:5.2f}  f_thick={_evs}  diff={_dfs}\n'

        # 选 bracket: 必须满足 (1) 端点 diff 异号 (2) 端点 EFL 都在合理量级
        # 合理量级判据: |f_thick| < 100 * |target|. 这能把跨奇点产生的 ±1e9 类
        # 假符号变化筛掉 (奇点附近 |EFL| 趋于无穷大).
        _max_reasonable = 100.0 * abs(f_target_local)
        _bracket = None
        for _i_b in range(len(_scan_diffs) - 1):
            _ka, _va, _da = _scan_diffs[_i_b]
            _kb, _vb, _db = _scan_diffs[_i_b + 1]
            if not (np.isfinite(_da) and np.isfinite(_db)):
                continue
            if _da * _db >= 0:
                continue
            if abs(_va) > _max_reasonable or abs(_vb) > _max_reasonable:
                continue   # 跨奇点的虚假异号
            _bracket = (_ka, _kb)
            break

        if not np.isfinite(f_target_local) or not np.isfinite(_efl_at_1):
            _scale_msg = ('EFL 缩放跳过: target 或 k=1 时 EFL 非有限\n'
                          + _scan_table_str)
        elif abs(_efl_at_1 - f_target_local) / abs(f_target_local) < 1e-3:
            _scale_msg = (f'EFL 缩放跳过: k=1 时偏差 '
                          f'{(_efl_at_1-f_target_local)/f_target_local*100:+.3f}% < 0.1%')
        elif _bracket is None:
            _scale_msg = ('EFL 缩放失败: 在 k∈[0.1, 10] 无合法异号区间 '
                          '(可能跨奇点或物理不可达).\n' + _scan_table_str)
        else:
            try:
                _k_solve = brentq(
                    lambda k: _efl_with_scale(k) - f_target_local,
                    _bracket[0], _bracket[1], xtol=1e-8, maxiter=200,
                )
                _efl_after = _efl_with_scale(_k_solve)
                # 防奇点二次校验: brentq 找到的 k_solve 处, EFL 必须真的接近 target.
                # 如果偏差 > 1%, 说明 brentq 在不连续函数上找到了假零点, 拒绝缩放.
                _post_err_pct = (abs(_efl_after - f_target_local) /
                                 abs(f_target_local) * 100)
                if _post_err_pct > 1.0 or not np.isfinite(_efl_after):
                    _scale_msg = (f'EFL 缩放失败: brentq 找到 k={_k_solve:.4f} 但 '
                                  f'EFL={_efl_after:+.3f} 远离 target '
                                  f'(偏差 {_post_err_pct:.2f}%, 怀疑跨奇点假零点); '
                                  f'保持原值.\n' + _scan_table_str)
                else:
                    _scale_factor_applied = _k_solve
                    _scale_msg = (f'EFL 缩放: k={_k_solve:.6f}  '
                                  f'(k=1 时 EFL={_efl_at_1:+.3f} → '
                                  f'k={_k_solve:.4f} 时 EFL={_efl_after:+.3f}, '
                                  f'target={f_target_local:+.3f}, '
                                  f'bracket=[{_bracket[0]}, {_bracket[1]}])')
            except (ValueError, RuntimeError) as _e:
                _scale_msg = (f'EFL 缩放失败: brentq 在 [{_bracket[0]}, '
                              f'{_bracket[1]}] 异常 ({_e}); 保持原值.\n'
                              + _scan_table_str)

        # 5c.5: 应用缩放, 写回 cem_results / _final_radii, 突破检查写 clamp_notes
        if abs(_scale_factor_applied - 1.0) > 1e-9:
            _k = _scale_factor_applied
            _scale_breach_notes = {}
            for _key_s, _cr_s in cem_results.items():
                for _r_field in ('c1', 'c_cem', 'c3'):
                    _cr_s[_r_field] = _cr_s[_r_field] * _k
                _cr_s['R1']    = (1.0 / _cr_s['c1']
                                  if abs(_cr_s['c1']) > 1e-12 else float('inf'))
                _cr_s['R_cem'] = (1.0 / _cr_s['c_cem']
                                  if abs(_cr_s['c_cem']) > 1e-12 else float('inf'))
                _cr_s['R3']    = (1.0 / _cr_s['c3']
                                  if abs(_cr_s['c3']) > 1e-12 else float('inf'))
                _ci_s, _cj_s = _key_s
                for _r_val, _label_s in [
                    (_cr_s['R1'], f'片{_ci_s+1}({glass_names[_ci_s]}) 前表面'),
                    (_cr_s['R_cem'], f'片{_ci_s+1}/{_cj_s+1} 胶合面'),
                    (_cr_s['R3'], f'片{_cj_s+1}({glass_names[_cj_s]}) 后表面'),
                ]:
                    if abs(_r_val) < min_R_mm - 1e-6 and abs(_r_val) > 1e-6:
                        _scale_breach_notes[_label_s] = (
                            f'⚠ EFL 缩放后 R={_r_val:+.2f}mm 突破 min_R_mm '
                            f'({min_R_mm:.1f}mm), 缩放系数 k={_k:.4f}.')
            for _li_s, (_R1_old, _R2_old) in list(_final_radii.items()):
                _c1_old = 1.0 / _R1_old if abs(_R1_old) > 1e-12 else 0.0
                _c2_old = 1.0 / _R2_old if abs(_R2_old) > 1e-12 else 0.0
                _c1_new = _c1_old * _k
                _c2_new = _c2_old * _k
                _R1_new = 1.0 / _c1_new if abs(_c1_new) > 1e-12 else float('inf')
                _R2_new = 1.0 / _c2_new if abs(_c2_new) > 1e-12 else float('inf')
                _final_radii[_li_s] = (_R1_new, _R2_new)
                for _r_val, _label_s in [
                    (_R1_new, f'片{_li_s+1}({glass_names[_li_s]}) 前表面'),
                    (_R2_new, f'片{_li_s+1}({glass_names[_li_s]}) 后表面'),
                ]:
                    if abs(_r_val) < min_R_mm - 1e-6 and abs(_r_val) > 1e-6:
                        _scale_breach_notes[_label_s] = (
                            f'⚠ EFL 缩放后 R={_r_val:+.2f}mm 突破 min_R_mm '
                            f'({min_R_mm:.1f}mm), 缩放系数 k={_k:.4f}.')
            clamp_notes.update(_scale_breach_notes)
        print(f"\n【步骤5c】EFL 缩放修正")
        print(f"  {_scale_msg}")

    # ── 整理折射面序列 ────────────────────────────────────────
    surfaces    = []   # (面描述, R值, 所属片索引, 是否是胶合面)
    # clamp_notes 已在【步骤5c】之前初始化, 此处不重复初始化以保留 5c 写入的内容
    processed   = set()

    for i in range(N):
        cem_first = next((k for k in cemented_pairs if k[0] == i), None)
        if i in processed:
            continue
        if cem_first:
            cr  = cem_results[cem_first]
            cj  = cem_first[1]
            surfaces.append((f'片{i+1}({glass_names[i]}) 前表面',
                              cr['R1'], i, False))
            surfaces.append((f'片{i+1}/{i+2} 胶合面',
                              cr['R_cem'], i, True))
            surfaces.append((f'片{i+2}({glass_names[cj]}) 后表面',
                              cr['R3'], cj, False))
            processed.add(cj)
            # 将胶合对各面的超限说明合并入 clamp_notes
            clamp_notes.update(cr.get('face_notes', {}))
        else:
            desc_f = f'片{i+1}({glass_names[i]}) 前表面'
            desc_b = f'片{i+1}({glass_names[i]}) 后表面'
            if i in _final_radii:
                R1, R2 = _final_radii[i]
                note1 = note2 = None
            else:
                R1, R2, note1, note2 = radii_single(q[i], phi[i], nd[i], c_max)
            surfaces.append((desc_f, R1, i, False))
            surfaces.append((desc_b, R2, i, False))
            if note1:
                clamp_notes[desc_f] = note1
            if note2:
                clamp_notes[desc_b] = note2

    # ── 步骤6：厚度计算 ───────────────────────────────────────
    thickness = {}     # key: 片索引 → (t_center, note)
    processed_t = set()

    for i in range(N):
        if i in processed_t:
            continue
        cem_first = next((k for k in cemented_pairs if k[0] == i), None)
        if cem_first:
            cj = cem_first[1]
            cr = cem_results[cem_first]
            t2, t3, note = compute_cemented_thickness(
                cr['R1'], cr['R_cem'], cr['R3'],
                D_mm, phi[i], phi[cj],
                t_cemented_min, t_edge_min)
            thickness[i]  = (t2, note + f'（前片 {t2}mm）')
            thickness[cj] = (t3, f'后片 {t3}mm（与前片共 {t2+t3:.2f}mm）')
            processed_t.add(cj)
        else:
            is_pos = phi[i] >= 0
            if i in _final_radii:
                R1, R2 = _final_radii[i]
            else:
                R1, R2, _, _ = radii_single(q[i], phi[i], nd[i], c_max)
            t, note = compute_thickness(R1, R2, D_mm, is_pos,
                                        t_edge_min, t_center_min)
            thickness[i] = (t, note)
        processed_t.add(i)

    # ── 打印结果 ─────────────────────────────────────────────
    sep = "=" * 72

    print(f"\n{sep}")
    print(f"  镜组初始结构计算结果")
    print(f"  口径 D={D_mm}mm  最小曲率半径 min_R={min_R_mm:.1f}mm"
          f"（R/D ≥ {min_R_mm/D_mm:.2f}）")
    print(sep)

    # 光线追迹
    print(f"\n【步骤1】近轴边缘光线追迹（{'平行光入射' if u0==0 else f'u₀={u0:.4f}rad'}，h₁={h1}mm）")
    print(f"  {'片':>3}  {'玻璃':>10}  {'f(mm)':>8}  "
          f"{'h(mm)':>9}  {'u_in(rad)':>12}  {'u_out(rad)':>12}")
    print("  " + "-"*60)
    for i in range(N):
        print(f"  片{i+1}  {glass_names[i]:>10}  {focal_lengths_mm[i]:>+8.2f}  "
              f"{h_k[i]:>9.4f}  {u_in[i]:>+12.7f}  {u_out[i]:>+12.7f}")
    f_sys = -h1 / u_out[-1] if abs(u_out[-1]) > 1e-10 else float('inf')
    print(f"  系统等效焦距（近轴估算）≈ {f_sys:.2f}mm")

    # p 和 q
    print(f"\n【步骤2&3】共轭因子 p  和  形状因子 q")
    print(f"  {'片':>3}  {'玻璃':>10}  {'nd':>7}  {'p':>10}  {'来源':>10}  {'q':>10}")
    print("  " + "-"*62)
    for i in range(N):
        q_str   = f'{q[i]:>+10.5f}' if q[i] is not None else f"{'(胶合)':>10}"
        src_str = '变焦加权' if p_source[i] == 'zoom_avg' else '单点追迹'
        print(f"  片{i+1}  {glass_names[i]:>10}  {nd[i]:>7.5f}  "
              f"{p[i]:>+10.5f}  {src_str:>10}  {q_str}")

    # 胶合对详情
    for key, cr in cem_results.items():
        ci, cj = key
        print(f"\n【步骤4】胶合对 片{ci+1}({glass_names[ci]}) + 片{cj+1}({glass_names[cj]})")
        if cr['feasible']:
            lo_R = 1/cr['c1_hi'] if abs(cr['c1_hi'])>1e-9 else float('inf')
            hi_R = 1/cr['c1_lo'] if abs(cr['c1_lo'])>1e-9 else float('inf')
            print(f"  c₁ 可行域：[{cr['c1_lo']:+.5f}, {cr['c1_hi']:+.5f}] mm⁻¹"
                  f"  →  R₁ ∈ [{lo_R:+.1f}, {hi_R:+.1f}]mm")
        else:
            print(f"  ⚠ 可行域为空（min_R_mm={min_R_mm:.1f}mm 在本组合下无法同时满足）")
        print(f"  选取方法：{cr['method']}")

    # 联合优化诊断输出
    print(f"\n【联合优化】目标函数（wSI·SI²+wSII·SII²+wSIV·SIV²）"
          f"[wSI={w_SI}, wSII={w_SII}, wSIV={w_SIV}]："
          f"{_si0_jt:.6e} → {_si_final_jt:.6e}")
    if _adj_pairs_jt:
        print(f"  相邻面曲率差约束触发情况：")
        for _li_d, _fi_d, _ib_d, _if_d, _gap_d in _adj_pairs_jt:
            _dc = abs(x_final_jt[_ib_d] - x_final_jt[_if_d])
            _dR = 1.0 / _dc if _dc > 1e-12 else float('inf')
            _active = '已激活' if _dc <= delta_c_min + 1e-9 else '未激活'
            print(f"    片{_li_d+1}后表面 ↔ 片{_fi_d+1}前表面："
                  f"Δc={_dc:.6f}mm⁻¹  (ΔR≈{_dR:.1f}mm，"
                  f"气隙={_gap_d:.1f}mm，约束{_active})")
    else:
        print(f"  无相邻面满足触发条件（所有相邻间距 ≥ {gap_thresh_mm}mm）")
    print(f"  优化状态：{_joint_msg_jt}")

    # 曲率半径汇总
    print(f"\n【曲率半径】（符号：R>0 曲率心在右/朝左为凸，R<0 曲率心在左）")
    print(f"  {'面序':>4}  {'描述':38}  {'R(mm)':>10}  {'R/D':>6}  状态")
    print("  " + "-"*68)
    for idx, (desc, R, _, is_cem) in enumerate(surfaces, 1):
        if abs(R) > 1e5:
            R_str, rd_str, mark = f"{'∞':>10}", f"{'∞':>6}", "✓ 平面"
        else:
            rd = abs(R) / D_mm
            R_str  = f'{R:>+10.2f}'
            rd_str = f'{rd:6.2f}'
            mark   = ('✓' if rd >= 1.0 else
                      '✓ (偏强)' if rd >= 0.8 else
                      '⚠ 偏小' if rd >= 0.6 else '✗ 过小')
        print(f"  面{idx:<3}  {desc:38}  {R_str}  {rd_str}  {mark}")

    # 厚度汇总
    print(f"\n【中心厚度】（最小边缘厚={t_edge_min}mm，最小中心厚={t_center_min}mm）")
    print(f"  {'片':>3}  {'玻璃':>10}  {'中心厚(mm)':>10}  说明")
    print("  " + "-"*68)
    for i in range(N):
        t, note = thickness[i]
        print(f"  片{i+1}  {glass_names[i]:>10}  {t:>10.2f}  {note}")

    # Zemax 汇总表
    print(f"\n{sep}")
    print(f"  Zemax 输入汇总（按面序号）")
    print(sep)
    print(f"  {'面序':>4}  {'描述':38}  {'R(mm)':>10}  {'厚度(mm)':>9}  {'玻璃':>10}")
    print("  " + "-"*78)

    surf_list = list(enumerate(surfaces, 1))
    for surf_idx, (desc, R, lens_idx, is_cem) in surf_list:
        R_str = f'{R:>+10.2f}' if abs(R) < 1e5 else f"{'∞':>10}"

        # 确定本面之后的介质和厚度
        # 胶合面 → 后片玻璃 + 后片中心厚
        # 前表面（后紧跟胶合面除外）→ 本片玻璃 + 本片中心厚
        # 后表面 → 空气间隔（或像面）

        if is_cem:
            # 胶合面后是后片玻璃
            next_entries = [(li, d) for _, (d, _, li, _)
                            in surf_list[surf_idx:] if li != lens_idx]
            if next_entries:
                next_li = next_entries[0][0]
                t_str   = f'{thickness[next_li][0]:>9.2f}'
                mat_str = glass_names[next_li]
            else:
                t_str = f'{"(像面)":>9}'; mat_str = ''
        else:
            # 查看下一个面
            if surf_idx < len(surf_list):
                _, (_, _, next_li, next_is_cem) = surf_list[surf_idx]
            else:
                next_li, next_is_cem = -1, False

            same_lens_next = (next_li == lens_idx)

            if same_lens_next and next_is_cem:
                # 前表面，下一个是胶合面 → 本片玻璃
                t_str   = f'{thickness[lens_idx][0]:>9.2f}'
                mat_str = glass_names[lens_idx]
            elif same_lens_next:
                # 前表面，后跟非胶合面（同片） → 本片玻璃
                t_str   = f'{thickness[lens_idx][0]:>9.2f}'
                mat_str = glass_names[lens_idx]
            else:
                # 后表面 → 空气间隔
                # spacings_mm[i] 是片i后到片i+1前的间距
                # 注意胶合组：胶合组最后一片的 lens_idx 可能不等于物理片序
                # 用 lens_idx 直接索引（它恰好等于0-based片号）
                if lens_idx < len(spacings_mm) and spacings_mm[lens_idx] > 0:
                    t_str   = f'{spacings_mm[lens_idx]:>9.2f}'
                    mat_str = 'AIR'
                elif surf_idx < len(surf_list):
                    t_str   = f'{0.0:>9.2f}'
                    mat_str = 'AIR'
                else:
                    t_str = f'{"(像面)":>9}'; mat_str = ''

        print(f"  面{surf_idx:<3}  {desc:38}  {R_str}  {t_str}  {mat_str}")

    # 工程警告
    bad_R = [(desc, R) for desc, R, _, _ in surfaces
             if abs(R) < float('inf') and abs(R)/D_mm < 0.8]
    if bad_R:
        print(f"\n  ⚠ 以下面的 R/D < 0.8，加工难度较高：")
        for desc, R in bad_R:
            print(f"    {desc}  R={R:+.2f}mm  R/D={abs(R)/D_mm:.2f}")
        print(f"  建议：回到穷举步骤提高 min_f_mm，选焦距更长的玻璃组合。")
    else:
        print(f"\n  ✓ 所有面 R/D ≥ 0.8，曲率半径均在可加工范围内。")

    print(f"\n  注：本结果为初级像差理论初始值，直接录入 Zemax 作为起点。")
    print(f"  建议将所有曲率半径和间距设为变量，以 RMS 波前差为目标进行优化。")
    print(sep)

    # ── 步骤7：计算主面位置 ────────────────────────────────────
    print(f"\n【主面位置】")
    delta_H, delta_Hp = compute_principal_planes(
        surfaces, thickness, nd_values, glass_names,
        spacings_mm, cemented_pairs)

    return dict(p=p, q=q, surfaces=surfaces, thickness=thickness,
                cem_results=cem_results, clamp_notes=clamp_notes,
                delta_H=delta_H, delta_Hp=delta_Hp)


