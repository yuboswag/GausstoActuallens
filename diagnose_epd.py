"""
验证：SetCurrentConfiguration 后，读取 EPD 并用 EPD × F/# 还原 EFL。
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

    # 从 MCE 读出每个配置的 F/#（APER 行，Param1=0）
    fnum_vals = {}
    for row_i in range(1, mce.NumberOfOperands + 1):
        op = mce.GetOperandAt(row_i)
        if str(op.Type) == 'APER':
            for cfg in range(1, mce.NumberOfConfigurations + 1):
                cell = op.GetOperandCell(cfg)
                fnum_vals[cfg] = cell.DoubleValue
            break
    print(f"从 MCE 读取的 F/# 值: {fnum_vals}")

    # 目标 EFL（来自高斯解）
    target_efls = {1: 11.887, 2: 33.454, 3: 62.561, 4: 98.366, 5: 140.422}

    print("\n── 逐配置读取 EPD，推算 EFL ──")
    print(f"  {'Config':8s} {'F/#':8s} {'EPD':10s} {'EFL推算':12s} {'目标EFL':12s} {'误差%':8s}")
    print(f"  {'-'*60}")

    for cfg_idx in range(1, mce.NumberOfConfigurations + 1):
        mce.SetCurrentConfiguration(cfg_idx)

        # 读 EPD
        try:
            aperture = bridge._system.SystemData.Aperture
            # 探查可用属性（仅第一次）
            if cfg_idx == 1:
                print(f"\n  SystemData.Aperture 成员:")
                for name in dir(aperture):
                    if not name.startswith('_'):
                        try:
                            val = getattr(aperture, name)
                            if not callable(val):
                                print(f"    {name} = {val}")
                        except:
                            pass
                print()

            epd = aperture.EntrancePupilDiameter
            fnum = fnum_vals.get(cfg_idx, 4.0)
            efl_calc = epd * fnum
            target = target_efls[cfg_idx]
            err_pct = (efl_calc - target) / target * 100
            flag = "✅" if abs(err_pct) < 10 else "❌"
            print(f"  Config {cfg_idx}: F/{fnum:.3f}  EPD={epd:.4f}  "
                  f"EFL推算={efl_calc:.3f}  目标={target:.3f}  误差={err_pct:+.1f}% {flag}")
        except Exception as e:
            print(f"  Config {cfg_idx}: 失败: {e}")
            # 探查 Aperture 对象
            if cfg_idx == 1:
                try:
                    aperture = bridge._system.SystemData.Aperture
                    print(f"    dir(Aperture): {[n for n in dir(aperture) if not n.startswith('_')]}")
                except Exception as e2:
                    print(f"    SystemData.Aperture 也失败: {e2}")

    bridge.disconnect()
    print("\n诊断完成。")

if __name__ == '__main__':
    run()
