"""
analyze_theoretical_efl.py — 用 ABCD 矩阵追迹精确计算 26 面系统的 EFL
"""
import json
import numpy as np

JSON_PATH = r'D:\myprojects\gauss_to_lens\last_run_config.json'


def compute_efl_abcd(surface_prescriptions, d1, d2, d3):
    """
    按 ABCD 矩阵追迹 26 个面 + 3 个组间间距，返回 EFL (mm)。

    surface_prescriptions: 来自 JSON 的 4 组面处方列表
    d1, d2, d3: G1-G2, G2-G3, G3-G4 组间物理间距（mm，空气）

    组内面的厚度取自 surf['t']（最后一面 t=0，表示组终止）
    组间间距由参数 d1/d2/d3 提供

    坐标约定:
    - ABCD 矩阵为 (A B; C D) 2×2 形式
    - 光线向量 [y, θ]^T，y 为高，θ 为角度
    - 折射面: [1, 0; -φ, 1], φ = (n_next - n_prev) / R
    - 平移: [1, t/n; 0, 1], n 为当前介质折射率（面后介质）
    - 系统 EFL = -1 / C_44 (即 -1/M[1,1])
    """
    M = np.eye(2)
    n_prev = 1.0  # 第一个面前是空气
    gaps = [d1, d2, d3]

    for gi, group in enumerate(surface_prescriptions):
        surfaces = group['surfaces']
        for i, surf in enumerate(surfaces):
            R = surf['R']
            nd_after = surf['nd']
            t_after = surf['t']
            n_next = nd_after

            # 1. 折射面矩阵
            # R 为无穷大（平面）时，phi = 0
            if abs(R) > 1e12:
                R_mat = np.eye(2)
            else:
                phi = (n_next - n_prev) / R
                R_mat = np.array([[1.0, 0.0], [-phi, 1.0]])
            M = R_mat @ M

            # 2. 面后介质厚度（最后一面 t=0 跳过）
            if t_after > 0:
                T_mat = np.array([[1.0, t_after / n_next], [0.0, 1.0]])
                M = T_mat @ M

            n_prev = n_next

        # 3. 组间空气间距（除最后一组外）
        if gi < 3:
            T_gap = np.array([[1.0, gaps[gi]], [0.0, 1.0]])
            M = T_gap @ M
            n_prev = 1.0  # 组间是空气

    A, B, C, D = M[0, 0], M[0, 1], M[1, 0], M[1, 1]

    # 系统后焦距 BFD = -B / D  (光线从最后一面出发，θ=0，打到焦面时 y=0)
    # 系统前焦距 BFD_front = B / C
    # EFL（有效焦距）= -1/C（前提是 D=1 的近轴系统，或去焦平移使 D=1）
    # 广义罗括号定理：任意 ABCD 系统的有效焦距 = -1 / C（当系统已归一化至 D=1）
    # 对折射系统 C ≠ 0 时有效，EFL = -1/C
    if abs(C) < 1e-12:
        return float('inf')
    return float(-1.0 / C)


def main():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    pp = data['principal_plane_correction']
    surface_prescriptions = pp['surface_prescriptions']
    raw_configs = pp['raw_zoom_configs']
    corrected_configs = pp['corrected_zoom_configs']

    names = [c['name'] for c in raw_configs]
    targets = [c['efl'] for c in raw_configs]

    # ─────────────────────────────────────────────────────────────
    # 参考数据
    #
    # 场景 A/B 对照：Zemax iteration 0（直接用 corrected_zoom_configs 写进
    #                 LDE 后，未做任何优化计算的原始读取值）
    #                 ← 就是 corrected_zoom_configs 的 efl 字段自身
    zemax_initial = [c['efl'] for c in corrected_configs]
    #
    # 场景 C 对照：Zemax 迭代 15 轮后收敛的 d2（d1, d3 保持 corrected
    #             不变），以及对应读取的 EFL 值
    #              ← 来自用户提供的历史数据
    Zemax_converged_d2 = [39.77, 17.96, 7.70, 2.65, 2.00]
    Zemax_converged_efl = [11.897, 33.069, 60.204, 85.326, 97.356]
    # ─────────────────────────────────────────────────────────────

    # ── 表头 ────────────────────────────────────────────────────
    print(f"\n{'配置':<22}  {'目标EFL':>8}  {'Zemax初始':>10}")
    print("─" * 50)
    for i in range(5):
        print(f"{names[i]:<22}  {targets[i]:>8.2f}  {zemax_initial[i]:>10.3f}")

    # ── 场景 A：使用 raw d1/d2/d3 追迹 ───────────────────────────
    print(f"\n{'=' * 85}")
    print("  场景 A：ABCD 追迹 — 使用 Gaussianoptics raw d1/d2/d3")
    print(f"{'=' * 85}")
    header = f"{'配置':<22}  {'目标EFL':>8}  {'raw d1':>7}  {'raw d2':>7}  {'raw d3':>7}  {'算得EFL':>10}  {'相对误差':>9}"
    print(header)
    print("─" * len(header))
    for i in range(5):
        c = raw_configs[i]
        efl_theo = compute_efl_abcd(
            surface_prescriptions,
            c['d1'], c['d2'], c['d3']
        )
        err = (efl_theo - targets[i]) / targets[i] * 100
        print(
            f"{names[i]:<22}  {targets[i]:>8.2f}  "
            f"{c['d1']:>7.2f}  {c['d2']:>7.2f}  {c['d3']:>7.2f}  "
            f"{efl_theo:>10.3f}  {err:>+8.2f}%"
        )

    # ── 场景 B：使用 corrected d1/d2/d3 追迹 ─────────────────────
    print(f"\n{'=' * 85}")
    print("  场景 B：ABCD 追迹 — 使用 correct_zoom_spacings 输出 d1/d2/d3")
    print(f"{'=' * 85}")
    header = f"{'配置':<22}  {'目标EFL':>8}  {'corr d1':>7}  {'corr d2':>7}  {'corr d3':>7}  {'算得EFL':>10}  {'相对误差':>9}  {'Zemax初始':>10}"
    print(header)
    print("─" * len(header))
    for i in range(5):
        c = corrected_configs[i]
        efl_theo = compute_efl_abcd(
            surface_prescriptions,
            c['d1'], c['d2'], c['d3']
        )
        err = (efl_theo - targets[i]) / targets[i] * 100
        print(
            f"{names[i]:<22}  {targets[i]:>8.2f}  "
            f"{c['d1']:>7.2f}  {c['d2']:>7.2f}  {c['d3']:>7.2f}  "
            f"{efl_theo:>10.3f}  {err:>+8.2f}%  "
            f"{zemax_initial[i]:>10.3f}"
        )

    # ── 场景 C：使用 Zemax 迭代收敛后的 d2 追迹 ─────────────────
    print(f"\n{'=' * 85}")
    print("  场景 C：ABCD 追迹 — d1/d3 保持 corrected，d2 使用 Zemax 收敛值")
    print(f"{'=' * 85}")
    header = (
        f"{'配置':<22}  {'目标EFL':>8}  {'conv d2':>7}  "
        f"{'算得EFL':>10}  {'Zemax收敛EFL':>13}  {'差异':>9}"
    )
    print(header)
    print("─" * len(header))
    ef_label_width = 10
    for i in range(5):
        c = corrected_configs[i]
        d2_zm = Zemax_converged_d2[i]
        efl_theo = compute_efl_abcd(
            surface_prescriptions,
            c['d1'], d2_zm, c['d3']
        )
        efl_zm = Zemax_converged_efl[i]
        diff = (efl_theo - efl_zm) / efl_zm * 100
        print(
            f"{names[i]:<22}  {targets[i]:>8.2f}  "
            f"{d2_zm:>7.2f}  "
            f"{efl_theo:>10.3f}  {efl_zm:>13.3f}  {diff:>+8.2f}%"
        )

    # ── 解读 ─────────────────────────────────────────────────────
    print(f"\n{'=' * 85}")
    print("  解读 & 验证结论")
    print(f"{'=' * 85}")
    print("""
【关键判据】

1. 场景 C（ABCD vs Zemax 收敛值）是最核心的对照：
   - 如果 |差异| < 2%  → d2 写入完全正确（zemax_bridge.d2 映射无误）
   - 如果 ABCD 结果接近目标 EFL，但 Zemax 显著偏小  → d2 写入有 bug
   - 如果两者都很接近（<5%）但均显著小于目标  → Gaussianoptics 解本身就有局限

2. 场景 A vs 场景 B（raw vs corrected d1/d2/d3）揭示 correct_zoom_spacings
   的修正是否正确：
   - 若 raw 下 ABCD 结果接近目标 → 当前 correction 是多余的（过度修正）
   - 若 corr 下 ABCD 结果接近目标 → correction 正确，但 Zemax 不响应（bridge 问题）
   - 若两者均偏差大 → 需要新的修正公式

3. 场景 C 的 d2 差量 Δd2 = Zemax_d2 - corrected_d2 是可操作的修正量：
   - d2_corrected + Δd2 ≈ Zemax_read_d2  → 这个 Δd2 就是下一步的补偿修正

【预期结果指引】（基于历史数据）
- Config 1: 目标 11.9 mm, Zemax read ≈ 11.9 mm  → 应完全吻合
- Config 2: 目标 33.2 mm, Zemax read ≈ 33.1 mm  → 高度吻合
- Config 3: 目标 62.3 mm, Zemax read ≈ 60.2 mm  → 偏小 3.3%
- Config 4: 目标 98.3 mm, Zemax read ≈ 85.3 mm  → 偏小 13%
- Config 5: 目标 141.0 mm, Zemax read ≈ 97.4 mm  → 偏小 31%

若 ABCD 追迹结果普遍大于 Zemax 值（尤其是 Tele 端），
说明 Zemax 写入的 d2 值偏小（不足）导致功率不足；
反之则是 d2 偏大。

【坐标约定确认】
本脚本使用：
- 光线向量 (y, θ)^T
- 折射矩阵 R = [1, 0; -(n'-n)/R, 1]
- 厚度矩阵 T = [1, t/n; 0, 1]，t 为面后介质厚度，n 为该介质折射率
- 全链路 M = R_n @ T_n @ ... @ R_2 @ T_2 @ R_1 @ T_1
- EFL = -1 / C_22（即 M[1,1]，C 元素下同）

此约定与 Gaussianoptics 使用的 (E, u)^T = (y, nθ)^T 约定不同：
后者光线向量为 (y, nθ)^T，平移矩阵为 [1, t; 0, 1]，不除以 n。
若发现系统性偏差（所有 EFL 差一个固定比例），注意检查此约定差异。
    """)


if __name__ == '__main__':
    main()
