"""
sensitivity_scan.py — G1/G4 曲率缩放灵敏度分析
"""
import json
import copy
import numpy as np

JSON_PATH = r'D:\myprojects\gauss_to_lens\last_run_config.json'


def compute_efl_abcd(surface_prescriptions, d1, d2, d3):
    """完全复用 invert_d2.py 的 ABCD 追迹函数"""
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
    A, B, C, D_ = M[0,0], M[0,1], M[1,0], M[1,1]
    if abs(C) < 1e-12:
        return float('inf')
    return abs(-1.0 / C)


def scale_group_radii(surface_prescriptions, group_idx, factor):
    """对指定组的所有面做曲率半径 × factor 缩放（深拷贝）"""
    sp_new = copy.deepcopy(surface_prescriptions)
    for surf in sp_new[group_idx]['surfaces']:
        if abs(surf['R']) < 1e12:  # 跳过平面
            surf['R'] = surf['R'] * factor
    return sp_new


def find_efl_ceiling(surface_prescriptions, d1, d3, d2_range=(0.5, 100), n_sample=500):
    """扫描 d2，返回 (最大 EFL, 对应 d2)"""
    d2_grid = np.linspace(d2_range[0], d2_range[1], n_sample)
    efls = [compute_efl_abcd(surface_prescriptions, d1, d2, d3) for d2 in d2_grid]
    idx = int(np.argmax(efls))
    return efls[idx], d2_grid[idx]


def main():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    pp = data['principal_plane_correction']
    sp_base = pp['surface_prescriptions']
    corrected = pp['corrected_zoom_configs']

    # Tele 配置的 d1 和 d3（保持 corrected，不变）
    tele = corrected[-1]
    d1_tele = tele['d1']
    d3_tele = tele['d3']
    target_efl = tele['efl']

    # ABCD_SCALE 换算：Zemax 想要 141，ABCD 追迹需达到 141 × 0.95 = 133.95
    target_abcd = target_efl * 0.95

    print(f"{'=' * 90}")
    print(f"  灵敏度分析：G1/G4 曲率缩放对 Tele 端 EFL 天花板的影响")
    print(f"  Tele 目标 EFL = {target_efl:.2f} mm (ABCD 追迹目标 = {target_abcd:.2f} mm)")
    print(f"  固定 d1 = {d1_tele:.2f}, d3 = {d3_tele:.2f}")
    print(f"{'=' * 90}")

    scales = [0.7, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2, 1.3]

    # ── G1 缩放扫描 ───────────────────────────────────────────
    print(f"\n【G1 缩放】(f1 ≈ 62 / scale, 越小 scale → 越长 f1)")
    print(f"{'scale':>8}  {'估算 f1(mm)':>12}  {'Tele EFL天花板':>14}  {'最优 d2':>9}  {'达标':>6}")
    print(f"{'-' * 70}")
    f1_base = 61.8  # 当前 G1 真实焦距
    for s in scales:
        sp_scaled = scale_group_radii(sp_base, group_idx=0, factor=s)
        efl_max, d2_best = find_efl_ceiling(sp_scaled, d1_tele, d3_tele)
        f1_new = f1_base / s
        达标 = '✅' if efl_max >= target_abcd else ''
        print(f"{s:>8.2f}  {f1_new:>12.1f}  {efl_max:>14.2f}  {d2_best:>9.2f}  {达标:>6}")

    # ── G4 缩放扫描 ───────────────────────────────────────────
    print(f"\n【G4 缩放】(f4 ≈ 66 / scale)")
    print(f"{'scale':>8}  {'估算 f4(mm)':>12}  {'Tele EFL天花板':>14}  {'最优 d2':>9}  {'达标':>6}")
    print(f"{'-' * 70}")
    f4_base = 66.4
    for s in scales:
        sp_scaled = scale_group_radii(sp_base, group_idx=3, factor=s)
        efl_max, d2_best = find_efl_ceiling(sp_scaled, d1_tele, d3_tele)
        f4_new = f4_base / s
        达标 = '✅' if efl_max >= target_abcd else ''
        print(f"{s:>8.2f}  {f4_new:>12.1f}  {efl_max:>14.2f}  {d2_best:>9.2f}  {达标:>6}")

    # ── G1 + G4 联合扫描（粗网格）─────────────────────────────
    print(f"\n【G1+G4 联合缩放】(寻找最小代价让 Tele EFL 达标)")
    print(f"{'G1 scale':>10}  {'G4 scale':>10}  {'f1(mm)':>9}  {'f4(mm)':>9}  "
          f"{'Tele EFL天花板':>14}  {'达标':>6}")
    print(f"{'-' * 80}")
    for s1 in [0.75, 0.8, 0.85, 0.9, 0.95, 1.0]:
        for s4 in [0.8, 0.9, 1.0]:
            sp_tmp = scale_group_radii(sp_base, group_idx=0, factor=s1)
            sp_tmp = scale_group_radii(sp_tmp, group_idx=3, factor=s4)
            efl_max, d2_best = find_efl_ceiling(sp_tmp, d1_tele, d3_tele)
            达标 = '✅' if efl_max >= target_abcd else ''
            print(f"{s1:>10.2f}  {s4:>10.2f}  {f1_base/s1:>9.1f}  {f4_base/s4:>9.1f}  "
                  f"{efl_max:>14.2f}  {达标:>6}")


if __name__ == '__main__':
    main()
