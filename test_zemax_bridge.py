"""
test_zemax_bridge.py — ZemaxBridge 连接和基础功能验证脚本

验证流程：
  步骤 1: 连接 Zemax（standalone 模式）
  步骤 2: 创建空白系统
  步骤 3: 写入 BK7 单透镜（R1=100, R2=-100, CT=5, EPD=20, λ=0.587μm）
  步骤 4: 读取系统信息，打印各面参数
  步骤 5: 读取 Zemax 赛德尔系数
  步骤 6: 读取 RMS Spot Size（轴上 0° + 离轴 10°）
  步骤 7: 保存为 test_singlet.zmx
  步骤 8: 断开连接

每步有明确的 PASS / FAIL 输出。
"""

import os
import math
import traceback

from zemax_bridge import ZemaxBridge, ZemaxBridgeError


# ---------------------------------------------------------------------------
# 测试参数
# ---------------------------------------------------------------------------
SINGLET_R1        = 100.0       # 前表面曲率半径 (mm)
SINGLET_R2        = -100.0      # 后表面曲率半径 (mm)
SINGLET_CT        = 5.0         # 中心厚度 (mm)
SINGLET_GLASS     = 'N-BK7'    # 玻璃名称
SINGLET_EPD       = 20.0        # 入瞳直径 (mm)
SINGLET_WAVE_UM   = 0.587056    # 参考波长（d 线，微米）
SINGLET_IMG_DIST  = 100.0       # 初始像距 (mm)

# 保存路径
SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'test_singlet.zmx')


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
    print(f"  {tag}  {msg}")

def fail_msg(msg: str = ''):
    tag = '[ FAIL ]'
    print(f"  {tag}  {msg}")


# ---------------------------------------------------------------------------
# 主测试流程
# ---------------------------------------------------------------------------
def run_test():
    bridge = ZemaxBridge()
    all_pass = True

    try:
        # ------------------------------------------------------------------ #
        # 步骤 1：连接 Zemax
        # ------------------------------------------------------------------ #
        step_header(1, "连接 Zemax (standalone 模式)")
        try:
            bridge.connect(mode='standalone')
            pass_msg("Zemax 连接成功")
        except ZemaxBridgeError as e:
            fail_msg(f"连接失败：{e}")
            all_pass = False
            # 连接失败无法继续后续步骤
            return
        except Exception as e:
            fail_msg(f"连接时发生未知异常：{e}")
            traceback.print_exc()
            all_pass = False
            return

        # ------------------------------------------------------------------ #
        # 步骤 2：创建空白系统
        # ------------------------------------------------------------------ #
        step_header(2, "创建空白系统")
        try:
            bridge.new_system()
            pass_msg("空白系统创建成功")
        except Exception as e:
            fail_msg(f"创建空白系统失败：{e}")
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 3：写入单透镜
        # ------------------------------------------------------------------ #
        step_header(3, f"写入 N-BK7 单透镜  "
                       f"R1={SINGLET_R1}, R2={SINGLET_R2}, CT={SINGLET_CT}, "
                       f"EPD={SINGLET_EPD}, λ={SINGLET_WAVE_UM}μm")
        try:
            bridge.write_singlet(
                r1           = SINGLET_R1,
                r2           = SINGLET_R2,
                thickness    = SINGLET_CT,
                glass        = SINGLET_GLASS,
                epd          = SINGLET_EPD,
                wavelength_um= SINGLET_WAVE_UM,
                image_distance= SINGLET_IMG_DIST,
            )
            pass_msg("单透镜写入成功")
        except Exception as e:
            fail_msg(f"写入单透镜失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 4：读取系统信息
        # ------------------------------------------------------------------ #
        step_header(4, "读取系统信息（验证写入是否正确）")
        try:
            info = bridge.read_system_info()
            print(f"  面数（含 OBJ/IMA）：{info['num_surfaces']}")
            print(f"  有效焦距 EFL：{info['efl']:.4f} mm")
            print()
            print(f"  {'面号':>4}  {'曲率半径':>12}  {'厚度':>10}  "
                  f"{'玻璃':>8}  {'半口径':>10}  {'光阑':>4}")
            print(f"  {'-'*60}")
            for s in info['surfaces']:
                r_str = f"{s['radius']:>12.4f}" if s['radius'] != 0 else \
                        f"{'平面':>12}"
                stop  = 'STO' if s['is_stop'] else ''
                print(f"  {s['index']:>4}  {r_str}  "
                      f"{s['thickness']:>10.4f}  "
                      f"{s['material']:>8}  "
                      f"{s['semi_diameter']:>10.4f}  "
                      f"{stop:>4}")

            # 基本合理性检查
            ok = True
            surfs = {s['index']: s for s in info['surfaces']}

            if 1 in surfs:
                s1 = surfs[1]
                if abs(s1['radius'] - SINGLET_R1) > 1e-6:
                    fail_msg(f"面 1 R 值不符：期望 {SINGLET_R1}，实际 {s1['radius']}")
                    ok = False
                if abs(s1['thickness'] - SINGLET_CT) > 1e-6:
                    fail_msg(f"面 1 厚度不符：期望 {SINGLET_CT}，实际 {s1['thickness']}")
                    ok = False
                if s1['material'].upper() not in (SINGLET_GLASS.upper(), 'N-BK7'):
                    fail_msg(f"面 1 玻璃不符：期望 {SINGLET_GLASS}，实际 {s1['material']}")
                    ok = False
            else:
                fail_msg("面 1 不存在")
                ok = False

            if 2 in surfs:
                s2 = surfs[2]
                if abs(s2['radius'] - SINGLET_R2) > 1e-6:
                    fail_msg(f"面 2 R 值不符：期望 {SINGLET_R2}，实际 {s2['radius']}")
                    ok = False
            else:
                fail_msg("面 2 不存在")
                ok = False

            if ok:
                pass_msg("系统信息读取正确，与写入参数吻合")
            else:
                all_pass = False

        except Exception as e:
            fail_msg(f"读取系统信息失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 5：读取赛德尔系数
        # ------------------------------------------------------------------ #
        step_header(5, "读取 Zemax 赛德尔系数")
        try:
            seidel = bridge.read_seidel()
            print(f"  {'面号':>4}  {'S1(球差)':>12}  {'S2(彗差)':>12}  "
                  f"{'S3(像散)':>12}  {'S4(场曲)':>12}  {'S5(畸变)':>12}")
            print(f"  {'-'*72}")
            for row in seidel:
                surf_label = '合计' if row['surface'] == 0 else str(row['surface'])
                print(f"  {surf_label:>4}  "
                      f"{row['S1_spha']:>12.6f}  "
                      f"{row['S2_coma']:>12.6f}  "
                      f"{row['S3_asti']:>12.6f}  "
                      f"{row['S4_fcur']:>12.6f}  "
                      f"{row['S5_dist']:>12.6f}")

            # 检查是否有有效数据（至少合计行不全为 NaN）
            total_row = next((r for r in seidel if r['surface'] == 0), None)
            if total_row and not math.isnan(total_row['S1_spha']):
                pass_msg("赛德尔系数读取成功")
            else:
                fail_msg("合计行数据异常（NaN）")
                all_pass = False

        except Exception as e:
            fail_msg(f"读取赛德尔系数失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 6：读取 RMS Spot Size
        # ------------------------------------------------------------------ #
        step_header(6, "读取 RMS Spot Size（轴上 Field 1 + 离轴 Field 2）")
        try:
            spot_results = bridge.read_spot_rms(field_points=[1, 2])
            print(f"  {'视场':>6}  {'RMS (mm)':>12}  {'GEO (mm)':>12}")
            print(f"  {'-'*36}")
            for res in spot_results:
                print(f"  {res['field_index']:>6}  "
                      f"{res['rms_mm']:>12.6f}  "
                      f"{res['geo_mm']:>12.6f}")

            # 检查是否有有效数据
            valid = all(not math.isnan(r['rms_mm']) for r in spot_results)
            if valid:
                pass_msg("RMS Spot Size 读取成功")
            else:
                fail_msg("部分视场 RMS 数据为 NaN")
                all_pass = False

        except Exception as e:
            fail_msg(f"读取 RMS Spot Size 失败：{e}")
            traceback.print_exc()
            all_pass = False

        # ------------------------------------------------------------------ #
        # 步骤 7：保存文件
        # ------------------------------------------------------------------ #
        step_header(7, f"保存系统文件：{SAVE_PATH}")
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
        print("  [全部通过] ZemaxBridge 基础功能验证完成，所有步骤 PASS。")
    else:
        print("  [存在失败] 部分步骤未通过，请查看上方输出排查问题。")
    print('='*60)
    print()


if __name__ == '__main__':
    run_test()
