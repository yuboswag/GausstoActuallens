"""
read_seidel_per_config.py
通过 SeidelCoefficients Analysis 在 5 个 config 各读一次赛德尔合计像差系数。
绕开 zemax_bridge.read_seidel() 的 CalculateMeritFunction reset 坑。
"""
import json, os, sys, tempfile, time, uuid, io

ZMX_PATH  = r"D:\myprojects\Action_a\test_zoom_lde_opt.zmx"
JSON_PATH = r"D:\myprojects\Action_a\last_run_config.json"


def _read_seidel_total_via_analysis(bridge, cfg_idx):
    """
    切到 cfg_idx 配置,跑 SeidelCoefficients Analysis,解析"累计"行返回 S1-S5。

    返回 dict: {'S1': float, 'S2': float, 'S3': float, 'S4': float, 'S5': float}

    解析规则:
      1. UTF-16-LE 解码报告
      2. 找 line.strip() == '赛德尔像差系数:' 的行(精确匹配,带冒号),开始进入第一段
      3. 进入第一段后,找 line.lstrip().startswith('累计') 的第一行
      4. line.split('\\t'),strip 后取 parts[1:6] 转 float (S1-S5)
      5. 解析完立即 break,不要管后面的段
    """
    ZOSAPI = bridge._ZOSAPI

    # 切配置 + 等待
    mce = bridge._system.MCE
    mce.SetCurrentConfiguration(cfg_idx)
    for _ in range(20):
        if mce.CurrentConfiguration == cfg_idx:
            break
        time.sleep(0.05)
    time.sleep(0.1)

    # 跑 SeidelCoefficients Analysis
    analysis = bridge._system.Analyses.New_Analysis(
        ZOSAPI.Analysis.AnalysisIDM.SeidelCoefficients)
    analysis.ApplyAndWaitForCompletion()

    # 写入临时文件(用 uuid 避免 GetTextFile 静默失败)
    tmp = os.path.join(tempfile.gettempdir(), f"seidel_{uuid.uuid4().hex}.txt")
    try:
        analysis.GetResults().GetTextFile(tmp)
    finally:
        analysis.Close()
        time.sleep(0.2)

    # 解析
    with io.open(tmp, encoding='utf-16-le') as f:
        lines = f.readlines()
    os.remove(tmp)

    in_first_seidel_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == '赛德尔像差系数:':
            in_first_seidel_section = True
            continue
        if in_first_seidel_section and line.lstrip().startswith('累计'):
            parts = [p.strip() for p in line.split('\t')]
            return {
                'S1': float(parts[1]),
                'S2': float(parts[2]),
                'S3': float(parts[3]),
                'S4': float(parts[4]),
                'S5': float(parts[5]),
            }

    raise RuntimeError(
        f"Config {cfg_idx}: 未在报告中找到第一段的'累计'行")


# ── 读 EFL target 值 ────────────────────────────────────────────
efl_targets = []
try:
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    configs = data.get('principal_plane_correction', {}).get(
        'corrected_zoom_configs', [])
    if not configs:
        configs = data.get('principal_plane_correction', {}).get(
            'raw_zoom_configs', [])
    for c in configs:
        efl_targets.append({
            'name': c.get('name', f"Config {len(efl_targets)+1}"),
            'efl': float(c.get('efl', 0)),
        })
    if len(efl_targets) != 5:
        print(f"[WARN] last_run_config.json 中只找到 {len(efl_targets)} 个 config，期望 5")
except FileNotFoundError:
    print("[FAIL] last_run_config.json 不存在，无法获取 EFL 目标值")
    efl_targets = [{'name': f'Config {i}', 'efl': 0} for i in range(1, 6)]
except Exception as e:
    print(f"[FAIL] 读取 last_run_config.json 失败: {e}")
    efl_targets = [{'name': f'Config {i}', 'efl': 0} for i in range(1, 6)]

# ── 连接 Zemax ───────────────────────────────────────────────────
try:
    from zemax_bridge import ZemaxBridge
except ImportError as e:
    print(f"[FAIL] 导入 ZemaxBridge 失败: {e}")
    sys.exit(1)

bridge = None
try:
    bridge = ZemaxBridge()
    bridge.connect(mode='extension')
    bridge._system.LoadFile(ZMX_PATH, False)
    print(f"已打开: {ZMX_PATH}")
except Exception as e:
    print(f"[FAIL] 连接/打开 Zemax 失败: {e}")
    if bridge:
        bridge.disconnect()
    sys.exit(1)

# ── 主循环：逐 config 读赛德尔合计 ──────────────────────────────
cfg_totals = {}
try:
    for cfg in [1, 2, 3, 4, 5]:
        result = _read_seidel_total_via_analysis(bridge, cfg)
        cfg_totals[cfg] = result
        print(f"  Config {cfg} 读取完成")
except Exception as e:
    print(f"[FAIL] 读取赛德尔系数过程中出错: {e}")
finally:
    if bridge:
        bridge.disconnect()

# ── 打印对比表 ──────────────────────────────────────────────────
print()
print("========== Step 3b: SeidelCoefficients Analysis 5 Config 对比 ==========")
print()

header = (
    f"{'Cfg':>3}  {'EFL_target':>10}  "
    f"{'SPHA(S1)':>12}  {'COMA(S2)':>12}  "
    f"{'ASTI(S3)':>12}  {'FCUR(S4)':>12}  "
    f"{'DIST(S5)':>12}"
)
print(header)
print("  " + "-" * (len(header) - 2))

for cfg in [1, 2, 3, 4, 5]:
    t = cfg_totals.get(cfg)
    if t is None:
        print(f"{cfg:>3}  {'N/A':>10}  {'N/A':>12}  {'N/A':>12}  "
              f"{'N/A':>12}  {'N/A':>12}  {'N/A':>12}")
        continue
    efl = efl_targets[cfg - 1]['efl'] if cfg - 1 < len(efl_targets) else 0
    print(
        f"{cfg:>3}  {efl:>10.3f}  "
        f"{t['S1']:>12.6f}  {t['S2']:>12.6f}  "
        f"{t['S3']:>12.6f}  {t['S4']:>12.6f}  "
        f"{t['S5']:>12.6f}"
    )

# ── 诊断 ────────────────────────────────────────────────────────
print()
print("诊断:")

# Check 1: S_I diversity
si_vals = [cfg_totals[c]['S1'] for c in [1, 2, 3, 4, 5] if c in cfg_totals]
if si_vals:
    si_min, si_max = min(si_vals), max(si_vals)
    si_range = si_max - si_min
    print(f"  [check 1] 5 config 的 S_I 是否各不相同?")
    print(f"            min={si_min:.6f}  max={si_max:.6f}  range={si_range:.6f}")
    if si_range > 0.001:
        print("            ✓ config 切换正常")
    else:
        print("            ⚠ 仍踩 reset 坑")

    # Check 2: |S_I| magnitude
    abs_si_max = max(abs(v) for v in si_vals)
    max_si_cfg = si_vals.index(max(si_vals, key=abs)) + 1
    print(f"  [check 2] |S_I| 最大值 = {abs_si_max:.6f}, 出现在 Cfg {max_si_cfg}")
    if abs_si_max < 0.5:
        print("            评级: 良好")
    elif abs_si_max < 2.0:
        print("            评级: 可优化但有挑战")
    else:
        print("            评级: 结构本身需调整")

    # Check 3: |S_II| max
    sii_vals = [abs(cfg_totals[c]['S2']) for c in [1, 2, 3, 4, 5] if c in cfg_totals]
    abs_sii_max = max(sii_vals)
    max_sii_cfg = [c for c in [1, 2, 3, 4, 5] if c in cfg_totals][
        sii_vals.index(abs_sii_max)]
    print(f"  [check 3] |S_II| 最大值 = {abs_sii_max:.6f}, 出现在 Cfg {max_sii_cfg}")

print()
print("完成")
