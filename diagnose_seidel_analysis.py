"""
探测 ZOS-API 中 Seidel Analysis 的枚举名 + 输出文本格式。

用途：
  确定 SeidelAnalysis 的 AnalysisIDM 枚举名，
  确认其报告是否在切换 config 后反映当前配置的赛德尔系数。

参考：
  _read_efl_via_cardinal (zemax_bridge.py:460-540)
"""
import json, os, sys, tempfile, time, uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ZMX_PATH   = os.path.join(SCRIPT_DIR, 'test_zoom_lde_opt.zmx')

# ── 1. 连接 Zemax ────────────────────────────────────────────────
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
    print("========== 诊断 Seidel Analysis ==========\n")
    print(f"已打开: {ZMX_PATH}")
except Exception as e:
    print(f"[FAIL] 连接/打开 Zemax 失败: {e}")
    if bridge:
        bridge.disconnect()
    sys.exit(1)

ZOSAPI = bridge._ZOSAPI

# ── 2. 枚举 AnalysisIDM ─────────────────────────────────────────
print("\n[Step 1] 列出 AnalysisIDM 枚举:")

idm = ZOSAPI.Analysis.AnalysisIDM
# 反射获取所有枚举值
enum_names = [name for name in dir(idm) if not name.startswith('_')]
print(f"  共 {len(enum_names)} 个枚举")

# 过滤含 Seidel / Aberration 的
hits = [name for name in enum_names
        if 'Seidel' in name or 'Aberration' in name]
print(f"  含 'Seidel' / 'Aberration' 的:")
if hits:
    for h in hits:
        print(f"    - {h}")
else:
    print("    (无)")

# ── 3. 选取枚举 ─────────────────────────────────────────────────
print("\n[Step 2] 选用枚举:", end=' ')

selected = None
for candidate in ['SeidelCoefficients', 'SeidelDiagram', 'AberrationCoefficients']:
    if hasattr(idm, candidate):
        selected = getattr(idm, candidate)
        print(candidate)
        break

if selected is None:
    print("[FAIL] 未找到 Seidel / Aberration 相关枚举")
    bridge.disconnect()
    sys.exit(1)

SELECTED_NAME = [n for n in hits if hasattr(idm, n)][0]

# ── 4. Config 1 报告 ────────────────────────────────────────────
print(f"\n[Step 3] Config 1 的 Seidel 报告({SELECTED_NAME}):")

mce = bridge._system.MCE
mce.SetCurrentConfiguration(1)
for _wait in range(20):
    if mce.CurrentConfiguration == 1:
        break
    time.sleep(0.05)
time.sleep(0.1)

analysis = bridge._system.Analyses.New_Analysis(selected)
try:
    analysis.ApplyAndWaitForCompletion()
    tmp_path = os.path.join(tempfile.gettempdir(),
                            f'seidel_cfg1_{uuid.uuid4().hex}.txt')

    for attempt in range(5):
        try:
            results = analysis.GetResults()
            results.GetTextFile(tmp_path)
        except Exception as e:
            print(f"    [重试 {attempt+1}] GetTextFile 异常: {e}")

        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 50:
            try:
                with open(tmp_path, 'r', encoding='utf-16-le',
                          errors='replace') as f:
                    lines = f.readlines()
                # 成功
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                break
            except Exception as e:
                print(f"    [重试 {attempt+1}] 读取异常: {e}")

        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        if attempt < 4:
            time.sleep(0.1 * (2 ** attempt))
    else:
        lines = [f"[FAIL] 无法读取报告 (最后路径: {tmp_path})"]

    for i, line in enumerate(lines):
        print(f"  {i}: {repr(line)}")
finally:
    analysis.Close()
    time.sleep(0.2)

# ── 5. Config 5 报告(前 30 行) ──────────────────────────────────
print(f"\n[Step 4] Config 5 的 Seidel 报告({SELECTED_NAME}, 前 30 行):")

mce.SetCurrentConfiguration(5)
for _wait in range(20):
    if mce.CurrentConfiguration == 5:
        break
    time.sleep(0.05)
time.sleep(0.1)

analysis5 = bridge._system.Analyses.New_Analysis(selected)
try:
    analysis5.ApplyAndWaitForCompletion()
    tmp_path5 = os.path.join(tempfile.gettempdir(),
                             f'seidel_cfg5_{uuid.uuid4().hex}.txt')

    for attempt in range(5):
        try:
            results5 = analysis5.GetResults()
            results5.GetTextFile(tmp_path5)
        except Exception as e:
            print(f"    [重试 {attempt+1}] GetTextFile 异常: {e}")

        if os.path.exists(tmp_path5) and os.path.getsize(tmp_path5) > 50:
            try:
                with open(tmp_path5, 'r', encoding='utf-16-le',
                          errors='replace') as f:
                    lines5 = f.readlines()
                try:
                    os.unlink(tmp_path5)
                except Exception:
                    pass
                break
            except Exception as e:
                print(f"    [重试 {attempt+1}] 读取异常: {e}")

        try:
            os.unlink(tmp_path5)
        except Exception:
            pass
        if attempt < 4:
            time.sleep(0.1 * (2 ** attempt))
    else:
        lines5 = [f"[FAIL] 无法读取报告 (最后路径: {tmp_path5})"]

    for i, line in enumerate(lines5[:30]):
        print(f"  {i}: {repr(line)}")
finally:
    analysis5.Close()
    time.sleep(0.2)

# ── 6. 清理 ──────────────────────────────────────────────────────
if bridge:
    bridge.disconnect()
print("\n诊断完成")
