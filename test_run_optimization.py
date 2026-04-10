"""
test_run_optimization.py
完整优化流程验证：写入系统 → 建立 MFE → 运行优化 → 读回 EFL 对比目标
"""
import traceback
from zemax_bridge import ZemaxBridge
from test_zoom_lde import SURFACE_PRESCRIPTION, ZOOM_CONFIGS

TARGET_EFL = [cfg[1] for cfg in ZOOM_CONFIGS]
CONFIG_NAMES = [cfg[0] for cfg in ZOOM_CONFIGS]

if __name__ == '__main__':
    bridge = ZemaxBridge()
    try:
        # 步骤1：连接
        print("\n步骤1：连接 Zemax")
        bridge.connect(mode='extension')
        print("  [PASS] 连接成功")

        # 步骤2：写入系统
        print("\n步骤2：写入变焦系统")
        bridge.write_zoom_system(
            surface_prescription=SURFACE_PRESCRIPTION,
            zoom_configs=ZOOM_CONFIGS,
            wavelength_um=0.587056,
            sensor_half_diag_mm=3.8,
            stop_surface_idx=14,
            bfd_mm=8.0,
        )
        print("  [PASS] 写入完成")

        # 步骤3：建立 MFE
        print("\n步骤3：建立优化评价函数")
        mfe_info = bridge.setup_optimization_mfe(ZOOM_CONFIGS)
        print(f"  MFE 共 {mfe_info['total_operands']} 行")
        mf_val = mfe_info['mf_value']
        print(f"  初始 MF = {mf_val:.4f}" if mf_val is not None else "  初始 MF = (无法读取)")

        # 步骤4：读取优化前 EFL
        print("\n步骤4：优化前 EFL")
        efls_before = bridge.read_zoom_efl()
        for i, efl in enumerate(efls_before):
            err = abs(efl - TARGET_EFL[i]) / TARGET_EFL[i] * 100
            print(f"  {CONFIG_NAMES[i]:<22} 目标={TARGET_EFL[i]:.3f}  "
                  f"实测={efl:.3f}  误差={err:.1f}%")

        # 步骤5：运行优化
        print("\n步骤5：运行 DLS 优化（Auto cycles）")
        bridge.run_local_optimization(algorithm='DLS', cycles=0)
        print("  [PASS] 优化完成")

        # 步骤6：读取优化后 EFL
        print("\n步骤6：优化后 EFL")
        efls_after = bridge.read_zoom_efl()
        all_pass = True
        for i, efl in enumerate(efls_after):
            err = abs(efl - TARGET_EFL[i]) / TARGET_EFL[i] * 100
            ok = err < 2.0
            if not ok:
                all_pass = False
            status = 'PASS' if ok else 'FAIL'
            print(f"  [{status}] {CONFIG_NAMES[i]:<22} 目标={TARGET_EFL[i]:.3f}  "
                  f"实测={efl:.3f}  误差={err:.1f}%")

        print()
        print('PASS: 所有配置 EFL 误差 < 2%' if all_pass
              else 'FAIL: 部分配置 EFL 误差超标，可能需要更多优化迭代')

    except Exception:
        traceback.print_exc()
    finally:
        bridge.disconnect()
