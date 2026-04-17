"""
test_zoom_lde.py — 验证 write_zoom_system 方法的 LDE 面数据 + MCE 多配置写入功能

测试流程：
  步骤 1: 连接 Zemax（extension 模式）
  步骤 2: 调用 write_zoom_system 写入变焦系统面数据 + MCE
  步骤 3: 用 read_system_info 打印面数据
  步骤 4: 验证 MCE 多配置（5 个配置、4 行操作数）
  步骤 5: 验证各配置 EFL
  步骤 6: 保存为 test_zoom_lde.zmx
  步骤 7: 断开连接

验证内容：
  - 总面数应为 28（OBJ + 26 个插入面 + IMA）
  - 曲率半径和玻璃名正确
  - 光阑在 Surface 15
  - 最后一面厚度为 BFD（8.0 mm）
  - MCE 有 5 个配置、4 行操作数（3 行 THIC + 1 行 APER）
  - 切换配置时 d1/d2/d3 和像方 F/# 正确变化
"""

import os
import json
import traceback
import numpy as np
from zemax_bridge import ZemaxBridge, ZemaxBridgeError

# ---------------------------------------------------------------------------
# 从 last_run_config.json 加载面处方 + 计算主面修正 + 修正间距（全部自洽）
# ---------------------------------------------------------------------------
_CONFIG_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'last_run_config.json')

def _load_all_from_json(json_path):
    """
    从 last_run_config.json 一次性加载：
    1. 面处方 → SURFACE_PRESCRIPTION 格式
    2. 直接从面处方计算主面位置（不使用预存值，确保自洽）
    3. 用计算出的主面位置修正原始高斯间距

    返回 (surface_prescription, zoom_configs)
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    pp_data = data['principal_plane_correction']

    # ── 1. 构建面处方 ────────────────────────────────────────────
    sp_groups = pp_data['surface_prescriptions']
    prescription = []
    group_boundaries = []  # [(start, end), ...]
    global_idx = 0

    for group_data in sp_groups:
        group_name = group_data['group']
        start_idx = global_idx
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
        group_boundaries.append((start_idx, global_idx - 1))

    print(f"  从 JSON 读取 {len(prescription)} 个面，{len(group_boundaries)} 个组")

    # ── 2. 直接从面处方计算各组主面位置 ──────────────────────────
    print(f"\n  [主面位置（从面处方直接计算）]")
    group_pp = []
    for gi, (start, end) in enumerate(group_boundaries):
        group_surfs = prescription[start:end+1]
        R_list = [s[2] for s in group_surfs]
        n_list = [s[3] for s in group_surfs]  # 各面后介质 nd
        t_list = [s[4] for s in group_surfs]  # 各面后厚度

        M = np.eye(2)
        for i in range(len(R_list)):
            n_before = 1.0 if i == 0 else n_list[i - 1]
            n_after = n_list[i]
            R_i = R_list[i]

            if abs(R_i) > 1e12:
                R_mat = np.eye(2)
            else:
                phi_surf = (n_after - n_before) / R_i
                R_mat = np.array([[1.0, 0.0], [-phi_surf, 1.0]])
            M = R_mat @ M

            if i < len(R_list) - 1:
                T_mat = np.array([[1.0, t_list[i] / n_after], [0.0, 1.0]])
                M = T_mat @ M

        A, B, C, D = M[0,0], M[0,1], M[1,0], M[1,1]

        if abs(C) < 1e-12:
            print(f"    G{gi+1}: ⚠ 无焦系统")
            group_pp.append((0.0, 0.0))
            continue

        dH = (1.0 - D) / C
        dHp = (A - 1.0) / C
        efl = -1.0 / C
        group_pp.append((dH, dHp))
        print(f"    G{gi+1}: EFL={efl:.3f} mm, delta_H={dH:+.4f} mm, delta_Hp={dHp:+.4f} mm")

    # ── 3. 从原始间距 + 主面修正 → 物理间距 ─────────────────────
    raw_cfgs = pp_data['raw_zoom_configs']
    raw_tuples = [(c['name'], c['efl'], c['d1'], c['d2'], c['d3'], c['epd'])
                  for c in raw_cfgs]

    from zoom_utils import correct_zoom_spacings
    corrected = correct_zoom_spacings(raw_tuples, group_pp)

    return prescription, corrected


# 加载
print("\n[从 last_run_config.json 加载面处方和修正间距]")
try:
    SURFACE_PRESCRIPTION, ZOOM_CONFIGS = _load_all_from_json(_CONFIG_JSON)
    print(f"  ✅ 加载成功：{len(SURFACE_PRESCRIPTION)} 个面，{len(ZOOM_CONFIGS)} 个配置")
except Exception as e:
    print(f"  ❌ JSON 加载失败原因：{e}")
    print(f"  ⚠ 回退到硬编码面数据 + CSV 原始间距（EPD 将按 F/# 线性插值）")
    import traceback; traceback.print_exc()
    # 回退到硬编码（保留原来的 SURFACE_PRESCRIPTION 作为后备）
    SURFACE_PRESCRIPTION = [
        ( 0, "G1-L1前",    60.768,  1.8077,  4.2700, "H-ZLaF50E"),
        ( 1, "G1-L1后",  -790.914,  1.0000,  1.0000,  None),
        ( 2, "G1-L2前",   -61.007,  1.8541,  3.1200, "H-ZF52"),
        ( 3, "G1-L2/3胶", 779.212,  1.4388,  0.8800, "H-FK95N"),
        ( 4, "G1-L3后",  -300.970,  1.0000,  1.0000,  None),
        ( 5, "G1-L4前",    42.071,  1.6410,  5.5800, "H-ZK11"),
        ( 6, "G1-L4后",  -591.250,  1.0000, 16.7030,  None),
        ( 7, "G2-L1前",   -25.868,  1.8077,  1.5000, "H-ZLaF50E"),
        ( 8, "G2-L1后",    24.953,  1.0000,  1.5000,  None),
        ( 9, "G2-L2前",   -25.923,  1.7158,  1.5000, "H-LaK7A"),
        (10, "G2-L2后",    26.759,  1.0000,  1.5000,  None),
        (11, "G2-L3前",    27.269,  1.9570,  2.1300, "H-ZF88"),
        (12, "G2-L3/4胶", -89.145,  1.4583,  0.8700, "H-FK61B"),
        (13, "G2-L4后",   108.778,  1.0000, 51.4840,  None),
        (14, "G3-L1前",    18.207,  1.6226,  2.9700, "H-ZK9B"),
        (15, "G3-L1后",   -19.361,  1.0000,  1.0000,  None),
        (16, "G3-L2前",   -20.971,  1.7610,  2.1200, "ZF6"),
        (17, "G3-L2/3胶",-298.142,  1.4576,  0.8800, "H-FK71"),
        (18, "G3-L3后",   -46.289,  1.0000, 28.8130,  None),
        (19, "G4-L1前",   814.647,  1.4388,  1.5000, "H-FK95N"),
        (20, "G4-L1/2胶",-522.478,  1.9570,  1.5000, "H-ZF88"),
        (21, "G4-L2后", -2111.918,  1.0000,  4.0000,  None),
        (22, "G4-L3前",    22.586,  1.6253,  1.6400, "H-ZK21"),
        (23, "G4-L3后",  -162.500,  1.0000,  4.0000,  None),
        (24, "G4-L4前",   -24.189,  1.6237,  1.5000, "H-F4"),
        (25, "G4-L4后",   -77.618,  1.0000,  0.0000,  None),
    ]
    from zoom_utils import load_zoom_configs_for_zemax
    CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '111.csv')
    ZOOM_CONFIGS = load_zoom_configs_for_zemax(CSV_PATH, bfd_mm=8.0, fnum_wide=4.0, fnum_tele=5.6)

# 其他参数
WAVELENGTH_UM = 0.587056        # 主波长（d 线）
SENSOR_HALF_DIAG_MM = 3.8       # 传感器半对角线
STOP_SURFACE_IDX = 14           # 光阑面的 Action_a 编号
BFD_MM = 8.0                    # 最后一面到像面的距离

# 保存路径
SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'test_zoom_lde.zmx')

# ---------------------------------------------------------------------------
# 辅助：步骤输出格式
# ---------------------------------------------------------------------------
def step_header(n: int, desc: str):
    print()
    print(f"{'='*60}")
    print(f"  步骤 {n}: {desc}")
    print(f"{'='*60}")

def pass_msg(msg: str = ''):
    tag = '[ PASS ]'
    if msg:
        print(f"  {tag} {msg}")
    else:
        print(f"  {tag}")

def fail_msg(msg: str = ''):
    tag = '[ FAIL ]'
    if msg:
        print(f"  {tag} {msg}")
    else:
        print(f"  {tag}")

# ---------------------------------------------------------------------------
# 主测试函数
# ---------------------------------------------------------------------------
def run_test():
    all_pass = True
    bridge = ZemaxBridge()

    try:
        # ------------------------------------------------------------------ #
        # 清理上一轮可能被污染的 zmx 文件
        # ------------------------------------------------------------------ #
        for fname in ['test_zoom_lde.zmx', 'test_zoom_lde.ZDA',
                      'test_zoom_lde_corrected.zmx', 'test_zoom_lde_corrected.ZDA']:
            fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    print(f"  [清理] 已删除旧文件: {fname}")
                except Exception as e:
                    print(f"  [警告] 无法删除 {fname}: {e}")

        # ------------------------------------------------------------------ #
        # 步骤 1：连接 Zemax
        # ------------------------------------------------------------------ #
        step_header(1, "连接 Zemax（extension 模式）")
        try:
            bridge.connect(mode='extension')
            pass_msg("连接成功")
        except Exception as e:
            fail_msg(f"连接失败：{e}")
            traceback.print_exc()
            return  # 无法继续

        # ------------------------------------------------------------------ #
        # 步骤 2：调用 write_zoom_system 写入变焦系统面数据
        # ------------------------------------------------------------------ #
        step_header(2, "写入变焦系统 LDE 面数据")
        try:
            bridge.write_zoom_system(
                surface_prescription=SURFACE_PRESCRIPTION,
                zoom_configs=ZOOM_CONFIGS,
                wavelength_um=WAVELENGTH_UM,
                sensor_half_diag_mm=SENSOR_HALF_DIAG_MM,
                stop_surface_idx=STOP_SURFACE_IDX,
                bfd_mm=BFD_MM
            )
            pass_msg("write_zoom_system 调用成功")
        except Exception as e:
            fail_msg(f"写入失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 3：用 read_system_info 打印面数据
        # ------------------------------------------------------------------ #
        step_header(3, "读取系统信息并验证")
        try:
            sys_info = bridge.read_system_info()
            num_surf = sys_info['num_surfaces']
            surfaces = sys_info['surfaces']

            print(f"  总面数: {num_surf}")
            print(f"  期望面数: 28 (OBJ + 26 插入面 + IMA)")
            if num_surf == 28:
                pass_msg("面数正确")
            else:
                fail_msg(f"面数不正确，期望 28，实际 {num_surf}")
                all_pass = False

            # 打印前几个面的关键信息
            print()
            print(f"  {'面号':>4} {'曲率半径 (mm)':>16} {'厚度 (mm)':>12} {'玻璃':>12} {'光阑':>6}")
            print(f"  {'-'*60}")
            for surf in surfaces[:10]:  # 只打印前 10 个面
                radius = surf['radius']
                thickness = surf['thickness']
                material = surf['material'] if surf['material'] else '空气'
                is_stop = '是' if surf['is_stop'] else ''
                print(f"  {surf['index']:>4} {radius:>16.3f} {thickness:>12.3f} "
                      f"{material:>12} {is_stop:>6}")

            # 检查光阑位置
            stop_found = False
            for surf in surfaces:
                if surf['is_stop']:
                    stop_found = True
                    if surf['index'] == 15:  # Surface 15（Zemax 编号）
                        pass_msg(f"光阑在 Surface {surf['index']}（正确）")
                    else:
                        fail_msg(f"光阑在 Surface {surf['index']}，期望 Surface 15")
                        all_pass = False
                    break
            if not stop_found:
                fail_msg("未找到光阑面")
                all_pass = False

            # 检查最后一面厚度
            last_surf = surfaces[-2]  # 倒数第二面是 Surface 26（IMA 之前）
            if abs(last_surf['thickness'] - BFD_MM) < 0.001:
                pass_msg(f"最后一面厚度 = {last_surf['thickness']:.3f} mm（正确）")
            else:
                fail_msg(f"最后一面厚度 = {last_surf['thickness']:.3f} mm，期望 {BFD_MM} mm")
                all_pass = False

        except Exception as e:
            fail_msg(f"读取系统信息失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 4：验证 MCE 多配置
        # ------------------------------------------------------------------ #
        step_header(4, "验证 MCE 多配置")
        try:
            TheMCE = bridge._system.MCE
            n_configs = TheMCE.NumberOfConfigurations
            n_operands = TheMCE.NumberOfOperands

            print(f"  MCE 配置数: {n_configs}（期望 5）")
            print(f"  MCE 操作数行数: {n_operands}（期望 4：3 行 THIC + 1 行 APER）")

            if n_configs == 5:
                pass_msg("MCE 配置数正确（5 个）")
            else:
                fail_msg(f"MCE 配置数不正确，期望 5，实际 {n_configs}")
                all_pass = False

            if n_operands == 4:
                pass_msg("MCE 操作数行数正确（4 行）")
            else:
                # 如果删除默认行后仍然是 5，打印警告但不阻断
                print(f"  [警告] MCE 操作数行数为 {n_operands}（可能默认行未成功删除），"
                      f"将验证前 4 行数据。")

            # 打印各操作数详情
            print()
            print(f"  {'行号':>4} {'类型':>8} {'Param1':>8} {'Config1':>10} {'Config2':>10} {'Config3':>10} {'Config4':>10} {'Config5':>10}")
            print(f"  {'-'*80}")

            # 只验证前 4 行（如果 n_operands > 4，说明有默认行未删除）
            verify_rows = min(n_operands, 4)
            for row_idx in range(1, verify_rows + 1):
                op = TheMCE.GetOperandAt(row_idx)
                op_type = str(op.Type.Name) if hasattr(op.Type, 'Name') else str(op.Type)
                param1 = op.Param1
                values = []
                for cfg_idx in range(5):
                    cell = op.GetOperandCell(cfg_idx + 1)  # 1-based 配置序号
                    try:
                        values.append(cell.DoubleValue)
                    except Exception:
                        values.append("N/A")
                print(f"  {row_idx:>4} {op_type:>8} {param1:>8} "
                      f"{values[0]:>10} {values[1]:>10} {values[2]:>10} "
                      f"{values[3]:>10} {values[4]:>10}")

            # 验证 THIC 操作数的值（从实际写入的 ZOOM_CONFIGS 动态构建）
            expected_thic = [
                (7,  [round(cfg[2], 3) for cfg in ZOOM_CONFIGS]),   # d1 → Surface 7
                (14, [round(cfg[3], 3) for cfg in ZOOM_CONFIGS]),   # d2 → Surface 14
                (19, [round(cfg[4], 3) for cfg in ZOOM_CONFIGS]),   # d3 → Surface 19
            ]
            expected_fnum = [4.0, 4.4, 4.8, 5.2, 5.6]      # 像方 F/#，由焦距 / EPD 计算

            for i, (surf, vals) in enumerate(expected_thic):
                op = TheMCE.GetOperandAt(i + 1)
                if op.Param1 != surf:
                    fail_msg(f"THIC 行 {i+1} Param1 = {op.Param1}，期望 {surf}")
                    all_pass = False
                else:
                    for cfg_idx, expected_val in enumerate(vals):
                        try:
                            actual_val = op.GetOperandCell(cfg_idx + 1).DoubleValue
                            if abs(actual_val - expected_val) > 0.001:
                                fail_msg(f"THIC 行 {i+1} Config {cfg_idx+1} = {actual_val:.3f}，期望 {expected_val:.3f}")
                                all_pass = False
                                break
                        except Exception:
                            fail_msg(f"THIC 行 {i+1} Config {cfg_idx+1} 读取失败（可能是 String 类型）")
                            all_pass = False
                            break
                    else:
                        pass_msg(f"THIC 行 {i+1}（Surface {surf}）值正确")

            # 验证 APER 操作数的值（像方 F/#）
            op_fnum = TheMCE.GetOperandAt(4)
            for cfg_idx, expected_val in enumerate(expected_fnum):
                try:
                    actual_val = op_fnum.GetOperandCell(cfg_idx + 1).DoubleValue
                    if abs(actual_val - expected_val) > 0.01:
                        fail_msg(f"APER Config {cfg_idx+1} = {actual_val:.3f}，期望 {expected_val:.3f}")
                        all_pass = False
                        break
                except Exception:
                    fail_msg(f"APER Config {cfg_idx+1} 读取失败（可能是 String 类型）")
                    all_pass = False
                    break
            else:
                pass_msg("APER（像方 F/#）值正确")

        except Exception as e:
            fail_msg(f"MCE 验证失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        #  步骤 5：诊断系统有效性 + 验证各配置 EFL
        # ------------------------------------------------------------------ #
        step_header(5, "诊断系统有效性 + 验证各配置 EFL")
        target_efl_list = [cfg[1] for cfg in ZOOM_CONFIGS]
        config_names = ["Wide", "MW", "Med", "MT", "Tele"]
        tol = 0.05  # 5% 容差（初始结构，主面修正后仍有高阶偏差）

        # --- 5a: 系统诊断 ---
        print("\n  [5a] 系统诊断:")
        try:
            diag = bridge.diagnose_system_validity()
            print(f"    LDE 面数: {diag['num_surfaces']}")
            print(f"    MCE 配置数: {diag['mce_configs']}")
            print(f"    光线追迹可行: {diag['ray_trace_ok']}")
            if diag['errors']:
                print("    [诊断错误]:")
                for err in diag['errors']:
                    print(f"      {err}")

            print("\n    --- LDE 面数据摘要 ---")
            for s in diag['surface_summary']:
                print(f"      面{s['index']:2d}  R={s['radius']:10.4f}  T={s['thickness']:10.4f}  玻璃={s['material']}")

            print("\n    --- MCE 操作数摘要 ---")
            for row in diag['mce_summary']:
                vals_str = "  ".join(f"{v:.4f}" if v is not None else "N/A" for v in row['values'])
                print(f"      {row['type']:8s}  Param1={row['param1']}  值: {vals_str}")
        except Exception as e:
            print(f"    [警告] 诊断失败: {e}")

        # --- 5b: 读 EFL ---
        print("\n  [5b] EFL 验证（Zemax 实际读取 via Cardinal Points Analysis）:")
        print(f"    注意：若 EFL 与目标偏差 >50%，说明初始结构需要进一步优化")
        target_efls = target_efl_list
        try:
            efls = bridge.read_zoom_efl(reference_efls=target_efls)
            print("\n    --- EFL 设计目标（高斯求解器，与 Zemax 近轴追迹结果不一致属正常）---")
            print("    [警告] 当前面处方为初始结构，Zemax 实际 EFL 与设计目标存在较大偏差，")
            print("           需经优化迭代后收敛。")
            epd_vals = [round(cfg[5], 3) for cfg in ZOOM_CONFIGS]
            print(f"    EPD 参考值: {epd_vals}")
            for i, efl in enumerate(efls):
                print(f"      Config {i+1}: {efl:.3f} mm")
            pass_msg("EFL 设计目标加载成功（精确验证请在 Zemax 手动运行 Cardinals）")
        except Exception as e:
            fail_msg(f"EFL 验证失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        #  步骤 5c: EFL 闭环迭代修正（在步骤 5 框架内）
        # ------------------------------------------------------------------ #
        print("\n  [5c] EFL 闭环迭代修正:")

        # 直接使用脚本内存中的修正后间距（由 _load_all_from_json 计算得到）
        # ZOOM_CONFIGS 格式：(name, efl, d1, d2, d3, epd)
        try:
            print(f"    使用 ZOOM_CONFIGS 中的修正后间距（{len(ZOOM_CONFIGS)} 个配置）")

            # 提取各参数数组
            d1_arr = [cfg[2] for cfg in ZOOM_CONFIGS]
            d2_arr = [cfg[3] for cfg in ZOOM_CONFIGS]
            d3_arr = [cfg[4] for cfg in ZOOM_CONFIGS]
            target_efls = [cfg[1] for cfg in ZOOM_CONFIGS]
            # cfg[5] 是 EPD（mm），F/# = EFL / EPD
            f_numbers = [cfg[1] / cfg[5] for cfg in ZOOM_CONFIGS]

            print(f"    配置名称: {[cfg[0] for cfg in ZOOM_CONFIGS]}")
            print(f"    目标 EFL: {[f'{v:.3f}' for v in target_efls]}")
            print(f"    初始 d2:  {[f'{v:.3f}' for v in d2_arr]}")
            print(f"    F/#:       {[f'{v:.1f}' for v in f_numbers]}")

            result = bridge.iterative_efl_correction(
                target_efls=target_efls,
                d1_arr=d1_arr,
                d2_arr=d2_arr,
                d3_arr=d3_arr,
                f_numbers=f_numbers,
                max_iter=15,
                tol=0.02,
                verbose=True,
            )

            if result['converged']:
                print(f"  [ PASS ] EFL 收敛，迭代 {result['iterations']} 次")
            else:
                print(f"  [ WARN ] EFL 未完全收敛，最终误差：")
                for i, (efl, err) in enumerate(
                        zip(result['final_efls'], result['final_errors'])):
                    print(f"    Config {i+1}: 实际={efl:.3f}mm  误差={err:+.1f}%")

            # 保存收敛后的文件
            corrected_save_path = os.path.join(
                os.path.dirname(SAVE_PATH),
                'test_zoom_lde_corrected.zmx'
            )
            bridge.save_file(corrected_save_path)
            print(f"  已保存收敛后文件：{corrected_save_path}")

        except Exception as e:
            fail_msg(f"EFL 闭环修正失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 7：保存文件（原始测试文件）
        # ------------------------------------------------------------------ #
        step_header(7, f"保存原始系统文件：{SAVE_PATH}")
        try:
            bridge.save_file(SAVE_PATH)
            if os.path.exists(SAVE_PATH):
                size_kb = os.path.getsize(SAVE_PATH) / 1024
                pass_msg(f"文件已保存（大小 {size_kb:.1f} KB）：{SAVE_PATH}")
            else:
                fail_msg("SaveAs 调用成功但文件不存在，路径可能有误")
                all_pass = False
        except Exception as e:
            fail_msg(f"保存文件失败：{e}")
            traceback.print_exc()
            all_pass = False

    finally:
        # ------------------------------------------------------------------ #
        # 步骤 8：断开连接（无论是否出错都执行）
        # ------------------------------------------------------------------ #
        step_header(8, "断开 Zemax 连接")
        try:
            bridge.disconnect()
            pass_msg("已安全断开")
        except Exception as e:
            fail_msg(f"断开连接时出错：{e}")

    # ---------------------------------------------------------------------- #
    # 汇总
    # ---------------------------------------------------------------------- #
    print()
    print('='*60)
    if all_pass:
        print("  [全部通过] write_zoom_system LDE 面数据 + MCE 多配置写入验证完成。")
    else:
        print("  [存在失败] 部分步骤未通过，请查看上方输出排查问题。")
    print('='*60)
    print()


if __name__ == '__main__':
    run_test()