"""
read_optimized_spacings.py

用途：读 test_zoom_lde_opt.zmx 中 15 个 THIC（3 行 × 5 config），
与 111.csv 的 raw d1/d2/d3 对比，打印 diff 报告。

依赖：pythonnet + Zemax OpticStudio（extension 模式）
"""

import json
import csv
import os
import sys

from zemax_bridge import ZemaxBridge


def main():
    # ── 1. 路径 ──────────────────────────────────────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__))
    zmx_path = os.path.join(script_dir, 'test_zoom_lde_opt.zmx')
    config_path = os.path.join(script_dir, 'last_run_config.json')
    csv_path = os.path.join(script_dir, '111.csv')

    if not os.path.exists(zmx_path):
        print(f"[FAIL] 找不到 test_zoom_lde_opt.zmx")
        return

    # ── 2. 从 last_run_config.json 读 efl 目标 ─────────────────────
    efl_targets = [None] * 5
    try:
        with open(config_path, 'r', encoding='utf-8-sig') as f:
            cfg_data = json.load(f)
        raw_cfgs = cfg_data.get('principal_plane_correction', {}).get(
            'raw_zoom_configs', [])
        efl_targets = [c['efl'] for c in raw_cfgs]
        config_names = [c['name'] for c in raw_cfgs]
    except Exception as e:
        print(f"[FAIL] 读取 last_run_config.json 失败: {e}")
        return

    if len(efl_targets) != 5:
        print(f"[FAIL] last_run_config.json 中 efl 数量不为 5: {len(efl_targets)}")
        return

    # ── 3. 从 CSV 读 raw d1/d2/d3（共 15 个值） ───────────────────
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            filtered = (ln for ln in f if not ln.lstrip().startswith('#'))
            reader = csv.DictReader(filtered)
            csv_rows = list(reader)
    except Exception as e:
        print(f"[FAIL] 读取 CSV 文件失败: {e}")
        return

    required_cols = ['d1 (G1-G2间距) (mm)',
                     'd2 (G2-G3间距) (mm)',
                     'd3 (G3-G4间距) (mm)']
    for col in required_cols:
        if col not in csv_rows[0]:
            print(f"[FAIL] CSV 缺少列: {col}")
            return

    raw_d = {'d1': [], 'd2': [], 'd3': []}
    for row in csv_rows:
        raw_d['d1'].append(float(row['d1 (G1-G2间距) (mm)']))
        raw_d['d2'].append(float(row['d2 (G2-G3间距) (mm)']))
        raw_d['d3'].append(float(row['d3 (G3-G4间距) (mm)']))

    # ── 4. 连接 Zemax + 读 MCE THIC ──────────────────────────────
    bridge = ZemaxBridge()
    try:
        bridge.connect(mode='extension')

        # 打开目标文件
        bridge._system.LoadFile(zmx_path, False)

        # 用 diagnose_system_validity 读 MCE
        diag = bridge.diagnose_system_validity()
        mce_summary = diag['mce_summary']

        # 从 mce_summary 过滤出 THIC 行（行 1/2/3，对应 Surface 7/14/19）
        thic_rows = [op for op in mce_summary if op['type'] == 'THIC']
        if len(thic_rows) < 3:
            print(f"[FAIL] MCE 中未找到 3 行 THIC，找到 {len(thic_rows)} 行")
            return

        # 按 MCE 行号（而非 surface 号）取前 3 行
        # 行 1 → d1 (Surface 7), 行 2 → d2 (Surface 14), 行 3 → d3 (Surface 19)
        opt_d = {
            'd1': [thic_rows[0]['values'][i] for i in range(5)],
            'd2': [thic_rows[1]['values'][i] for i in range(5)],
            'd3': [thic_rows[2]['values'][i] for i in range(5)],
        }

        # ── 5. 打印对比表 ──────────────────────────────────────────
        print()
        print("========== Step 2: DLS 间距 diff 报告 ==========")
        print()

        deltas_pct_all = []

        for ci in range(5):
            name = config_names[ci]
            efl_t = efl_targets[ci]
            # 短/长焦标注
            if ci == 0:
                tag = 'Wide'
            elif ci == 4:
                tag = 'Tele'
            else:
                tag = ''
            # 短标注显示简短名称
            short_name = name.replace('短焦 (', '').replace(')', '')
            print(f"Cfg{ci+1} ({short_name},    EFL_target={efl_t}):")

            for di, dkey in enumerate(['d1', 'd2', 'd3'], start=1):
                raw_val = raw_d[dkey][ci]
                opt_val = opt_d[dkey][ci]
                if opt_val is None:
                    print(f"  d{di}: raw={raw_val}  opt=N/A  Δ=N/A  Δ%=N/A")
                    continue
                delta = opt_val - raw_val
                delta_pct = (delta / raw_val * 100) if abs(raw_val) > 1e-12 else 0.0
                deltas_pct_all.append(abs(delta_pct))
                sign = '+' if delta >= 0 else ''
                sign_p = '+' if delta_pct >= 0 else ''
                # 小数位：d1/d3 固定 3 位小数，d2 因数值较大用 3 位
                print(f"  d{di}: raw={raw_val:7.3f}  opt={opt_val:7.3f}"
                      f"  Δ={sign}{delta:.3f}  Δ%={sign_p}{delta_pct:.1f}%")

        print()
        max_dp = max(deltas_pct_all) if deltas_pct_all else 0.0
        avg_dp = sum(deltas_pct_all) / len(deltas_pct_all) if deltas_pct_all else 0.0
        # 找最大 |Δ%| 所在位置
        max_idx = deltas_pct_all.index(max_dp) if deltas_pct_all else -1
        if max_idx >= 0:
            max_cfg_i = max_idx // 3
            max_di   = (max_idx % 3) + 1
            dkey_max = f'd{max_di}'
            print("总结:")
            print(f"  最大 |Δ%| = {max_dp:.1f}%  (Cfg{max_cfg_i+1}, d{max_di}, "
                  f"raw={raw_d[dkey_max][max_cfg_i]:.3f} → opt={opt_d[dkey_max][max_cfg_i]:.3f})")
        print(f"  平均 |Δ%| = {avg_dp:.1f}%")

    except Exception as e:
        print(f"[FAIL] Zemax 连接失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        bridge.disconnect()


if __name__ == '__main__':
    main()
