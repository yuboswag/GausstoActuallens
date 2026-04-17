"""
调试脚本：检查 write_zoom_system 中的面数问题
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from zemax_bridge import ZemaxBridge

bridge = ZemaxBridge()
try:
    bridge.connect(mode='extension')
    print("连接成功")
    
    # 创建空白系统
    bridge._system.New(False)
    TheLDE = bridge._system.LDE
    print(f"新建系统后的面数: {TheLDE.NumberOfSurfaces}")
    
    # 打印所有面
    for i in range(TheLDE.NumberOfSurfaces):
        s = TheLDE.GetSurfaceAt(i)
        print(f"  面 {i}: 类型={s.SurfaceType}, 注释={s.Comment}")
    
    # 插入26个面
    for i in range(1, 27):
        TheLDE.InsertNewSurfaceAt(i)
        print(f"  在索引 {i} 插入后，面数: {TheLDE.NumberOfSurfaces}")
    
    print(f"最终面数: {TheLDE.NumberOfSurfaces}")
    
    # 断开连接
    bridge.disconnect()
except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()