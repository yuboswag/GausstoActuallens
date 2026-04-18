"""
invert_d2.py — 反向求解：对每个配置，找出让 ABCD 算出目标 EFL 的 d2

策略：固定 d1 和 d3 为 corrected 值，用 brentq 搜索 d2，
     使 compute_efl_abcd(..., d2, ...) = ABCDTarget
     其中 ABCDTarget = 目标 EFL × ABCD_SCALE（考虑 ABCD 与 Zemax 的系统性偏差）
"""
import json
import numpy as np
from scipy.optimize import brentq

JSON_PATH = r'D:\myprojects\Action_a\last_run_config.json'

# ─── 复用 analyze_theoretical_efl.py 里的 ABCD 函数 ─────────
def compute_efl_abcd(surface_prescriptions, d1, d2, d3):
    """按 (y, θ) 约定的 ABCD 追迹——与 analyze_theoretical_efl.py 完全一致。"""
    M = np.eye(2)
    n_prev = 1.0
    gaps = [d1, d2, d3]

    for gi, group in enumerate(surface_prescriptions):
        for surf in group['surfaces']:
            R = surf['R']
            n_next = surf['nd']
            t_after = surf['t']

            if abs(R) > 1e12:
                R_mat = np.eye(2)
            else:
                phi = (n_next - n_prev) / R
                R_mat = np.array([[1.0, 0.0], [-phi, 1.0]])
            M = R_mat @ M

            if t_after > 0:
                T_mat = np.array([[1.0, t_after / n_next], [0.0, 1.0]])
                M = T_mat @ M

            n_prev = n_next

        if gi < 3:
            T_gap = np.array([[1.0, gaps[gi]], [0.0, 1.0]])
            M = T_gap @ M
            n_prev = 1.0

    _, _, C, _ = M[0, 0], M[0, 1], M[1, 0], M[1, 1]
    if abs(C) < 1e-12:
        return float('inf')
    return float(-1.0 / C)
# ─────────────────────────────────────────────────────────────


def main():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    pp = data['principal_plane_correction']
    surface_prescriptions = pp['surface_prescriptions']
    corrected_configs = pp['corrected_zoom_configs']
    raw_configs = pp['raw_zoom_configs']

    # ABCD 追迹与 Zemax 实测存在系统性偏差（约 −5%）
    # 即：ABCD_EFL ≈ 0.95 × Zemax_EFL
    # 因此要让 Zemax 实测命中目标 EFL，ABCD 需要算出 0.95 × 目标
    ABCD_SCALE = 0.95

    names = [c['name'] for c in corrected_configs]
    targets = [c['efl'] for c in corrected_configs]

    print(f"\n{'=' * 106}")
    print("  反向求解：搜索每个配置在 ABCD 追迹下达到目标 EFL 的 d2")
    print(f"{'=' * 106}")
    print(
        f"{'配置':<22}  {'目标EFL':>8}  {'ABCD目标':>9}  {'d1':>7}  {'d3':>7}  "
        f"{'求解d2':>9}  {'验证EFL':>9}  {'相对误差':>9}"
    )
    print("─" * 106)

    solutions = []

    for i, c in enumerate(corrected_configs):
        name = names[i]
        target_efl = targets[i]
        abcd_target = target_efl * ABCD_SCALE
        d1 = c['d1']
        d3 = c['d3']

        def f(d2):
            return compute_efl_abcd(surface_prescriptions, d1, d2, d3) - abcd_target

        # d2 物理合理范围：[0.5, 200] mm
        d2_lo, d2_hi = 0.5, 200.0

        try:
            f_lo, f_hi = f(d2_lo), f(d2_hi)

            # 全局扫描寻找变号区间（单调性可能非全局）
            d2_grid = np.linspace(d2_lo, d2_hi, 300)
            efl_grid = [compute_efl_abcd(surface_prescriptions, d1, d2, d3) for d2 in d2_grid]
            diff_grid = [e - abcd_target for e in efl_grid]

            # 寻找所有变号点
            sign_changes = []
            for j in range(len(diff_grid) - 1):
                if diff_grid[j] * diff_grid[j + 1] <= 0:
                    sign_changes.append((d2_grid[j], d2_grid[j + 1]))

            if sign_changes:
                # 取最左侧有效解（d2 最小时 EFL 达到目标）
                d2_a, d2_b = sign_changes[0]
                d2_sol = brentq(f, d2_a, d2_b, xtol=1e-8)
                efl_verify = compute_efl_abcd(surface_prescriptions, d1, d2_sol, d3)
                err = (efl_verify - abcd_target) / abcd_target * 100
                solutions.append({'d2': d2_sol, 'efl': efl_verify, 'valid': True})
                marker = ""  # 可达
                print(
                    f"{name:<22}  {target_efl:>8.2f}  {abcd_target:>9.3f}  "
                    f"{d1:>7.2f}  {d3:>7.2f}  "
                    f"{d2_sol:>9.3f}  {efl_verify:>9.3f}  {err:>+8.3f}%{marker}"
                )
            else:
                # 无解：取 EFL 最大处
                idx_max = int(np.argmax(efl_grid))
                d2_max = d2_grid[idx_max]
                efl_max = efl_grid[idx_max]
                solutions.append({'d2': d2_max, 'efl': efl_max, 'valid': False})
                err_max = (efl_max - abcd_target) / abcd_target * 100
                marker = "  [EFL上限]"
                print(
                    f"{name:<22}  {target_efl:>8.2f}  {abcd_target:>9.3f}  "
                    f"{d1:>7.2f}  {d3:>7.2f}  "
                    f"{'—':>9}  {efl_max:>9.3f}  {err_max:>+8.3f}%{marker}"
                )
        except Exception as e:
            # 安全打印异常信息（避免 GBK 编码问题）
            err_msg = str(e).encode('ascii', 'replace').decode('ascii')
            print(f"{name:<22}  求解失败: {err_msg}")
            solutions.append({'d2': None, 'efl': None, 'valid': False})

    # ─── 汇总对比 ───────────────────────────────────────────────
    print(f"\n{'=' * 106}")
    print("  汇总：与 corrected d2、Zemax 收敛 d2 对比")
    print(f"{'=' * 106}")
    zemax_converged_d2 = [39.77, 17.96, 7.70, 2.65, 2.00]

    print(
        f"{'配置':<22}  {'目标EFL':>8}  {'求解d2':>9}  "
        f"{'corrected d2':>13}  {'差分Δd2':>9}  {'Zemax d2':>10}  {'ΔvsZemax':>9}"
    )
    print("─" * 106)

    for i in range(len(corrected_configs)):
        sol = solutions[i]
        name = names[i]
        d2_corr = corrected_configs[i]['d2']
        d2_zm = zemax_converged_d2[i]

        if sol['valid']:
            d2_sol = sol['d2']
            delta = d2_sol - d2_corr
            delta_zm = d2_sol - d2_zm
            print(
                f"{name:<22}  {targets[i]:>8.2f}  {d2_sol:>9.3f}  "
                f"{d2_corr:>13.3f}  {delta:>+9.3f}  "
                f"{d2_zm:>10.3f}  {delta_zm:>+9.3f}"
            )
        else:
            print(
                f"{name:<22}  {targets[i]:>8.2f}  {'无法达到':>9}  "
                f"{d2_corr:>13.3f}  {'—':>9}  "
                f"{d2_zm:>10.3f}  {'—':>9}"
            )

    # ─── 解读 ───────────────────────────────────────────────────
    print(f"\n{'=' * 106}")
    print("  解读")
    print(f"{'=' * 106}")
    print("""
【符号约定】
- Δd2 = 求解d2 - corrected d2：若为正，说明当前 correction 的 d2 偏小，需增大才能达到目标
- ΔvsZemax = 求解d2 - Zemax d2：若为正，说明 Zemax 实测 d2 仍需增大才够；若为负则过大

【逻辑链条】
1. 若所有配置均“可解”且 Δd2 与 ΔvsZemax 符号相反 → correct_zoom_spacings
   与 Zemax 的收敛方向不一致（可能是公式方向反了）
2. 若 Δd2 ≈ ΔvsZemax（符号+量级均接近）→ 当前 correct_zoom_spacings
   的方向是对的，但缺一个固定的缩放系数（所有 Δ 同比例）
3. 若“可解”的配置集中在 Wide/Med-W（高 EFL 不可解）→ 长焦端需要
   额外的功率补偿（仅调 d2 不够，需修改曲率或引入非近轴效应）
4. 若全部“不可解”（EFL 天花板远低于目标）→ Gaussianoptics 面处方
   本身在该 d1/d3 固定下无法达到目标，需放宽 d1/d3 约束

【预期参考】（基于上一轮场景 C 的 EFL 偏差 -4%~-5%）
- 因为 ABCD_SCALE = 0.95，求解 d2 ≈ Zemax d2 / 0.95 ≈ Zemax d2 × 1.05
- 预期：求解d2 比 Zemax 收敛 d2 大约 5%（即 ΔvsZemax > 0）
    """)


if __name__ == '__main__':
    main()
