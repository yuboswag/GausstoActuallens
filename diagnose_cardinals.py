"""
诊断：Cardinals 分析对象结构探查，目标是找到不依赖 GetTextFile 的 EFL 读取方式。
运行前：Zemax 已打开 test_zoom_lde.zmx，Interactive Extension 已激活。
"""
import sys
sys.path.insert(0, r'D:\myprojects\Action_a')
from zemax_bridge import ZemaxBridge

def run():
    bridge = ZemaxBridge()
    bridge.connect(mode='extension')
    bridge._system.LoadFile(
        r'D:\myprojects\Action_a\test_zoom_lde.zmx', False)

    mce = bridge._system.MCE
    print(f"MCE 配置数: {mce.NumberOfConfigurations}")

    # ── 第一轮：探查 Analyses 对象 ──
    analyses = bridge._system.Analyses
    print("\n=== 探查 Analyses 对象 ===")
    members = [n for n in dir(analyses) if not n.startswith('_')]
    print(f"  成员数: {len(members)}")
    # 打印所有含 Card/EFL/Focal/System/Parax 的成员
    for n in members:
        if any(k in n.lower() for k in ['card', 'efl', 'focal', 'parax', 'system']):
            print(f"    [关键] {n}")
    # 打印全部成员（分页，每行 4 个）
    print(f"  全部成员:")
    for i in range(0, len(members), 4):
        print("    " + "  ".join(f"{m:35s}" for m in members[i:i+4]))

    # ── 第二轮：New_Cardinals，逐配置读取 ──
    print("\n=== 逐配置运行 Cardinals 分析 ===")
    target_efls = [11.887, 33.454, 62.561, 98.366, 140.422]

    for cfg_idx in range(1, mce.NumberOfConfigurations + 1):
        mce.SetCurrentConfiguration(cfg_idx)
        print(f"\n  --- Config {cfg_idx} ---")
        try:
            ca = analyses.New_Cardinals()
            ca.ApplyAndWaitForCompletion()
            results = ca.GetResults()

            # 探查 results 对象
            print(f"    results 类型: {type(results).__name__}")
            res_members = [n for n in dir(results) if not n.startswith('_')]
            print(f"    results 成员: {res_members}")

            # 尝试 DataGrids
            try:
                dg = results.DataGrids
                print(f"    DataGrids 类型: {type(dg).__name__}, 长度: {dg.Length}")
                for gi in range(min(dg.Length, 3)):
                    grid = dg[gi]
                    gm = [n for n in dir(grid) if not n.startswith('_')]
                    print(f"    DataGrid[{gi}] 成员: {gm}")
                    # 尝试读 Grid 数据
                    try:
                        for row in range(min(grid.Rows, 10)):
                            for col in range(min(grid.Cols, 4)):
                                val = grid.GetCell(row, col)
                                if val is not None:
                                    print(f"      [{row},{col}] = {val}")
                    except Exception as e:
                        print(f"      GetCell 失败: {e}")
            except Exception as e:
                print(f"    DataGrids 失败: {e}")

            # 尝试 Texts
            try:
                texts = results.Texts
                print(f"    Texts 长度: {texts.Length}")
                for ti in range(min(texts.Length, 5)):
                    print(f"    Texts[{ti}]: {texts[ti]}")
            except Exception as e:
                print(f"    Texts 失败: {e}")

            # 尝试 NumberOfDataSeries
            try:
                print(f"    NumberOfDataSeries: {results.NumberOfDataSeries}")
            except Exception as e:
                print(f"    NumberOfDataSeries 失败: {e}")

            ca.Close()

        except Exception as e:
            print(f"    New_Cardinals 失败: {e}")

        # 只探第一个配置的详细结构，后续只读 EFL
        if cfg_idx == 1:
            print("\n  （后续配置只尝试读 EFL 数值）")
            break

    # ── 第三轮：尝试直接读 DataGrid 数值（仅基于第一轮的结构信息）──
    print("\n=== 尝试逐配置读 EFL 数值 ===")
    for cfg_idx in range(1, mce.NumberOfConfigurations + 1):
        mce.SetCurrentConfiguration(cfg_idx)
        try:
            ca = analyses.New_Cardinals()
            ca.ApplyAndWaitForCompletion()
            results = ca.GetResults()
            # 打印全部 Text 内容（寻找含 EFL / 焦 的行）
            try:
                texts = results.Texts
                efl_found = None
                for ti in range(texts.Length):
                    t = str(texts[ti])
                    if any(k in t for k in ['EFL', 'Focal', '焦', 'efl']):
                        print(f"    Config {cfg_idx}: [Text {ti}] {t.strip()}")
                        efl_found = t
                if efl_found is None:
                    print(f"    Config {cfg_idx}: 未在 Texts 中找到 EFL 相关行")
                    # 打印所有 Text 内容
                    for ti in range(min(texts.Length, 20)):
                        print(f"      Text[{ti}]: {str(texts[ti]).strip()}")
            except Exception as e:
                print(f"    Config {cfg_idx}: Texts 读取失败: {e}")
            ca.Close()
        except Exception as e:
            print(f"    Config {cfg_idx}: 失败: {e}")

    bridge.disconnect()
    print("\n诊断完成。")

if __name__ == '__main__':
    run()
