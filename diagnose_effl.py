"""
诊断脚本：探查 EFFL 操作数在多配置系统下的正确读值方式。
运行前：确保 Zemax 已打开 test_zoom_lde.zmx 并激活 Interactive Extension。
"""
import sys
sys.path.insert(0, r'D:\myprojects\Action_a')
from zemax_bridge import ZemaxBridge

def run():
    bridge = ZemaxBridge()
    bridge.connect(mode='extension')

    # 加载已保存的变焦系统（上次写入的）
    bridge._system.LoadFile(
        r'D:\myprojects\Action_a\test_zoom_lde.zmx', False)

    mce = bridge._system.MCE
    mfe = bridge._system.MFE

    print(f"MCE 配置数: {mce.NumberOfConfigurations}")
    print(f"MFE 当前行数: {mfe.NumberOfOperands}")

    # ── 方法 A: 插入 EFFL，CalculateMeritFunction，探查所有可访问属性 ──
    print("\n=== 方法 A: 插入单行 EFFL，探查属性 ===")
    new_op = mfe.AddOperand()
    effl_type = bridge._ZOSAPI.Editors.MFE.MeritOperandType.EFFL
    new_op.ChangeType(effl_type)
    new_op.GetOperandCell(
        bridge._ZOSAPI.Editors.MFE.MeritColumn.Param1
    ).IntegerValue = 0  # 主波长

    bridge._system.MFE.CalculateMeritFunction()

    print(f"  new_op.Value = {new_op.Value}")

    # 探查 MeritColumn 枚举所有可能的列名
    merit_col = bridge._ZOSAPI.Editors.MFE.MeritColumn
    print(f"\n  MeritColumn 枚举成员:")
    for name in dir(merit_col):
        if not name.startswith('_'):
            print(f"    {name}")

    # 尝试逐列读值
    print(f"\n  尝试用各列枚举读 GetOperandCell().DoubleValue:")
    for name in dir(merit_col):
        if name.startswith('_'):
            continue
        try:
            col_val = getattr(merit_col, name)
            cell = new_op.GetOperandCell(col_val)
            try:
                dv = cell.DoubleValue
                print(f"    列 {name:30s} DoubleValue = {dv}")
            except:
                try:
                    iv = cell.IntegerValue
                    print(f"    列 {name:30s} IntegerValue = {iv}")
                except:
                    print(f"    列 {name:30s} 无法读取")
        except Exception as e:
            print(f"    列 {name:30s} 异常: {e}")

    # 删除临时行
    mfe.RemoveOperandAt(mfe.NumberOfOperands)

    # ── 方法 B: 插入 5 行 EFFL（各配置一行），一次 CalculateMeritFunction ──
    print("\n=== 方法 B: 插入 5 行 EFFL，一次计算，逐行读 Value ===")
    inserted = []
    for cfg in range(1, 6):
        op = mfe.AddOperand()
        op.ChangeType(effl_type)
        op.GetOperandCell(
            bridge._ZOSAPI.Editors.MFE.MeritColumn.Param1
        ).IntegerValue = 0
        inserted.append(op)

    bridge._system.MFE.CalculateMeritFunction()

    for i, op in enumerate(inserted):
        print(f"  行 {i+1}: Value = {op.Value}")

    # 清理
    for _ in inserted:
        mfe.RemoveOperandAt(mfe.NumberOfOperands)

    # ── 方法 C: 用 SetCurrentConfiguration + Cardinals 分析 ──
    print("\n=== 方法 C: Cardinals 分析对象探查 ===")
    try:
        analyses = bridge._system.Analyses
        print(f"  Analyses 对象类型: {type(analyses)}")
        print(f"  dir(Analyses) 部分成员（含 Card / EFL / Cardin）:")
        for name in dir(analyses):
            if any(k in name.lower() for k in ['card', 'efl', 'focal']):
                print(f"    {name}")

        # 尝试 New_Cardinals
        try:
            ca = analyses.New_Cardinals()
            print(f"\n  New_Cardinals() 成功，类型: {type(ca)}")
            ca.ApplyAndWaitForCompletion()
            results = ca.GetResults()
            print(f"  GetResults() 类型: {type(results)}")
            print(f"  dir(results) 关键成员:")
            for name in dir(results):
                if not name.startswith('_'):
                    print(f"    {name}")
            ca.Close()
        except Exception as e:
            print(f"  New_Cardinals() 失败: {e}")
    except Exception as e:
        print(f"  Analyses 探查失败: {e}")

    bridge.disconnect()
    print("\n诊断完成。")

if __name__ == '__main__':
    run()
