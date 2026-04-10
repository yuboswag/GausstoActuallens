"""
test_optimization_setup.py — 测试优化 MFE 构建与性能读取

测试流程：
  步骤 1：连接 Zemax（extension 模式）
  步骤 2：调用 write_zoom_system 写入系统
  步骤 3：调用 setup_optimization_mfe 构建 MFE + 设变量
  步骤 4：调用 read_real_performance 读性能
  步骤 5：打印汇总表格
  步骤 6：保存 zmx 文件
  步骤 7：断开连接
"""

import os
import sys
import traceback
from zemax_bridge import ZemaxBridge, ZemaxBridgeError

# ---------------------------------------------------------------------------
# 测试数据（与 test_zoom_lde.py 完全一致）
# ---------------------------------------------------------------------------
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

ZOOM_CONFIGS = [
    # (name, efl, d1, d2, d3, epd)
    ("Wide",        12.024, 16.703, 51.484, 28.813,  3.01),
    ("Medium-Wide", 33.384, 29.289, 31.757, 35.954,  7.84),
    ("Medium",      62.758, 34.089, 20.828, 42.082, 13.59),
    ("Medium-Tele", 99.473, 36.622, 12.681, 47.698, 19.63),
    ("Tele",       143.158, 38.186,  5.818, 52.996, 25.56),
]

# 其他参数
WAVELENGTH_UM = 0.587056
SENSOR_HALF_DIAG_MM = 3.8
STOP_SURFACE_IDX = 14
BFD_MM = 8.0

# 保存路径
SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'test_optimization_setup.zmx')

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
        # 步骤 1：连接 Zemax
        # ------------------------------------------------------------------ #
        step_header(1, "连接 Zemax（extension 模式）")
        try:
            bridge.connect(mode='extension')
            pass_msg("连接成功")
        except Exception as e:
            fail_msg(f"连接失败：{e}")
            traceback.print_exc()
            return

        # ------------------------------------------------------------------ #
        # 步骤 2：调用 write_zoom_system 写入系统
        # ------------------------------------------------------------------ #
        step_header(2, "写入变焦系统（LDE + MCE）")
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
        # 步骤 3：调用 setup_optimization_mfe 构建 MFE + 设变量
        # ------------------------------------------------------------------ #
        step_header(3, "构建优化 MFE + 设置 MCE 变量")
        opt_info = None
        try:
            opt_info = bridge.setup_optimization_mfe(ZOOM_CONFIGS)
            if opt_info:
                pass_msg(f"MFE 构建成功，共 {opt_info['total_operands']} 行操作数")
                print(f"  MCE 变量数: {3 * len(ZOOM_CONFIGS)} (3 行 × {len(ZOOM_CONFIGS)} 配置)")
                print(f"  TOTR 等长约束: {opt_info['num_diff_constraints']} 行 DIFF")
                if opt_info['mf_value'] is not None:
                    print(f"  初始 MF 值: {opt_info['mf_value']:.3f}")
            else:
                fail_msg("setup_optimization_mfe 返回空结果")
                all_pass = False
        except Exception as e:
            fail_msg(f"构建优化 MFE 失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 4：调用 read_real_performance 读性能
        # ------------------------------------------------------------------ #
        step_header(4, "读取真实光线追迹性能（EFL + TTL）")
        perf_data = None
        try:
            perf_data = bridge.read_real_performance(ZOOM_CONFIGS, totr_rows=opt_info.get('totr_rows') if opt_info else None)
            if perf_data and perf_data['configs']:
                pass_msg(f"成功读取 {len(perf_data['configs'])} 个配置的性能数据")
                if perf_data.get('total_mf') is not None:
                    print(f"  总 MF 值: {perf_data['total_mf']:.3f}")
            else:
                fail_msg("未读取到性能数据")
                all_pass = False
        except Exception as e:
            fail_msg(f"读取性能失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 5：打印汇总表格
        # ------------------------------------------------------------------ #
        step_header(5, "性能结果汇总")
        if perf_data and perf_data['configs']:
            print()
            print(f"  {'配置':<16} {'目标EFL':>9} {'实际EFL':>9} {'偏差%':>8} {'TTL':>9}")
            print(f"  {'-'*56}")

            for cfg in perf_data['configs']:
                name = cfg['name']
                target = cfg['target_efl']
                actual = cfg['actual_efl']
                err = cfg['efl_error_pct']
                ttl = cfg.get('ttl', 0)

                err_str = f'{err:+.1f}%' if err == err else 'N/A'
                actual_str = f'{actual:.3f}' if actual == actual else 'N/A'
                ttl_str = f'{ttl:.3f}' if ttl == ttl else 'N/A'

                print(f"  {name:<16} {target:>9.3f} {actual_str:>9} {err_str:>8} {ttl_str:>9}")

            # 打印优化信息
            if opt_info:
                print()
                print(f"  初始 MF 值: {opt_info.get('mf_value', 'N/A')}")
                print(f"  MCE 变量数: {3 * len(ZOOM_CONFIGS)} (3 行 × {len(ZOOM_CONFIGS)} 配置)")
                print(f"  MFE 操作数行数: {opt_info['total_operands']}")
                print(f"  TOTR 等长约束: {opt_info['num_diff_constraints']} 行 DIFF")
        else:
            print("  无性能数据可打印。")

        # ------------------------------------------------------------------ #
        # 步骤 6：保存 zmx 文件
        # ------------------------------------------------------------------ #
        step_header(6, f"保存系统文件：{SAVE_PATH}")
        try:
            bridge.save_file(SAVE_PATH)
            if os.path.exists(SAVE_PATH):
                size_kb = os.path.getsize(SAVE_PATH) / 1024
                pass_msg(f"文件已保存（大小 {size_kb:.1f} KB）：{SAVE_PATH}")
            else:
                fail_msg("SaveAs 调用成功但文件不存在")
                all_pass = False
        except Exception as e:
            fail_msg(f"保存文件失败：{e}")
            traceback.print_exc()
            all_pass = False

    finally:
        # ------------------------------------------------------------------ #
        # 步骤 7：断开连接
        # ------------------------------------------------------------------ #
        step_header(7, "断开 Zemax 连接")
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
        print("  [全部通过] 优化 MFE 构建与性能读取完成。")
    else:
        print("  [存在失败] 部分步骤未通过，请查看上方输出排查问题。")
    print('='*60)
    print()


if __name__ == '__main__':
    run_test()
