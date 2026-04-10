"""
test_bfd_correction.py
验证 G4 后主面偏移计算和 BFD 修正。
"""
from validation import compute_rear_principal_plane
from zoom_utils import load_zoom_configs_for_zemax
import numpy as np

# G4 面序列（直接用 SURFACE_PRESCRIPTION 里 index 19~25 的数据）
# 格式：{'R': float, 'n_in': float, 'n_out': float, 't_after': float}
# 玻璃折射率用 d 线近似值
G4_SEQ = [
    {'R':  814.647, 'n_in': 1.0,    'n_out': 1.4388, 't_after': 1.5},   # G4-L1前  H-FK95N
    {'R': -522.478, 'n_in': 1.4388, 'n_out': 1.9570, 't_after': 1.5},   # G4-L1/2胶 H-ZF88
    {'R':-2111.918, 'n_in': 1.9570, 'n_out': 1.0,    't_after': 4.0},   # G4-L2后
    {'R':   22.586, 'n_in': 1.0,    'n_out': 1.6253, 't_after': 1.64},  # G4-L3前  H-ZK21
    {'R': -162.500, 'n_in': 1.6253, 'n_out': 1.0,    't_after': 4.0},   # G4-L3后
    {'R':  -24.189, 'n_in': 1.0,    'n_out': 1.6237, 't_after': 1.5},   # G4-L4前  H-F4
    {'R':  -77.618, 'n_in': 1.6237, 'n_out': 1.0,    't_after': 0.0},   # G4-L4后
]

if __name__ == '__main__':
    print("=== G4 后主面偏移计算 ===")
    delta_H, g4_efl = compute_rear_principal_plane(G4_SEQ)
    bfd_gaussian = 8.0
    bfd_physical = bfd_gaussian - delta_H

    print(f"G4 等效焦距 EFL = {g4_efl:.4f} mm  （参考值 72.545 mm）")
    print(f"δH'（后主面偏移）= {delta_H:+.4f} mm")
    print(f"高斯 BFD         = {bfd_gaussian:.4f} mm")
    print(f"物理 BFD         = {bfd_physical:.4f} mm")

    print("\n=== 从 CSV 构建 ZOOM_CONFIGS ===")
    zoom_configs, bfd_phys, dH = load_zoom_configs_for_zemax(
        csv_path     = '111.csv',
        g4_seq       = G4_SEQ,
        bfd_gaussian = 8.0,
        fnum_wide    = 4.0,
        fnum_tele    = 5.6,
    )

    print(f"\n{'位置':<20} {'EFL':>8} {'d1':>8} {'d2':>8} {'d3':>8} {'EPD':>8}")
    print("-" * 62)
    for row in zoom_configs:
        print(f"{row[0]:<20} {row[1]:>8.3f} {row[2]:>8.3f} {row[3]:>8.3f} {row[4]:>8.3f} {row[5]:>8.3f}")

    print(f"\n物理 BFD（传给 write_zoom_system 的 bfd_mm）= {bfd_phys:.4f} mm")
