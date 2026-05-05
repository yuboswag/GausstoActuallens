"""
独立的几何校验脚本，不连接 Zemax。
直接读 last_run_config.json，跑 validate_geometry 看报告。
"""
import os
import json
from validate_geometry import validate_geometry, print_geometry_report
from zoom_utils import correct_zoom_spacings

# ── 1. 加载 last_run_config.json ─────────────────────────
CONFIG_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'last_run_config.json')

with open(CONFIG_JSON, 'r', encoding='utf-8') as f:
    data = json.load(f)

pp_data = data['principal_plane_correction']

# ── 2. 构建 SURFACE_PRESCRIPTION（复用 test_bridge.py 的逻辑）──
prescription = []
global_idx = 0
for group_data in pp_data['surface_prescriptions']:
    group_name = group_data['group']
    for surf in group_data['surfaces']:
        prescription.append((
            global_idx,
            f"{group_name}-{surf['desc']}",
            surf['R'],
            surf['nd'],
            surf['t'],
            surf['glass'],
        ))
        global_idx += 1

print(f"加载 {len(prescription)} 个面")

# ── 3. 构建 ZOOM_CONFIGS ────────────────────────────────
raw_cfgs = pp_data['raw_zoom_configs']
raw_tuples = [(c['name'], c['efl'], c['d1'], c['d2'], c['d3'], c['epd'])
              for c in raw_cfgs]

# 主面修正（NOOP，按 test_bridge.py 的做法保留调用以保持一致）
group_pp = [(float(pp['delta_H']), float(pp['delta_Hp']))
            for pp in pp_data['group_principal_planes']]
zoom_configs = correct_zoom_spacings(raw_tuples, group_pp)

print(f"加载 {len(zoom_configs)} 个 zoom configs")
for cfg in zoom_configs:
    print(f"  {cfg[0]:5s} EFL={cfg[1]:7.3f}  "
          f"d1={cfg[2]:6.2f}  d2={cfg[3]:6.2f}  d3={cfg[4]:6.2f}  "
          f"EPD={cfg[5]:6.3f}")

# ── 4. 跑几何校验 ────────────────────────────────────────
# 用最大 EPD × 1.2 作为代表口径（粗估，对小镜片偏保守）
max_epd = max(cfg[5] for cfg in zoom_configs)
D_estimate = max_epd * 1.2
print(f"\n使用代表口径 D = {D_estimate:.2f} mm （max(EPD)={max_epd:.2f} × 1.2）")
print("注意：这是粗估值，对 G3/G4 的小镜片可能 over-report；")
print("      重点关注 [gap_edge] 类别的 FAIL（受 D 影响最小）")

reports = validate_geometry(
    surface_prescription=prescription,
    zoom_configs=zoom_configs,
    D_mm=D_estimate,
    variable_thickness_idx=[6, 13, 18],  # d1/d2/d3 在 prescription 中的 0-based idx
)
print_geometry_report(reports)