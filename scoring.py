"""
scoring.py
方案过滤与评分：有效性检验、约束验证、光学评分、成本评分。
"""

import numpy as np
from itertools import product as iterproduct   # [FIX] _process_one_combo 内部使用
from solver import build_and_solve, build_A_matrix, build_b_vec, solve_with_A

# ============================================================
# 第四部分：过滤与评分
# ============================================================

def is_valid(phis, structure, min_f_mm, max_f_mm):
    """检查每片光焦度的符号和焦距范围是否合法。"""
    for phi, role in zip(phis, structure):
        if role == 'pos' and phi <= 0:
            return False
        if role == 'neg' and phi >= 0:
            return False
        if abs(phi) < 1e-10:
            return False
        f_abs = abs(1.0 / phi)
        if f_abs < min_f_mm or f_abs > max_f_mm:
            return False
    return True


def verify_constraints(phis, glasses_param, phi_total, apo, b_fit):
    """
    验证广义约束满足情况。
    返回 (err_disp, err_apo)，两者应趋近 0。
    """
    Vgens  = [g[1] for g in glasses_param]
    dPgens = [g[2] for g in glasses_param]
    err_disp = abs(sum(p / v for p, v in zip(phis, Vgens)))

    if apo:
        # [BUG-FIX] 正确残差：Σφᵢ·(dP/V + b/V²) 应趋近 0（即零二级光谱条件）
        lhs = sum(p * (dP / v + b_fit / v ** 2)
                  for p, dP, v in zip(phis, dPgens, Vgens))
        err_apo = abs(lhs)
    else:
        err_apo = None

    return err_disp, err_apo


def optical_score(phis, ns, vs, cemented_pairs, err_apo=0.0, w_apo=500.0):
    """
    光学性能综合评分（越低越好）。
    SA：球差代理；P_ptz：Petzval场曲；dv_term：胶合面色散效率。

    err_apo : 二级光谱残差（来自 verify_constraints），软约束惩罚项。
              apo=False 时传入默认值 0.0，惩罚项自动为零，无副作用。
    w_apo   : APO 惩罚权重。越大越优先选消二级光谱好的方案，但不作为
              硬过滤门槛，轻微违反的方案仍可出现在候选列表中（排名靠后）。
              默认 500.0，是经验值——使 w_apo * err_apo 与 SA 项在同量级。
    """
    # [FIX-6] 跳过 n≈1 的异常数据，防止除零
    SA = sum(
        abs(p)**3 / (n - 1.0)**2
        for p, n in zip(phis, ns)
        if abs(n - 1.0) > 1e-6
    )
    P_ptz = sum(p / n for p, n in zip(phis, ns))

    dv_term = 0.0
    for (ci, cj) in cemented_pairs:
        dv = abs(vs[ci] - vs[cj])
        if dv > 1e-3:
            dv_term += 1.0 / dv

    # APO 软约束惩罚：err_apo 越大评分越差，但不直接过滤
    return 2.0 * SA + 1.5 * abs(P_ptz) + 0.3 * dv_term + w_apo * err_apo


def weighted_cost(phis, rel_costs):
    """光焦度加权均价（光焦度越强的片权重越大）。"""
    total_w = sum(abs(p) for p in phis)
    if total_w < 1e-10:
        return 0.0
    return sum(c * abs(p) for c, p in zip(rel_costs, phis)) / total_w


# ============================================================
# 第五部分：单组合处理函数（多进程工作单元）
# ============================================================

def _process_one_combo(args):
    """
    [OPT-3] 处理单个玻璃组合的顶层函数。

    必须是模块顶层函数（而非嵌套函数），Python 多进程才能正确 pickle 并分发。
    所有需要的参数打包成一个元组 args 传入，因为 executor.map 只传单个参数。

    返回该组合下满足所有约束的方案列表（通常是 0 或 1 个）。
    """
    (glass_combo, structure, actual_cemented, allow_duplicate_glass,
     phi_total, apo, b_fit,
     scan_indices, free_indices, scan_grids,
     min_f_mm, max_f_mm,
     tol_disp, w_apo, valid_cemented_pairs_map) = args

    names = [name for name, _ in glass_combo]

    # 重复玻璃检查
    if not allow_duplicate_glass and len(set(names)) < len(names):
        return []

    # 胶合面剪枝（使用预过滤合法配对表，O(1) 查表替代 O(|pool|²) 循环）
    # [FIX-5] 使用 n_ref（工作波长折射率）和 V_gen（广义阿贝数），
    # 而非可见光的 nd 和 vd，宽谱/近红外下物理可信。
    for (ci, cj) in actual_cemented:
        valid_pairs = valid_cemented_pairs_map.get((ci, cj), [])
        if not valid_pairs:
            return []
        # 当前组合的这对玻璃是否在合法配对集合中
        pair = (glass_combo[ci], glass_combo[cj])
        if pair not in valid_pairs:
            return []

    # 构建广义参数三元组 (n_ref, V_gen, dP_gen)
    glasses_param = [
        (g['n_ref'], g['V_gen'], g['dP_gen'])
        for _, g in glass_combo
    ]

    # [BUG-FIX] 快速物理可行性检查：消色差约束 Σφᵢ/Vᵢ=0 要求
    # Σ(role_sign / V_gen) 既有正项又有负项，否则方程组必然无解。
    # role_sign: pos→+1, neg→-1
    role_signs = [1.0 if r == 'pos' else -1.0 for r in structure]
    phi_over_v = [s / gp[1] for s, gp in zip(role_signs, glasses_param)
                  if gp[1] is not None and abs(gp[1]) > 1e-6]
    if phi_over_v and (all(x > 0 for x in phi_over_v) or all(x < 0 for x in phi_over_v)):
        return []

    ns        = [g['n_ref']    for _, g in glass_combo]
    vs        = [g['V_gen']    for _, g in glass_combo]
    Vgens     = [g['V_gen']    for _, g in glass_combo]
    rel_costs = [g['rel_cost'] for _, g in glass_combo]

    local_results = []

    # ════════ 向量化批量扫描 ════════
    n_constraints = 3 if apo else 2
    A = build_A_matrix(glasses_param, free_indices, apo, b_fit)
    if A is None:
        return []

    N = len(structure)

    # 如果没有扫描变量，只有一个点
    if not scan_grids:
        fixed_phis = {}
        b_vec = build_b_vec(phi_total, glasses_param, fixed_phis, apo, b_fit)
        sol = solve_with_A(A, b_vec, glasses_param, free_indices, fixed_phis, n_constraints)
        if sol is None:
            return []
        if not is_valid(sol, structure, min_f_mm, max_f_mm):
            return []
        err_disp, err_apo = verify_constraints(sol, glasses_param, phi_total, apo, b_fit)
        if err_disp > tol_disp:
            return []
        apo_err_val = err_apo if (apo and err_apo is not None) else 0.0
        os_val = optical_score(sol, ns, vs, actual_cemented, err_apo=apo_err_val, w_apo=w_apo)
        wc_val = weighted_cost(sol, rel_costs)
        return [{
            "names": names, "phis": sol, "ns": ns, "vs": vs, "Vgens": Vgens,
            "dPgens": [g['dP_gen'] for _, g in glass_combo],
            "dPgFs": [g['dPgF'] for _, g in glass_combo],
            "rel_costs": rel_costs, "opt_score": os_val, "cost_score": wc_val,
            "P_ptz": sum(p / n for p, n in zip(sol, ns)),
            "err_disp": err_disp, "err_apo": err_apo,
        }]

    # ── 批量构建所有扫描点 ──
    scan_points = np.array(list(iterproduct(*scan_grids)))  # shape: (M, n_scan)
    M = len(scan_points)

    # 预计算各扫描变量的固定贡献
    scan_V_inv = np.array([1.0 / glasses_param[idx][1] for idx in scan_indices])  # (n_scan,)

    b_all = np.zeros((n_constraints, M))
    b_all[0, :] = phi_total - scan_points.sum(axis=1)
    b_all[1, :] = -(scan_points * scan_V_inv).sum(axis=1)
    if apo:
        scan_apo_coeffs = np.array([
            glasses_param[idx][2] / glasses_param[idx][1] + b_fit / glasses_param[idx][1]**2
            for idx in scan_indices
        ])
        b_all[2, :] = -(scan_points * scan_apo_coeffs).sum(axis=1)

    # ── 批量求解：A @ x = b → x = A_inv @ b ──
    try:
        A_inv = np.linalg.inv(A)
    except np.linalg.LinAlgError:
        return []

    free_phis_all = A_inv @ b_all  # shape: (n_constraints, M)

    # ── 重建完整 phi 数组 (N, M) ──
    all_phis = np.zeros((N, M))
    for k, idx in enumerate(scan_indices):
        all_phis[idx, :] = scan_points[:, k]
    for k, idx in enumerate(free_indices):
        all_phis[idx, :] = free_phis_all[k, :]

    # ── 批量校验：符号 + 焦距范围 ──
    valid_mask = np.ones(M, dtype=bool)
    for i_elem in range(N):
        row = all_phis[i_elem, :]
        if structure[i_elem] == 'pos':
            valid_mask &= (row > 0)
        else:
            valid_mask &= (row < 0)
        abs_phi = np.abs(row)
        valid_mask &= (abs_phi > 1e-10)
        f_abs = 1.0 / np.maximum(abs_phi, 1e-15)
        valid_mask &= (f_abs >= min_f_mm) & (f_abs <= max_f_mm)

    # ── 批量校验：消色差约束 ──
    V_inv_all = np.array([1.0 / glasses_param[i][1] for i in range(N)])  # (N,)
    err_disp_all = np.abs((all_phis * V_inv_all[:, None]).sum(axis=0))  # (M,)
    valid_mask &= (err_disp_all <= tol_disp)

    # ── 提取通过校验的结果 ──
    valid_indices = np.where(valid_mask)[0]
    if len(valid_indices) == 0:
        return []

    # 只对通过的点计算评分（通常很少，几个到几十个）
    ns_arr = np.array(ns)

    for vi in valid_indices:
        sol = all_phis[:, vi].tolist()
        err_disp = err_disp_all[vi]

        if apo:
            dPgens_arr = [glasses_param[i][2] for i in range(N)]
            lhs = sum(sol[i] * (dPgens_arr[i] / glasses_param[i][1] + b_fit / glasses_param[i][1]**2)
                      for i in range(N))
            err_apo = abs(lhs)
        else:
            err_apo = None

        apo_err_val = err_apo if (apo and err_apo is not None) else 0.0
        os_val = optical_score(sol, ns, vs, actual_cemented, err_apo=apo_err_val, w_apo=w_apo)
        wc_val = weighted_cost(sol, rel_costs)

        local_results.append({
            "names": names, "phis": sol, "ns": ns, "vs": vs, "Vgens": Vgens,
            "dPgens": [g['dP_gen'] for _, g in glass_combo],
            "dPgFs": [g['dPgF'] for _, g in glass_combo],
            "rel_costs": rel_costs, "opt_score": os_val, "cost_score": wc_val,
            "P_ptz": sum(p / n for p, n in zip(sol, ns)),
            "err_disp": float(err_disp), "err_apo": err_apo,
        })

    # ── 每组合只保留光学评分最优的方案 ──
    if len(local_results) > 1:
        local_results.sort(key=lambda x: x["opt_score"])
        local_results = local_results[:1]

    return local_results



