"""test_load_zoom_configs.py
验证 load_zoom_configs_for_zemax 能正确从 CSV 构建 ZOOM_CONFIGS。
"""

from zoom_utils import load_zoom_configs_for_zemax

EXPECTED = [
    ("短焦 (Wide)",          12.024, 16.703, 51.484, 28.813,  3.006),
    ("中短焦 (Medium-Wide)", 33.384, 29.289, 31.757, 35.954,  7.587),
    ("中焦 (Medium)",        62.758, 34.089, 20.828, 42.082, 13.075),
    ("中长焦 (Medium-Tele)", 99.473, 36.622, 12.681, 47.698, 19.129),
    ("长焦 (Tele)",         143.158, 38.186,  5.818, 52.996, 25.564),
]

if __name__ == '__main__':
    configs = load_zoom_configs_for_zemax(
        csv_path='111.csv', bfd_mm=8.0,
        fnum_wide=4.0, fnum_tele=5.6,
    )

    print("\n=== 对比验证 ===")
    all_pass = True
    for cfg, exp in zip(configs, EXPECTED):
        checks = [
            abs(cfg[1]-exp[1]) < 0.001,
            abs(cfg[2]-exp[2]) < 0.001,
            abs(cfg[3]-exp[3]) < 0.001,
            abs(cfg[4]-exp[4]) < 0.001,
            abs(cfg[5]-exp[5]) < 0.01,
        ]
        ok = all(checks)
        if not ok:
            all_pass = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {cfg[0]}")
    print()
    print('PASS: 所有数据正确' if all_pass else 'FAIL: 存在偏差')
