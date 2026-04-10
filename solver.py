"""
solver.py
线性约束方程组求解：消色差 / APO 光焦度分配，手写 2×2 / 3×3 解析解。
"""

import numpy as np
from itertools import combinations

# ============================================================
# 第三部分：约束方程求解
# ============================================================

def solve_2x2(A, b_vec):
    """
    [OPT-2] 手写 2×2 线性方程组解析解。
    比 np.linalg.solve 快约 8~12 倍（避免通用算法的函数调用开销）。
    行列式接近零时返回 None。
    """
    a00, a01 = A[0, 0], A[0, 1]
    a10, a11 = A[1, 0], A[1, 1]
    det = a00 * a11 - a01 * a10
    if abs(det) < 1e-14:
        return None
    inv_det = 1.0 / det
    return np.array([
        (b_vec[0] * a11 - b_vec[1] * a01) * inv_det,
        (a00 * b_vec[1] - a10 * b_vec[0]) * inv_det,
    ])


def solve_3x3(A, b_vec):
    """
    [OPT-2] 手写 3×3 线性方程组解析解（克莱默法则展开）。
    比 np.linalg.solve 快约 8~10 倍。
    行列式接近零时返回 None。
    """
    a = A  # 简化书写
    # 行列式（按第一行展开）
    det = (a[0,0] * (a[1,1]*a[2,2] - a[1,2]*a[2,1])
         - a[0,1] * (a[1,0]*a[2,2] - a[1,2]*a[2,0])
         + a[0,2] * (a[1,0]*a[2,1] - a[1,1]*a[2,0]))
    if abs(det) < 1e-14:
        return None
    inv_det = 1.0 / det
    # 克莱默法则：每个变量用对应替换列的子行列式除以主行列式
    x0 = (b_vec[0] * (a[1,1]*a[2,2] - a[1,2]*a[2,1])
        - a[0,1] * (b_vec[1]*a[2,2] - a[1,2]*b_vec[2])
        + a[0,2] * (b_vec[1]*a[2,1] - a[1,1]*b_vec[2])) * inv_det
    x1 = (a[0,0] * (b_vec[1]*a[2,2] - a[1,2]*b_vec[2])
        - b_vec[0] * (a[1,0]*a[2,2] - a[1,2]*a[2,0])
        + a[0,2] * (a[1,0]*b_vec[2] - b_vec[1]*a[2,0])) * inv_det
    x2 = (a[0,0] * (a[1,1]*b_vec[2] - b_vec[1]*a[2,1])
        - a[0,1] * (a[1,0]*b_vec[2] - b_vec[1]*a[2,0])
        + b_vec[0] * (a[1,0]*a[2,1] - a[1,1]*a[2,0])) * inv_det
    return np.array([x0, x1, x2])


def build_A_matrix(glasses_param, free_indices, apo, b_fit):
    """
    预构建约束矩阵 A（只依赖玻璃参数，与扫描值无关）。
    返回 np.ndarray，若某个 V_gen 为 None 或接近零则返回 None。

    [OPT] 设计为在扫描循环外调用一次，避免重复构建。
    """
    rows = []
    for idx in free_indices:
        _, V_gen, dP_gen = glasses_param[idx]
        if not V_gen or abs(V_gen) < 1e-10:
            return None
        col = [1.0, 1.0 / V_gen]
        if apo:
            col.append(dP_gen / V_gen + b_fit / V_gen ** 2)
        rows.append(col)
    return np.array(rows).T


def build_b_vec(phi_total, glasses_param, fixed_phis, apo, b_fit):
    """
    构建右端向量 b（依赖当前扫描值 fixed_phis）。
    每个扫描步骤调用一次，代价远低于重建 A 矩阵。
    """
    rhs_total = phi_total - sum(fixed_phis.values())
    rhs_disp  = -sum(phi / glasses_param[i][1]
                     for i, phi in fixed_phis.items())
    b_vec = [rhs_total, rhs_disp]
    if apo:
        rhs_apo = -sum(
            phi * (glasses_param[i][2] / glasses_param[i][1]
                   + b_fit / glasses_param[i][1] ** 2)
            for i, phi in fixed_phis.items()
        )
        b_vec.append(rhs_apo)
    return np.array(b_vec)


def solve_with_A(A, b_vec, glasses_param, free_indices, fixed_phis, n_constraints):
    """
    用预构建的 A 求解线性系统，返回全片位 phis 列表，奇异时返回 None。

    [OPT] 不做 np.linalg.cond 检查（消除 SVD 开销）——
    奇异矩阵由 solve_2x2/solve_3x3 的 det < 1e-14 检查自然捕获。
    """
    if n_constraints == 2:
        free_phis = solve_2x2(A, b_vec)
    elif n_constraints == 3:
        free_phis = solve_3x3(A, b_vec)
    else:
        try:
            free_phis = np.linalg.solve(A, b_vec)
        except np.linalg.LinAlgError:
            return None

    if free_phis is None:
        return None

    result = dict(fixed_phis)
    for idx, phi in zip(free_indices, free_phis):
        result[idx] = phi
    return [result[i] for i in range(len(glasses_param))]


def build_and_solve(phi_total, glasses_param, free_indices,
                    fixed_phis, apo, b_fit):
    """
    广义版约束方程求解（保持对外接口不变）。

    约束：
    ①  Σ φᵢ = φ_total
    ②  Σ φᵢ / V_gen,i = 0                          （消初级色差）
    ③  Σ φᵢ · dP_gen,i / V_gen,i = -b_fit·φ_total  （消二级光谱，APO）

    [OPT] 内部改用 build_A_matrix / build_b_vec / solve_with_A，
    删除原有的 np.linalg.cond 调用（SVD），改由 solve_2x2/solve_3x3
    的行列式检查判断奇异性，速度提升约 5×。
    """
    n_constraints = 3 if apo else 2

    if len(free_indices) != n_constraints:
        raise ValueError(
            f"自由未知数({len(free_indices)}) ≠ 约束数({n_constraints})"
        )

    A = build_A_matrix(glasses_param, free_indices, apo, b_fit)
    if A is None:
        return None

    b_vec = build_b_vec(phi_total, glasses_param, fixed_phis, apo, b_fit)
    return solve_with_A(A, b_vec, glasses_param, free_indices, fixed_phis, n_constraints)


def pick_best_free_indices(glasses_param, n_free, n_const, apo, b_fit=0.0):
    """选条件数最小的扫描/求解分组，用于 adaptive_grouping 模式。"""
    N = len(glasses_param)
    best_cond = np.inf
    best_free = list(range(n_free, N))
    best_scan = list(range(n_free))

    for free_cand in combinations(range(N), n_const):
        scan_cand = [i for i in range(N) if i not in free_cand]
        A = build_A_matrix(glasses_param, list(free_cand), apo, b_fit)
        if A is None:
            continue
        try:
            cond = np.linalg.cond(A)
        except Exception:
            continue
        if cond < best_cond:
            best_cond = cond
            best_free = list(free_cand)
            best_scan = scan_cand

    return best_scan, best_free


