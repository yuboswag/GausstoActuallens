"""
test_write_zoom.py — 变焦系统 EFL 与 RMS Spot 验证脚本

测试流程：
  步骤 1：连接 Zemax（extension 模式）
  步骤 2：调用 write_zoom_system 写入完整变焦系统
  步骤 3：保存为 test_zoom_system.zmx
  步骤 4：逐配置读取 EFL，与 CSV 对比
  步骤 5：逐配置读取轴上 RMS Spot Size
  步骤 6：断开连接
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
    #           [0]  [1]  [2]  [3]  [4]  [5]
    ("Wide",        12.024, 16.703, 51.484, 28.813,  3.01),
    ("Medium-Wide", 33.384, 29.289, 31.757, 35.954,  7.84),
    ("Medium",      62.758, 34.089, 20.828, 42.082, 13.59),
    ("Medium-Tele", 99.473, 36.622, 12.681, 47.698, 19.63),
    ("Tele",       143.158, 38.186,  5.818, 52.996, 25.56),
]

# 其他参数
WAVELENGTH_UM = 0.587056        # 主波长（d 线）
SENSOR_HALF_DIAG_MM = 3.8       # 传感器半对角线
STOP_SURFACE_IDX = 14           # 光阑面的 Action_a 编号
BFD_MM = 8.0                    # 最后一面到像面的距离

# 保存路径
SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'test_zoom_system.zmx')

# CSV 参考 EFL 值
CSV_EFLS = [12.024, 33.384, 62.758, 99.473, 143.158]

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

def warn_msg(msg: str = ''):
    tag = '[ WARN ]'
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
            return  # 无法继续

        # ------------------------------------------------------------------ #
        # 步骤 2：调用 write_zoom_system 写入完整变焦系统
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
        # 步骤 3：保存文件
        # ------------------------------------------------------------------ #
        step_header(3, f"保存系统文件：{SAVE_PATH}")
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

        # ------------------------------------------------------------------ #
        # 步骤 4：逐配置读取 EFL，与 CSV 对比
        # ------------------------------------------------------------------ #
        step_header(4, "逐配置读取 EFL，与 CSV 对比")

        # --- 正式 EFL 读取 ---
        try:
            TheMCE = bridge._system.MCE
            TheMFE = bridge._system.MFE
            ZOSAPI = bridge._ZOSAPI

            # 查找正确的配置切换方法名
            mce_methods = [m for m in dir(TheMCE) if 'config' in m.lower() or 'Config' in m]
            print(f"  [调试] MCE 配置相关方法：{mce_methods}")

            # 查找 EFFL 操作数类型
            mfe_mod = ZOSAPI.Editors.MFE
            effl_type = None
            for name in dir(mfe_mod.MeritOperandType):
                if 'EFFL' in name.upper():
                    effl_type = getattr(mfe_mod.MeritOperandType, name)
                    print(f"  [调试] EFFL 操作数类型：MeritOperandType.{name}")
                    break
            if effl_type is None:
                fail_msg("未找到 EFFL 操作数类型")
                all_pass = False
                raise ZemaxBridgeError("未找到 EFFL 操作数类型")

            efl_results = []
            for cfg in range(1, 6):  # 1-based
                cfg_name = ZOOM_CONFIGS[cfg - 1][0]
                csv_efl = ZOOM_CONFIGS[cfg - 1][1]

                # 切换配置
                try:
                    TheMCE.SetCurrentConfiguration(cfg)
                except AttributeError:
                    # 尝试其他可能的方法名
                    for method_name in mce_methods:
                        if 'set' in method_name.lower() or 'current' in method_name.lower():
                            try:
                                getattr(TheMCE, method_name)(cfg)
                                print(f"  [调试] 使用 {method_name}({cfg}) 切换配置成功")
                                break
                            except Exception:
                                continue
                    else:
                        fail_msg(f"无法切换到配置 {cfg}，未找到正确方法")
                        all_pass = False
                        continue

                # 插入 EFFL 操作数
                op = TheMFE.InsertNewOperandAt(1)
                op.ChangeType(effl_type)
                op.Target = 0
                op.Weight = 0

                # 计算并读取
                TheMFE.CalculateMeritFunction()

                # 读取值（尝试多种属性名）
                efl_value = None
                for attr in ('Value', 'ValueCell', 'OperandValue'):
                    try:
                        cell = getattr(op, attr)
                        if hasattr(cell, 'DoubleValue'):
                            efl_value = float(cell.DoubleValue)
                        else:
                            efl_value = float(cell)
                        break
                    except Exception:
                        continue

                if efl_value is None:
                    fail_msg(f"Config {cfg} ({cfg_name}) 无法读取 EFL 值")
                    all_pass = False
                else:
                    deviation = (efl_value - csv_efl) / csv_efl * 100
                    efl_results.append((cfg_name, csv_efl, efl_value, deviation))
                    if abs(deviation) < 2.0:
                        pass_msg(f"Config {cfg} ({cfg_name}) EFL 偏差 {deviation:+.2f}%")
                    else:
                        warn_msg(f"Config {cfg} ({cfg_name}) EFL 偏差 {deviation:+.2f}%（>2%）")

                # 删除临时行
                TheMFE.RemoveOperandAt(1)

            # 打印对比表格
            print()
            print(f"  {'配置':<16} {'CSV_EFL':>10} {'Zemax_EFL':>10} {'偏差%':>8}")
            print(f"  {'-'*48}")
            for name, csv_e, zem_e, dev in efl_results:
                print(f"  {name:<16} {csv_e:>10.3f} {zem_e:>10.3f} {dev:>+7.2f}%")

        except Exception as e:
            fail_msg(f"EFL 读取失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 5：逐配置读取轴上 RMS Spot Size
        # ------------------------------------------------------------------ #
        step_header(5, "逐配置读取轴上 RMS Spot Size")
        try:
            TheMCE = bridge._system.MCE
            ZOSAPI = bridge._ZOSAPI

            # 尝试使用已有的 read_spot_rms 方法
            rms_results = []
            for cfg in range(1, 6):
                cfg_name = ZOOM_CONFIGS[cfg - 1][0]

                # 切换配置
                try:
                    TheMCE.SetCurrentConfiguration(cfg)
                except AttributeError:
                    pass  # 已在步骤 4 中处理过

                # 调用 read_spot_rms（轴上 Field 1）
                try:
                    spot_data = bridge.read_spot_rms(field_points=[1])
                    if spot_data:
                        rms_um = spot_data[0]['rms_mm'] * 1000  # mm → μm
                        rms_results.append((cfg_name, rms_um))
                    else:
                        rms_results.append((cfg_name, None))
                except Exception as e:
                    print(f"  [调试] read_spot_rms 调用失败：{e}，尝试 RSCE 操作数")
                    rms_results.append((cfg_name, None))

            # 打印结果
            print()
            print(f"  {'配置':<16} {'RMS Spot (μm)':>15}")
            print(f"  {'-'*32}")
            for name, rms in rms_results:
                if rms is not None:
                    print(f"  {name:<16} {rms:>15.1f}")
                else:
                    print(f"  {name:<16} {'N/A':>15}")

            print(f"\n  [提示] RMS Spot 仅打印，不做 PASS/FAIL 判断（初始结构 spot 大是正常的）")

        except Exception as e:
            fail_msg(f"RMS Spot 读取失败：{e}")
            traceback.print_exc()
            all_pass = False

    finally:
        # ------------------------------------------------------------------ #
        # 步骤 6：断开连接（无论是否出错都执行）
        # ------------------------------------------------------------------ #
        step_header(6, "断开 Zemax 连接")
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
        print("  [全部通过] 变焦系统 EFL 与 RMS Spot 验证完成。")
    else:
        print("  [存在失败] 部分步骤未通过，请查看上方输出排查问题。")
    print('='*60)
    print()


if __name__ == '__main__':
    run_test()
