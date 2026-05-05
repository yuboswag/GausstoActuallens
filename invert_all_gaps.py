"""
invert_all_gaps.py — 独立反求 d1/d2/d3 的真实偏移量
"""
import json
import numpy as np
from scipy.optimize import brentq

JSON_PATH = r'D:\myprojects\gauss_to_lens\last_run_config.json'
ABCD_SCALE = 0.95  # ABCD vs Zemax 系统偏差


def compute_efl_abcd(surface_prescriptions, d1, d2, d3):
    """按 (y, theta) 约定的 ABCD 追迹——与 analyze_theoretical_efl.py 完全一致。"""
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


def invert_single_gap(surface_prescriptions, d1, d2, d3, which, target_efl,
                      search_lo=0.5, search_hi=200.0):
    """
    固定另外两个间距，反向求解 which in {'d1', 'd2', 'd3'}。
    返回 (求解值, 验证EFL, 是否收敛)；不收敛时返回 (None, None, False)。
    """
    abcd_target = target_efl * ABCD_SCALE

    def f(x):
        if which == 'd1':
            return compute_efl_abcd(surface_prescriptions, x, d2, d3) - abcd_target
        elif which == 'd2':
            return compute_efl_abcd(surface_prescriptions, d1, x, d3) - abcd_target
        elif which == 'd3':
            return compute_efl_abcd(surface_prescriptions, d1, d2, x) - abcd_target

    # 扫描找变号区间
    x_grid = np.linspace(search_lo, search_hi, 400)
    diff_grid = [f(x) for x in x_grid]

    sign_changes = []
    for j in range(len(diff_grid) - 1):
        if diff_grid[j] * diff_grid[j + 1] <= 0:
            sign_changes.append((x_grid[j], x_grid[j + 1]))

    if not sign_changes:
        # 无解：在整个搜索区间 f(x) 同号，说明 EFL 达不到目标
        return None, None, False

    # 用第一个变号区间做 brentq
    x_a, x_b = sign_changes[0]
    try:
        x_sol = brentq(f, x_a, x_b, xtol=1e-6)
        # 验证
        if which == 'd1':
            efl_verify = compute_efl_abcd(surface_prescriptions, x_sol, d2, d3)
        elif which == 'd2':
            efl_verify = compute_efl_abcd(surface_prescriptions, d1, x_sol, d3)
        elif which == 'd3':
            efl_verify = compute_efl_abcd(surface_prescriptions, d1, d2, x_sol)
        return x_sol, efl_verify, True
    except Exception:
        return None, None, False


def main():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    pp = data['principal_plane_correction']
    sp = pp['surface_prescriptions']
    raw_configs = pp['raw_zoom_configs']
    corrected_configs = pp['corrected_zoom_configs']

    # 只处理 Config 1/2/3（Config 4/5 已知不可达）
    reachable_indices = [0, 1, 2]

    print(f"\n{'=' * 120}")
    print("  独立反求 d1/d2/d3 的真实物理值（Config 1/2/3）")
    print(f"{'=' * 120}")

    # ── 反求 d1 ───────────────────────────────────────────
    print(f"\n[d1 反求] 固定 d2/d3 = corrected，搜索 d1 使 ABCD EFL = 目标 x {ABCD_SCALE}")
    print(f"{'配置':<22}  {'目标EFL':>8}  {'raw d1':>9}  {'corr d1':>9}  "
          f"{'真实 d1':>9}  {'raw偏移':>9}  {'corr偏移':>9}")
    print("─" * 115)
    d1_offsets_raw = []
    d1_offsets_corr = []
    for i in reachable_indices:
        raw_d1 = raw_configs[i]['d1']
        corr = corrected_configs[i]
        true_d1, efl_v, ok = invert_single_gap(
            sp, corr['d1'], corr['d2'], corr['d3'],
            which='d1', target_efl=corr['efl']
        )
        if ok:
            off_raw = raw_d1 - true_d1
            off_corr = corr['d1'] - true_d1
            d1_offsets_raw.append(off_raw)
            d1_offsets_corr.append(off_corr)
            print(f"{corr['name']:<22}  {corr['efl']:>8.2f}  {raw_d1:>9.3f}  "
                  f"{corr['d1']:>9.3f}  {true_d1:>9.3f}  "
                  f"{off_raw:>+9.3f}  {off_corr:>+9.3f}")
        else:
            print(f"{corr['name']:<22}  反求失败（EFL 天花板?）")

    # ── 反求 d2 ───────────────────────────────────────────
    print(f"\n[d2 反求] 固定 d1/d3 = corrected，搜索 d2 使 ABCD EFL = 目标 x {ABCD_SCALE}")
    print(f"{'配置':<22}  {'目标EFL':>8}  {'raw d2':>9}  {'corr d2':>9}  "
          f"{'真实 d2':>9}  {'raw偏移':>9}  {'corr偏移':>9}")
    print("─" * 115)
    d2_offsets_raw = []
    d2_offsets_corr = []
    for i in reachable_indices:
        raw_d2 = raw_configs[i]['d2']
        corr = corrected_configs[i]
        true_d2, efl_v, ok = invert_single_gap(
            sp, corr['d1'], corr['d2'], corr['d3'],
            which='d2', target_efl=corr['efl']
        )
        if ok:
            off_raw = raw_d2 - true_d2
            off_corr = corr['d2'] - true_d2
            d2_offsets_raw.append(off_raw)
            d2_offsets_corr.append(off_corr)
            print(f"{corr['name']:<22}  {corr['efl']:>8.2f}  {raw_d2:>9.3f}  "
                  f"{corr['d2']:>9.3f}  {true_d2:>9.3f}  "
                  f"{off_raw:>+9.3f}  {off_corr:>+9.3f}")
        else:
            print(f"{corr['name']:<22}  反求失败（EFL 天花板?）")

    # ── 反求 d3 ───────────────────────────────────────────
    print(f"\n[d3 反求] 固定 d1/d2 = corrected，搜索 d3 使 ABCD EFL = 目标 x {ABCD_SCALE}")
    print(f"{'配置':<22}  {'目标EFL':>8}  {'raw d3':>9}  {'corr d3':>9}  "
          f"{'真实 d3':>9}  {'raw偏移':>9}  {'corr偏移':>9}")
    print("─" * 115)
    d3_offsets_raw = []
    d3_offsets_corr = []
    for i in reachable_indices:
        raw_d3 = raw_configs[i]['d3']
        corr = corrected_configs[i]
        true_d3, efl_v, ok = invert_single_gap(
            sp, corr['d1'], corr['d2'], corr['d3'],
            which='d3', target_efl=corr['efl']
        )
        if ok:
            off_raw = raw_d3 - true_d3
            off_corr = corr['d3'] - true_d3
            d3_offsets_raw.append(off_raw)
            d3_offsets_corr.append(off_corr)
            print(f"{corr['name']:<22}  {corr['efl']:>8.2f}  {raw_d3:>9.3f}  "
                  f"{corr['d3']:>9.3f}  {true_d3:>9.3f}  "
                  f"{off_raw:>+9.3f}  {off_corr:>+9.3f}")
        else:
            print(f"{corr['name']:<22}  反求失败（EFL 天花板?）")

    # ── 偏移量稳定性分析 ──────────────────────────────────
    print(f"\n{'=' * 120}")
    print("  偏移量稳定性分析")
    print(f"{'=' * 120}")
    print(f"{'间距':>6}  {'raw 偏移均值':>13}  {'raw 范围':>18}  {'corr 偏移均值':>13}  {'corr 范围':>18}")
    print("─" * 90)

    def stat(offsets):
        if not offsets:
            return 'N/A', 'N/A', 'N/A'
        arr = np.array(offsets)
        return np.mean(arr), np.min(arr), np.max(arr)

    for label, off_raw, off_corr in [
        ('d1', d1_offsets_raw, d1_offsets_corr),
        ('d2', d2_offsets_raw, d2_offsets_corr),
        ('d3', d3_offsets_raw, d3_offsets_corr),
    ]:
        mean_r, min_r, max_r = stat(off_raw)
        mean_c, min_c, max_c = stat(off_corr)
        if isinstance(mean_r, str):
            print(f"{label:>6}  无数据")
            continue
        range_r = max_r - min_r
        range_c = max_c - min_c
        print(f"{label:>6}  {mean_r:>+13.4f}  [{min_r:>+7.3f}, {max_r:>+7.3f}]  "
              f"{mean_c:>+13.4f}  [{min_c:>+7.3f}, {max_c:>+7.3f}]  "
              f"(span_r={range_r:.3f}, span_c={range_c:.3f})")

    # ── 结论 ─────────────────────────────────────────────
    print(f"\n{'=' * 120}")
    print("  诊断结论")
    print(f"{'=' * 120}")
    print("""
【判断标准】
- 若某间距的 raw/corr 偏移量在 Config 1/2/3 下稳定（跨配置变化 < 1 mm），
  则该偏移可用固定公式修正：corrected = raw - avg_offset
- 若偏移量随配置漂移显著（span > 1 mm），则需要配置相关的修正公式

【预期输出示例】
如果 correct_zoom_spacings 是对的，预期 corr 偏移 ≈ 0；
如果 correct_zoom_spacings 缺一个固定补偿，预期 corr 偏移跨配置稳定不为零。
    """)


if __name__ == '__main__':
    main()
